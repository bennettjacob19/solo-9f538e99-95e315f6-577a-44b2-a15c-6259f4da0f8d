"""ConfigMap mount preflight.

Given a single-manifest OCI image tar and a JSON description of a Pod, this
module rebuilds the image rootfs exactly like ``/unpack`` does (same
verification, same layer application), materialises the requested required
ConfigMap volume mounts and reports, for every requested absolute path, the
SHA-256 and origin of the file *before* and *after* mounting.

Origin reporting:

* image files report the 1-based manifest layer that wrote their bytes;
* origins follow inodes, so overwriting one end of a hardlink pair does not
  change the layer reported for the other end;
* files removed by a whiteout or an opaque marker have no origin at all
  (the lookup reports ``not_found``);
* ConfigMap files report the ConfigMap name and the data key.

Whole-request failures (HTTP 400): an unresolved volume/ConfigMap/key
reference, a projection conflict, overlapping mount points, a type
mismatch, or a mount path containing a symlink.

Per-path statuses: ``regular`` (with sha256 + source), ``not_found``,
``non_regular`` (with the entry type) and ``link_cycle``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile

from .unpacker import CHUNK, MAX_SYMLINK_DEPTH, restore_image


class PreflightError(Exception):
    """Invalid mount description. Aborts the whole request."""


# ---------------------------------------------------------------------------
# small validation helpers
# ---------------------------------------------------------------------------


def _norm_projected_path(raw, what):
    """Validate a relative ConfigMap projection path. Returns a POSIX path."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PreflightError(f"{what}: invalid projection path {raw!r}")
    if raw.startswith("/"):
        raise PreflightError(f"{what}: projection path must be relative: {raw!r}")
    parts = []
    for p in raw.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise PreflightError(f"{what}: '..' not allowed in {raw!r}")
        parts.append(p)
    if not parts:
        raise PreflightError(f"{what}: empty projection path {raw!r}")
    return "/".join(parts)


def _norm_mount_path(raw):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PreflightError(f"invalid mountPath {raw!r}")
    if not raw.startswith("/"):
        raise PreflightError(f"mountPath must be absolute: {raw!r}")
    parts = []
    for p in raw.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            raise PreflightError(f"mountPath must not contain '..': {raw!r}")
        parts.append(p)
    if not parts:
        raise PreflightError("mounting over '/' is not supported")
    return "/" + "/".join(parts)


def _abs_lookup_path(raw):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PreflightError(f"invalid absolute path {raw!r}")
    if not raw.startswith("/"):
        raise PreflightError(f"path must be absolute: {raw!r}")
    return _norm_mount_path(raw)


def _overlaps(a, b):
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


# ---------------------------------------------------------------------------
# spec parsing / mount planning
# ---------------------------------------------------------------------------


class _ConfigMap:
    def __init__(self, name, namespace, data):
        self.name = name
        self.namespace = namespace
        self.data = data  # key -> bytes


class _Volume:
    def __init__(self, name, config_map, items):
        self.name = name
        self.config_map = config_map
        # projection path -> (key, bytes)
        self.projection = items


class _Mount:
    def __init__(self, volume, mount_path, sub_path):
        self.volume = volume
        self.mount_path = mount_path
        self.sub_path = sub_path  # projected path or None


def _load_json(raw, what):
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise PreflightError(f"invalid {what}: {e}")


