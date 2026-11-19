# OCI Image Unpacker

离线排查容器文件缺失时，从交付镜像还原最终根文件系统的服务。

- `POST /unpack`：上传仅含单个 manifest 的 OCI Image Layout tar，当次请求返回
  还原后的 `rootfs.tar`；
- `POST /preflight`：配置迁到 ConfigMap 之前，上传同一份镜像 tar 和一份描述
  Pod、ConfigMap、容器卷挂载及待查绝对路径的 JSON，预检挂载结果并逐个路径
  返回文件 SHA-256 与来源。与 `/unpack` 共用同一套校验与层还原逻辑。

## 快速开始（docker compose）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，打开 <http://localhost:8000/docs>：

1. 先调 `GET /sample-image` 下载样例镜像 `sample-image.tar`（Swagger 里点 "Download file"）；
2. 再在 `POST /unpack` 里上传该 tar，响应即为 `rootfs.tar`；
3. 配置预检：调 `GET /sample-mounts` 下载与样例镜像配套的挂载清单
   `sample-mounts.json`，在 `POST /preflight` 的 `file` 字段上传镜像 tar、
   `mounts` 字段粘贴该 JSON，响应为逐路径的 SHA-256 与来源。

命令行等价操作：

```bash
curl -OJ http://localhost:8000/sample-image          # 下载样例镜像
curl -F "file=@sample-image.tar" -OJ \
     http://localhost:8000/unpack                    # 得到 rootfs.tar
tar -tf rootfs.tar                                   # 查看还原结果

curl -OJ http://localhost:8000/sample-mounts         # 下载配套挂载清单
curl -F "file=@sample-image.tar" \
     -F "mounts=<sample-mounts.json" \
     http://localhost:8000/preflight                 # 预检结果 JSON
```

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/unpack` | 上传 OCI Image Layout tar（单 manifest），返回 `rootfs.tar`；任何校验/解包失败返回 400，不返回残缺文件系统 |
| `POST` | `/preflight` | 上传镜像 tar（表单字段 `file`）+ 挂载清单 JSON（表单字段 `mounts`，或以文件 `mounts_file` 上传），返回逐路径的 SHA-256 与来源；任何挂载/镜像错误返回 400 |
| `GET` | `/sample-image` | 下载样例镜像（含删除、opaque、硬链接覆盖） |
| `GET` | `/sample-mounts` | 下载与样例镜像配套的挂载清单 JSON |
| `GET` | `/healthz` | 健康检查 |

## 校验与还原语义

- **完整性校验**：index.json 引用的 manifest blob、manifest 引用的 config 与
  各 layer blob 都会核对**存在性、字节大小、SHA-256**；每个 layer 解压后
  （gzip 层先解压）再与 config 中 `rootfs.diff_ids` 逐一核对。层数与
  diff_ids 数量不一致、摘要不符、缺块、损坏，整次请求报错。
- **层格式**：支持未压缩层（`...layer.v1.tar`）与 gzip 层
  （`...layer.v1.tar+gzip`，含 docker 对应 mediaType）。
- **按清单层序还原**：普通文件、目录、符号链接、硬链接与权限位（含
  setuid/sgid/sticky）；目录权限与 mtime 在全部层应用完后统一设置，避免
  只读目录阻断同层/上层写入。
- **whiteout / opaque**：每层先扫描全部条目，把 `.wh.<name>` 与
  `.wh..wh..opq` 的删除动作**只作用于下层文件系统**，然后再写入本层新
  内容。因此删除标记在层内的位置（文件前/后）不会影响同层新增文件。
- **硬链接覆盖安全**：新文件先写临时文件再 `rename` 替换，绝不原地截断，
  所以覆盖硬链接的一端时，另一端保留原字节。输出 tar 中仍共享 inode 的
  硬链接会以硬链接条目保留。
- **路径安全**：条目路径拒绝绝对路径与 `..`；符号链接目标（绝对目标按
  rootfs 内解释）与硬链接目标经解析后必须落在隔离目录内，写入前再随
  符号链接逐层解析校验，任何逃逸都会让整次请求失败。
- **请求隔离**：每个请求使用独立临时目录，失败即清理，成功响应发送后
  清理；请求之间互不影响。

## ConfigMap 挂载预检（`POST /preflight`）

请求体为 multipart：`file` 是镜像 tar，`mounts` 是挂载清单 JSON。清单结构：

```json
{
  "namespace": "default",
  "container": "app",
  "pod": {
    "metadata": {"name": "sample-pod", "namespace": "default"},
    "spec": {
      "containers": [{
        "name": "app",
        "volumeMounts": [
          {"name": "cfg", "mountPath": "/etc/demo"},
          {"name": "cfg", "mountPath": "/app/data/keep.txt", "subPath": "greeting.txt"}
        ]
      }],
      "volumes": [{
        "name": "cfg",
        "configMap": {
          "name": "demo-config",
          "items": [{"key": "greeting.txt", "path": "greeting.txt"},
                    {"key": "extra.conf", "path": "nested/extra.conf"}]
        }
      }]
    }
  },
  "configMaps": [{"name": "demo-config", "namespace": "default",
                  "data": {"greeting.txt": "...", "extra.conf": "...", "tool.override": "..."}}],
  "paths": ["/app/data/keep.txt", "/bin/tool-link", "/etc/demo/greeting.txt"]
}
```

- 只支持使用 **`data`** 的**必选** ConfigMap 卷；`binaryData`、`optional: true`
  整次报错。Pod、卷、ConfigMap 均按 Pod 的命名空间解析，命名空间不一致
  报错。
- `items[].path` 指定投影路径；省略时用键名。键是 ConfigMap 的 `data` 键，
  投影路径可含目录层级。
- **目录挂载**（无 `subPath`）盖住整棵子树，卷内全部投影文件重建于挂载点；
  **subPath 挂载**按投影路径选一个文件，只替换挂载点本身（可覆盖镜像文件或
  替换符号链接）。
- 以下情况**整次 400**：卷 / ConfigMap / 键缺失、`items` 投影路径冲突、挂载点
  重叠、挂载类型不符（文件挂到目录上、目录挂到非目录上）、挂载路径（含各级
  父目录与目标本身）含符号链接。
- 挂载前后各自从根逐级解析符号链接（绝对目标按 rootfs 根解释），逐个路径返回：

  | `status` | 含义 |
  | --- | --- |
  | `regular` | 普通文件，附 `sha256`、`size`、`source` |
  | `not_found` | 路径不存在（含被 whiteout / opaque 删掉的下层文件） |
  | `non_regular` | 存在但不是普通文件，附 `type`（directory/symlink/fifo/…） |
  | `link_cycle` | 符号链接成环 |
  | `error` | 无法解析（如超过链接层数） |

- `source` 区分来源：
  - 镜像文件：`{"kind": "image", "layer": N}`，`N` 为按清单从 1 起的层号，
    即**写入该文件字节的层**；来源按 inode 追踪，覆盖硬链接的一端后，另一端
    仍报旧字节的层号；被 whiteout / opaque 删除的文件在挂载前即 `not_found`，
    没有来源。
  - ConfigMap 文件：`{"kind": "configmap", "name": "...", "key": "..."}`。

响应示例：

```json
{
  "namespace": "default",
  "container": "app",
  "mounts": [{"mountPath": "/etc/demo", "mode": "directory", "configMap": "demo-config", "subPath": null}],
  "paths": [
    {"path": "/bin/tool-link",
     "before": {"status": "regular", "sha256": "…", "size": 8,
                "source": {"kind": "image", "layer": 1}},
     "after":  {"status": "regular", "sha256": "…", "size": 8,
                "source": {"kind": "image", "layer": 1}}}
  ]
}
```

`GET /sample-mounts` 返回与样例镜像配套的挂载清单（一个目录挂载 + 两个
subPath 挂载，待查路径覆盖镜像文件、硬链接两端、符号链接、whiteout 删除的
文件与 ConfigMap 投影文件）。

## 样例镜像内容

两层（第一层 gzip，第二层未压缩），用于验证上述语义：

- 下层文件 `app/config.txt`、`app/data/remove.txt` 被 whiteout 删除；
- `secret/` 被标记 opaque：下层的 `secret/token.txt` 消失，同层新增的
  `secret/public.txt` 保留；
- `app/data/rebuilt.txt` 在同层先写新内容、后出现其 whiteout —— 新内容
  必须保留；
- `bin/tool` 与 `bin/tool-link` 在下层是同一硬链接，上层只覆盖
  `bin/tool`（`tool-v2`），`bin/tool-link` 必须仍是 `tool-v1`；
- 相对符号链接 `bin/tool-alias -> tool` 与绝对符号链接
  `etc/motd-link -> /etc/motd`；
- 权限位：`bin/tool` 0755、`secret/` 0700。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UNPACK_WORKDIR` | `$TMPDIR/oci-unpacker` | 请求临时目录与样例镜像存放位置 |
