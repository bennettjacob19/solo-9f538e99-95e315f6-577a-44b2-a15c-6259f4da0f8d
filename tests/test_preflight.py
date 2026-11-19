"""Tests for POST /preflight: ConfigMap mounts over an unpacked rootfs."""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sample_mounts import SAMPLE_MOUNT_SPEC

from test_unpack import LayoutEditor, _with_layer2, make_layer, sha256


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def sample(client):
    r = client.get("/sample-image")
    assert r.status_code == 200
    return r.content


def spec(**overrides):
    doc = copy.deepcopy(SAMPLE_MOUNT_SPEC)
    doc.update(overrides)
    return doc


def run(client, image, document, field="mounts"):
    if not isinstance(document, (str, bytes)):
        document = json.dumps(document)
    return client.post(
        "/preflight",
        files={"file": ("image.tar", image, "application/x-tar")},
        data={field: document},
    )


def by_path(body):
    return {entry["path"]: entry for entry in body["paths"]}


def sha(data: bytes) -> str:
    return sha256(data)


# ---------------------------------------------------------------------------
# sample spec / happy path
# ---------------------------------------------------------------------------


def test_sample_mounts_endpoint(client):
    r = client.get("/sample-mounts")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert json.loads(r.content) == SAMPLE_MOUNT_SPEC


def test_sample_preflight(client, sample):
    r = run(client, sample, SAMPLE_MOUNT_SPEC)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["namespace"] == "default"
    assert body["container"] == "app"
    assert len(body["mounts"]) == 3
    modes = {m["mountPath"]: m["mode"] for m in body["mounts"]}
    assert modes == {
        "/etc/demo": "directory",
        "/app/data/keep.txt": "subPath",
        "/app/data/rebuilt.txt": "subPath",
    }

    paths = by_path(body)

    # untouched image file: layer 1
    assert paths["/app/data/keep.txt"]["before"] == {
        "status": "regular",
        "sha256": sha(b"keep me\n"),
        "size": 8,
        "source": {"kind": "image", "layer": 1},
    }
    # subPath mount replaces the file in place
    assert paths["/app/data/keep.txt"]["after"] == {
        "status": "regular",
        "sha256": sha(b"hello from demo-config\n"),
        "size": 23,
        "source": {"kind": "configmap", "name": "demo-config", "key": "greeting.txt"},
    }

    # whiteout-deleted file: no origin before or after
    assert paths["/app/data/remove.txt"]["before"] == {"status": "not_found"}
    assert paths["/app/data/remove.txt"]["after"] == {"status": "not_found"}

    # subPath file from the "tool" key provides the bytes
    assert paths["/app/data/rebuilt.txt"]["before"]["source"] == {
        "kind": "image",
        "layer": 2,
    }
    after = paths["/app/data/rebuilt.txt"]["after"]
    assert after["source"] == {
        "kind": "configmap",
        "name": "demo-config",
        "key": "tool.override",
    }
    assert after["sha256"] == sha(b"tool-from-configmap\n")

    # directory mount: items include a nested projection path
    assert paths["/etc/demo/greeting.txt"]["after"]["source"] == {
        "kind": "configmap",
        "name": "demo-config",
        "key": "greeting.txt",
    }
    assert paths["/etc/demo/nested/extra.conf"]["after"]["source"] == {
        "kind": "configmap",
        "name": "demo-config",
        "key": "extra.conf",
    }

    # hardlink pair: overwriting one end kept the other's bytes in layer 1
    assert paths["/bin/tool"]["before"] == {
        "status": "regular",
        "sha256": sha(b"tool-v2\n"),
        "size": 8,
        "source": {"kind": "image", "layer": 2},
    }
    # the other end still reports the *original* layer, not the overwrite layer
    assert paths["/bin/tool-link"]["before"] == {
        "status": "regular",
        "sha256": sha(b"tool-v1\n"),
        "size": 8,
        "source": {"kind": "image", "layer": 1},
    }

    # symlink is resolved before lookup
    assert paths["/bin/tool-alias"]["before"]["source"] == {
        "kind": "image",
        "layer": 2,
    }
    # a directory is reported as non-regular
    assert paths["/secret"]["before"] == {"status": "non_regular", "type": "directory"}


def test_preflight_does_not_touch_unmentioned_files(client, sample):
    doc = spec(paths=["/etc/motd"])
    r = run(client, sample, doc)
    assert r.status_code == 200, r.text
    entry = r.json()["paths"][0]
    assert entry["before"] == entry["after"]
    assert entry["before"]["source"] == {"kind": "image", "layer": 1}