def _parse_spec(raw):
    doc = _load_json(raw, "mount spec")
    if not isinstance(doc, dict):
        raise PreflightError("mount spec must be a JSON object")

    pod = doc.get("pod")
    if not isinstance(pod, dict):
        raise PreflightError("mount spec: missing 'pod' object")
    spec = pod.get("spec", pod)
    if not isinstance(spec, dict):
        raise PreflightError("mount spec: invalid pod spec")

    # Pod namespace: everything (Pod, ConfigMaps) is resolved in it.
    meta = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
    if "namespace" in spec:
        raise PreflightError("namespace must be set on the Pod, not in its spec")
    namespaces = [
        v
        for v in (
            doc.get("namespace"),
            meta.get("namespace"),
            pod.get("namespace"),
        )
        if v is not None
    ]
    for ns in namespaces[1:]:
        if ns != namespaces[0]:
            raise PreflightError(
                f"namespace mismatch: {namespaces[0]!r} vs {ns!r}"
            )
    namespace = namespaces[0] if namespaces else "default"
    if not isinstance(namespace, str) or not namespace:
        raise PreflightError("invalid namespace")

    # ConfigMaps available in that namespace.
    cms_raw = doc.get("configMaps", [])
    if isinstance(cms_raw, dict):
        cms_raw = [
            {"name": name, **(value if isinstance(value, dict) else {})}
            for name, value in cms_raw.items()
        ]
    if not isinstance(cms_raw, list):
        raise PreflightError("mount spec: 'configMaps' must be a list")
    config_maps = {}
    for cm in cms_raw:
        if not isinstance(cm, dict):
            raise PreflightError("mount spec: invalid ConfigMap entry")
        name = cm.get("name")
        if not isinstance(name, str) or not name:
            raise PreflightError("mount spec: ConfigMap without a name")
        cm_ns = cm.get("namespace", namespace)
        if cm_ns != namespace:
            raise PreflightError(
                f"ConfigMap {name!r} is not in namespace {namespace!r}"
            )
        if "binaryData" in cm:
            raise PreflightError(
                f"ConfigMap {name!r}: only 'data' is supported, not 'binaryData'"
            )
        data = cm.get("data", {})
        if not isinstance(data, dict):
            raise PreflightError(f"ConfigMap {name!r}: 'data' must be an object")
        parsed = {}
        for key, value in data.items():
            if not isinstance(value, str):
                raise PreflightError(
                    f"ConfigMap {name!r}: value of {key!r} is not a string"
                )
            if "/" in key or key in (".", "..") or "\x00" in key:
                raise PreflightError(f"ConfigMap {name!r}: invalid key {key!r}")
            parsed[key] = value.encode("utf-8")
        if name in config_maps:
            raise PreflightError(f"duplicate ConfigMap {name!r}")
        config_maps[name] = _ConfigMap(name, cm_ns, parsed)

    # Volumes referenced by the selected container.
    volumes = {}
    for vol in spec.get("volumes", []):
        if not isinstance(vol, dict) or not isinstance(vol.get("name"), str):
            raise PreflightError("mount spec: volume without a name")
        if vol["name"] in volumes:
            raise PreflightError(f"duplicate volume {vol['name']!r}")
        volumes[vol["name"]] = vol

    container_name = doc.get("container")
    if not isinstance(container_name, str) or not container_name:
        raise PreflightError("mount spec: missing 'container' name")
    containers = spec.get("containers", [])
    if not isinstance(containers, list):
        raise PreflightError("mount spec: 'containers' must be a list")
    container = next(
        (c for c in containers if isinstance(c, dict) and c.get("name") == container_name),
        None,
    )
    if container is None:
        raise PreflightError(f"container {container_name!r} not found in pod")

    mounts = []
    resolved_volumes = {}
    for vm in container.get("volumeMounts", []):
        if not isinstance(vm, dict):
            raise PreflightError("invalid volumeMount entry")
        if "subPathExpr" in vm:
            raise PreflightError("subPathExpr is not supported")
        vol_name = vm.get("name")
        if not isinstance(vol_name, str) or not vol_name:
            raise PreflightError("volumeMount without a volume name")
        if vol_name not in volumes:
            raise PreflightError(
                f"volumeMount references missing volume {vol_name!r}"
            )
        mount_path = _norm_mount_path(vm.get("mountPath"))

        if vol_name not in resolved_volumes:
            resolved_volumes[vol_name] = _resolve_volume(
                volumes[vol_name], config_maps, namespace
            )
        volume = resolved_volumes[vol_name]

        sub_path = None
        if vm.get("subPath") is not None:
            sub_path = _norm_projected_path(
                vm.get("subPath"), f"subPath of mount {mount_path!r}"
            )
            if sub_path not in volume.projection:
                raise PreflightError(
                    f"mount {mount_path!r}: subPath {sub_path!r} is not a "
                    f"projected file of ConfigMap {volume.config_map!r}"
                )
        mounts.append(_Mount(volume, mount_path, sub_path))

    # Mount points must be pairwise disjoint: a mount would otherwise cover
    # (directory mount) or sit inside (subPath mount) another mount.
    for i in range(len(mounts)):
        for j in range(i + 1, len(mounts)):
            if _overlaps(mounts[i].mount_path, mounts[j].mount_path):
                raise PreflightError(
                    f"mount points overlap: {mounts[i].mount_path!r} and "
                    f"{mounts[j].mount_path!r}"
                )

    paths_raw = doc.get("paths", [])
    if not isinstance(paths_raw, list):
        raise PreflightError("mount spec: 'paths' must be a list")
    paths = [_abs_lookup_path(p) for p in paths_raw]

    return {
        "namespace": namespace,
        "container": container_name,
        "mounts": mounts,
        "paths": paths,
    }


