"""Mount spec paired with the sample image served at GET /sample-mounts.

It describes a Pod whose container mounts two required ConfigMap volumes:
one as a whole directory, one as a single file via ``subPath``.  The
``paths`` list exercises image files (including the hardlink pair and a
symlink), a file deleted by a whiteout and a ConfigMap-provided file.

Send the returned JSON as the ``mounts`` form field of ``POST /preflight``
together with ``sample-image.tar``.
"""

from __future__ import annotations

import json

SAMPLE_MOUNT_SPEC = {
    "namespace": "default",
    "container": "app",
    "pod": {
        "metadata": {"name": "sample-pod", "namespace": "default"},
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "image": "sample:latest",
                    "volumeMounts": [
                        {"name": "cfg", "mountPath": "/etc/demo"},
                        {
                            "name": "cfg",
                            "mountPath": "/app/data/keep.txt",
                            "subPath": "greeting.txt",
                        },
                        {
                            "name": "toolcfg",
                            "mountPath": "/app/data/rebuilt.txt",
                            "subPath": "tool",
                        },
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "cfg",
                    "configMap": {
                        "name": "demo-config",
                        "items": [
                            {"key": "greeting.txt", "path": "greeting.txt"},
                            {"key": "extra.conf", "path": "nested/extra.conf"},
                        ],
                    },
                },
                {
                    "name": "toolcfg",
                    "configMap": {
                        "name": "demo-config",
                        "items": [
                            {"key": "tool.override", "path": "tool"},
                        ],
                    },
                },
            ],
        },
    },
    "configMaps": [
        {
            "name": "demo-config",
            "namespace": "default",
            "data": {
                "greeting.txt": "hello from demo-config\n",
                "extra.conf": "extra=1\n",
                "tool.override": "tool-from-configmap\n",
                "unused.txt": "not projected\n",
            },
        }
    ],
    "paths": [
        "/app/data/keep.txt",
        "/app/data/remove.txt",
        "/app/data/rebuilt.txt",
        "/etc/demo/greeting.txt",
        "/etc/demo/nested/extra.conf",
        "/bin/tool",
        "/bin/tool-link",
        "/bin/tool-alias",
        "/secret",
    ],
}


def sample_mount_spec_json() -> str:
    return json.dumps(SAMPLE_MOUNT_SPEC, indent=2, sort_keys=True) + "\n"