def test_directory_mount_defaults_to_key_names(client, sample):
    doc = spec(
        paths=["/etc/cfg/a", "/etc/cfg/b"],
    )
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "full", "mountPath": "/etc/cfg"}
    ]
    doc["pod"]["spec"]["volumes"] = [
        {"name": "full", "configMap": {"name": "demo-config"}}
    ]
    doc["configMaps"][0]["data"] = {"a": "A\n", "b": "B\n"}
    r = run(client, sample, doc)
    assert r.status_code == 200, r.text
    paths = by_path(r.json())
    assert paths["/etc/cfg/a"]["after"]["source"] == {
        "kind": "configmap",
        "name": "demo-config",
        "key": "a",
    }
    assert paths["/etc/cfg/b"]["after"]["sha256"] == sha(b"B\n")


def test_directory_mount_hides_whole_subtree(client, sample):
    doc = spec(paths=["/app/data/keep.txt", "/app/data/greeting.txt"])
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "cfg", "mountPath": "/app/data"}
    ]
    r = run(client, sample, doc)
    assert r.status_code == 200, r.text
    paths = by_path(r.json())
    # the image file below the mount point is covered by the mount
    assert paths["/app/data/keep.txt"]["before"]["source"] == {
        "kind": "image",
        "layer": 1,
    }
    assert paths["/app/data/keep.txt"]["after"] == {"status": "not_found"}
    assert paths["/app/data/greeting.txt"]["after"]["source"] == {
        "kind": "configmap",
        "name": "demo-config",
        "key": "greeting.txt",
    }


def test_directory_mount_over_regular_file(client, sample):
    doc = spec(paths=[])
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "cfg", "mountPath": "/etc/motd"}
    ]
    _fail(client, sample, doc, "non-directory")


def test_configmap_value_is_utf8(client, sample):
    doc = spec(paths=["/etc/uni/note"])
    doc["configMaps"][0]["data"] = {"note": "héllo\n"}
    doc["pod"]["spec"]["volumes"] = [
        {
            "name": "uni",
            "configMap": {
                "name": "demo-config",
                "items": [{"key": "note", "path": "note"}],
            },
        }
    ]
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "uni", "mountPath": "/etc/uni"}
    ]
    r = run(client, sample, doc)
    assert r.status_code == 200, r.text
    after = by_path(r.json())["/etc/uni/note"]["after"]
    assert after["sha256"] == sha("héllo\n".encode())
    assert after["size"] == len("héllo\n".encode())
    layer = make_layer([
        ("app/loop-a", "symlink", "loop-b"),
        ("app/loop-b", "symlink", "loop-a"),
    ])
    doc = spec(paths=["/app/loop-a"])
    r = run(client, _with_layer2(sample, layer), doc)
    assert r.status_code == 200, r.text
    assert r.json()["paths"][0]["before"]["status"] == "link_cycle"


def test_relative_symlink_chain_resolved(client, sample):
    layer = make_layer([
        ("app/hop-a", "symlink", "hop-b"),
        ("app/hop-b", "symlink", "../etc/motd"),
    ])
    doc = spec(paths=["/app/hop-a"])
    r = run(client, _with_layer2(sample, layer), doc)
    assert r.status_code == 200, r.text
    entry = r.json()["paths"][0]["before"]
    assert entry["sha256"] == sha(b"hello from sample image\n")
    assert entry["source"] == {"kind": "image", "layer": 1}


def test_missing_image_blob_fails(client, sample):
    ed = LayoutEditor(sample)
    del ed.files[f"blobs/sha256/{ed.layer_digest(1)}"]
    r = run(client, ed.to_bytes(), SAMPLE_MOUNT_SPEC)
    assert r.status_code == 400
    assert "missing blob" in r.json()["detail"]


# ---------------------------------------------------------------------------
# whole-request failures
# ---------------------------------------------------------------------------


def _fail(client, sample, doc, needle):
    r = run(client, sample, doc)
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"], r.json()["detail"]


def test_missing_volume(client, sample):
    doc = spec()
    doc["pod"]["spec"]["containers"][0]["volumeMounts"][0]["name"] = "nope"
    _fail(client, sample, doc, "missing volume")


def test_missing_configmap(client, sample):
    doc = spec(configMaps=[])
    _fail(client, sample, doc, "not found in namespace")