def _resolve_volume(vol, config_maps, namespace):
    """Turn a ConfigMap volume into a projection map, or fail."""
    name = vol["name"]
    source = vol.get("configMap")
    if not isinstance(source, dict):
        raise PreflightError(
            f"volume {name!r}: only ConfigMap volumes are supported"
        )
    if source.get("optional") is True:
        raise PreflightError(
            f"volume {name!r}: optional ConfigMaps are not supported"
        )
    cm_name = source.get("name")
    if not isinstance(cm_name, str) or not cm_name:
        raise PreflightError(f"volume {name!r}: ConfigMap reference without a name")
    cm = config_maps.get(cm_name)
    if cm is None or cm.namespace != namespace:
        raise PreflightError(
            f"volume {name!r}: ConfigMap {cm_name!r} not found in namespace "
            f"{namespace!r}"
        )

    items = source.get("items")
    projection = {}
    if items is None:
        for key in cm.data:
            projection[key] = (key, cm.data[key])
    else:
        if not isinstance(items, list):
            raise PreflightError(f"volume {name!r}: 'items' must be a list")
        for item in items:
            if not isinstance(item, dict):
                raise PreflightError(f"volume {name!r}: invalid item")
            key = item.get("key")
            if not isinstance(key, str) or not key:
                raise PreflightError(f"volume {name!r}: item without a key")
            if "/" in key or key in (".", "..") or "\x00" in key:
                raise PreflightError(f"volume {name!r}: invalid key {key!r}")
            if key not in cm.data:
                raise PreflightError(
                    f"volume {name!r}: ConfigMap {cm_name!r} has no key {key!r}"
                )
            path = _norm_projected_path(
                item.get("path", key), f"volume {name!r} item {key!r}"
            )
            if path in projection:
                raise PreflightError(
                    f"volume {name!r}: projection conflict at {path!r}"
                )
            for other in projection:
                if _overlaps(path, other):
                    raise PreflightError(
                        f"volume {name!r}: projection conflict between "
                        f"{path!r} and {other!r}"
                    )
            projection[path] = (key, cm.data[key])

    return _Volume(name, cm_name, projection)


# ---------------------------------------------------------------------------
# filesystem lookups
# ---------------------------------------------------------------------------


def _resolve_path(root, path, origin, cm_origin):
    """Resolve an absolute path inside *root*, following every symlink.

    Absolute symlink targets are interpreted relative to the rootfs root,
    exactly like inside a container.  Returns a status dict.
    """
    stack = []  # resolved components below root
    todo = path.split("/")[1:]  # drop the leading empty component
    seen = set()
    hops = 0
    while todo:
        comp = todo.pop(0)
        if comp in ("", "."):
            continue
        if comp == "..":
            if stack:
                stack.pop()
            continue
        candidate = os.path.join(root, *stack, comp)
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            return {"status": "not_found"}
        except OSError as e:
            return {"status": "error", "detail": str(e)}
        if stat.S_ISLNK(st.st_mode):
            ident = (candidate, tuple(todo))
            if ident in seen:
                return {"status": "link_cycle", "detail": f"{path!r} loops"}
            seen.add(ident)
            hops += 1
            if hops > MAX_SYMLINK_DEPTH:
                return {
                    "status": "error",
                    "detail": f"too many symlinks resolving {path!r}",
                }
            try:
                target = os.readlink(candidate)
            except OSError as e:
                return {"status": "error", "detail": str(e)}
            if target.startswith("/"):
                stack = []
            todo = target.split("/") + todo
            continue
        stack.append(comp)

    final = os.path.join(root, *stack)
    try:
        st = os.lstat(final)
    except FileNotFoundError:
        return {"status": "not_found"}
    except OSError as e:
        return {"status": "error", "detail": str(e)}

    if stat.S_ISREG(st.st_mode):
        digest = _sha256_file(final)
        source = _source_for(st, origin, cm_origin)
        result = {"status": "regular", "sha256": digest, "size": st.st_size}
        if source is not None:
            result["source"] = source
        return result

    return {"status": "non_regular", "type": _entry_type(st.st_mode)}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


def _source_for(st, origin, cm_origin):
    key = (st.st_dev, st.st_ino)
    if key in cm_origin:
        name, data_key = cm_origin[key]
        return {"kind": "configmap", "name": name, "key": data_key}
    if key in origin:
        return {"kind": "image", "layer": origin[key]}
    return None


def _entry_type(mode):
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISCHR(mode):
        return "char_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


# ---------------------------------------------------------------------------
# mount application
# ---------------------------------------------------------------------------


def _check_mount_path(root, mount_path):
    """Reject a mount path with a symlink anywhere along it (target included)."""
    comps = mount_path.strip("/").split("/")
    cur = root
    for i, comp in enumerate(comps):
        cur = os.path.join(cur, comp)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            continue  # nothing below exists yet, so nothing to follow
        except OSError as e:
            raise PreflightError(f"cannot inspect mount path {mount_path!r}: {e}")
        if stat.S_ISLNK(st.st_mode):
            raise PreflightError(
                f"mount path contains a symlink: {mount_path!r}"
            )
        if i < len(comps) - 1 and not stat.S_ISDIR(st.st_mode):
            raise PreflightError(
                f"mount path {mount_path!r} crosses a non-directory {cur!r}"
            )


def _remove_path(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _write_cm_file(path, data, cm_name, key, cm_origin):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".cm-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    st = os.lstat(path)
    cm_origin[(st.st_dev, st.st_ino)] = (cm_name, key)


def _materialize_dir_mount(root, mount, cm_origin):
    target = os.path.join(root, mount.mount_path.strip("/"))
    if os.path.lexists(target) and (
        not os.path.isdir(target) or os.path.islink(target)
    ):
        raise PreflightError(
            f"mount {mount.mount_path!r}: cannot mount a directory over a "
            "non-directory"
        )
    # A directory mount covers the whole subtree previously at the mount
    # point; only the projected ConfigMap files remain.
    _remove_path(target)
    os.makedirs(target)
    for rel, (key, data) in sorted(mount.volume.projection.items()):
        dest = os.path.join(target, *rel.split("/"))
        _write_cm_file(dest, data, mount.volume.config_map, key, cm_origin)
    for dirpath, _dirnames, _filenames in os.walk(target):
        try:
            os.chmod(dirpath, 0o755)
        except OSError:
            pass


def _materialize_subpath_mount(root, mount, cm_origin):
    target = os.path.join(root, mount.mount_path.strip("/"))
    if os.path.isdir(target) and not os.path.islink(target):
        raise PreflightError(
            f"mount {mount.mount_path!r}: cannot mount a file over a directory"
        )
    key, data = mount.volume.projection[mount.sub_path]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _remove_path(target)
    _write_cm_file(target, data, mount.volume.config_map, key, cm_origin)


def apply_mounts(root, mounts):
    cm_origin = {}
    for mount in mounts:
        _check_mount_path(root, mount.mount_path)
    for mount in mounts:
        if mount.sub_path is not None:
            _materialize_subpath_mount(root, mount, cm_origin)
        else:
            _materialize_dir_mount(root, mount, cm_origin)
    return cm_origin


# ---------------------------------------------------------------------------
# top level
# ---------------------------------------------------------------------------


def preflight(upload_path, spec_raw, workdir):
    """Run the ConfigMap mount preflight; return the JSON-serialisable result."""
    plan = _parse_spec(spec_raw)
    rootfs, origin = restore_image(upload_path, workdir)

    before = {
        path: _resolve_path(rootfs, path, origin, {}) for path in plan["paths"]
    }

    cm_origin = apply_mounts(rootfs, plan["mounts"])
    after = {
        path: _resolve_path(rootfs, path, origin, cm_origin) for path in plan["paths"]
    }

    return {
        "namespace": plan["namespace"],
        "container": plan["container"],
        "mounts": [
            {
                "mountPath": m.mount_path,
                "mode": "subPath" if m.sub_path is not None else "directory",
                "configMap": m.volume.config_map,
                "subPath": m.sub_path,
            }
            for m in plan["mounts"]
        ],
        "paths": [
            {"path": path, "before": before[path], "after": after[path]}
            for path in plan["paths"]
        ],
    }