def test_configmap_wrong_namespace(client, sample):
    doc = spec()
    doc["configMaps"][0]["namespace"] = "other"
    _fail(client, sample, doc, "not in namespace")


def test_namespace_mismatch(client, sample):
    doc = spec(namespace="prod")
    _fail(client, sample, doc, "namespace mismatch")


def test_missing_key(client, sample):
    doc = spec()
    doc["pod"]["spec"]["volumes"][0]["configMap"]["items"][0]["key"] = "absent"
    _fail(client, sample, doc, "no key")


def test_optional_configmap_rejected(client, sample):
    doc = spec()
    doc["pod"]["spec"]["volumes"][0]["configMap"]["optional"] = True
    _fail(client, sample, doc, "optional")


def test_binary_data_rejected(client, sample):
    doc = spec()
    doc["configMaps"][0]["binaryData"] = {"x": "eA=="}
    _fail(client, sample, doc, "binaryData")


def test_projection_conflict(client, sample):
    doc = spec()
    doc["pod"]["spec"]["volumes"][0]["configMap"]["items"].append(
        {"key": "unused.txt", "path": "greeting.txt"}
    )
    _fail(client, sample, doc, "projection conflict")


def test_projection_path_escape(client, sample):
    doc = spec()
    doc["pod"]["spec"]["volumes"][1]["configMap"]["items"][0]["path"] = "../tool"
    _fail(client, sample, doc, "..")


def test_overlapping_mounts(client, sample):
    doc = spec()
    doc["pod"]["spec"]["containers"][0]["volumeMounts"].append(
        {"name": "cfg", "mountPath": "/etc/demo/nested", "subPath": "greeting.txt"}
    )
    _fail(client, sample, doc, "overlap")


def test_subpath_not_projected(client, sample):
    doc = spec()
    doc["pod"]["spec"]["containers"][0]["volumeMounts"][1]["subPath"] = "unused.txt"
    _fail(client, sample, doc, "not a projected file")


def test_file_over_directory_mismatch(client, sample):
    doc = spec()
    doc["pod"]["spec"]["containers"][0]["volumeMounts"].append(
        {
            "name": "cfg",
            "mountPath": "/etc",
            "subPath": "greeting.txt",
        }
    )
    # "/etc" overlaps "/etc/demo" -> rejected as an overlap first
    _fail(client, sample, doc, "overlap")

    doc = spec()
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "cfg", "mountPath": "/app/data", "subPath": "greeting.txt"}
    ]
    _fail(client, sample, doc, "over a directory")


def test_symlink_in_mount_path(client, sample):
    doc = spec(paths=[])
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "cfg", "mountPath": "/bin/tool-alias/x", "subPath": "greeting.txt"}
    ]
    _fail(client, sample, doc, "symlink")

    doc = spec(paths=[])
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "cfg", "mountPath": "/bin/tool-alias"}
    ]
    _fail(client, sample, doc, "symlink")


def test_directory_mount_over_symlink(client, sample):
    doc = spec(paths=[])
    doc["pod"]["spec"]["containers"][0]["volumeMounts"] = [
        {"name": "cfg", "mountPath": "/etc/motd-link"}
    ]
    _fail(client, sample, doc, "symlink")


def test_missing_spec_field(client, sample):
    r = client.post(
        "/preflight", files={"file": ("image.tar", sample, "application/x-tar")}
    )
    assert r.status_code == 400
    assert "mounts" in r.json()["detail"]


def test_spec_uploaded_as_file(client, sample):
    r = client.post(
        "/preflight",
        files={
            "file": ("image.tar", sample, "application/x-tar"),
            "mounts_file": (
                "sample-mounts.json",
                json.dumps(SAMPLE_MOUNT_SPEC).encode(),
                "application/json",
            ),
        },
    )
    assert r.status_code == 200, r.text
    assert by_path(r.json())["/app/data/keep.txt"]["after"]["source"] == {
        "kind": "configmap",
        "name": "demo-config",
        "key": "greeting.txt",
    }


def test_invalid_json_spec(client, sample):
    r = run(client, sample, "{not json")
    assert r.status_code == 400
    assert "mount spec" in r.json()["detail"]


def test_non_configmap_volume_rejected(client, sample):
    doc = spec()
    doc["pod"]["spec"]["volumes"][0] = {"name": "cfg", "secret": {"secretName": "x"}}
    _fail(client, sample, doc, "only ConfigMap")
