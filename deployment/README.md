# DOCX RAG smoke 离线发布 Quickstart

这是唯一的 smoke 操作文档。主路径固定为：本地 preflight → 一键构建和打包
→ 上传恰好七个文件 → 服务器 preflight → install → deploy → 冒烟验证。
生产冻结、故障恢复细节和清理命令见完整参考
`design/public/offline-build-and-server-deployment.md`，不需要从该长文档拼接
smoke 命令。

## 边界与前置条件

- 本地在仓库根目录执行，Git 工作区必须干净；Python 使用 `.venv/bin/python`。
- 本地 Docker、Compose、buildx、固定基础镜像、runtime wheel、OCR 模型与
  wheelhouse 必须已经离线就绪；脚本不 pull、不下载、不换版本。
- 服务器只允许更新 `rag-*` 容器、网络和 `/data/tyf/RAG`；服务器禁止
  build、pull、pip/apt/npm 安装及外网请求。
- smoke 可使用 provisional retrieval；`/ready=503` 是预期。它不能冒充
  production。production 必须另行提供 frozen 配置、FREEZE_DECISION、SBOM、
  acceptance 和完整 evaluation 证据。
- 默认复用已通过模型契约验证的 embedding、reranker 与四个 LLM。不要在
  `.60` 重复部署或额外占用 GPU。只有明确选择 self-hosted 时，才单独使用
  `deployment/model-services/`；该目录及模型权重/镜像不进入 RAG smoke 包。
- 服务器可使用 containerd 或 classic image store。前者校验
  `Descriptor.digest=manifest digest`，后者校验 `.Id=config digest`；两者
  都严格检查 tag、`linux/amd64`、OCI revision 和归档 SHA256。
- 服务器管理员预先准备只属于本次交付的
  `/data/tyf/RAG/incoming/<release-id>-<corpus-id>/`，上传账号只能写该目录。

## 1. 本地 preflight 与一键 release

先确认版本和磁盘；这些命令只读：

```bash
git status --short
.venv/bin/python --version
docker version
docker compose version
docker buildx version
docker sbom version
df -hT .
```

一条命令完成固定资产检查、runtime wheel 准备、两个 `--network none` 镜像
构建、两个断网自检、corpus manifest、smoke 双包及全新目录复验：

```bash
.venv/bin/python scripts/release_smoke.py
```

成功后读取脱敏报告，不从终端历史复制猜测路径：

```bash
report=artifacts/release-smoke-report.json
release_dir="$(${PWD}/.venv/bin/python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["release_dir"])' \
  "${report}")"
release_id="$(${PWD}/.venv/bin/python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["release_id"])' \
  "${report}")"
source_revision="$(${PWD}/.venv/bin/python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["source_revision"])' \
  "${report}")"
corpus_id="$(${PWD}/.venv/bin/python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["corpus_id"])' \
  "${report}")"
(cd "${release_dir}" && sha256sum -c RELEASE_MANIFEST.sha256)
test "$(cat "${release_dir}/RELEASE_ID")" = "${release_id}"
test "$(cat "${release_dir}/SOURCE_REVISION")" = "${source_revision}"
```

`SOURCE_REVISION` 是完整 40 位 Git revision，`RELEASE_ID` 是本次唯一发布 ID。
`release_id` 统一用于 release 目录、镜像 tag、归档名和 candidate env；
`RAG_RELEASE_REVISION` 使用完整 `source_revision`，不得截断为 release ID。

## 2. 上传恰好七个文件

设置已批准的服务器名和唯一 delivery；不得上传 `.env`、模型权重或模型镜像：

```bash
RAG_SERVER='<server-host>'
delivery="/data/tyf/RAG/incoming/${release_id}-${corpus_id}"
scp \
  "${release_dir}/RELEASE_MANIFEST.sha256" \
  "${release_dir}/offline_bundle.py" \
  "${release_dir}/offline_bundle.py.sha256" \
  "${release_dir}/rag-runtime-${release_id}.tar.gz" \
  "${release_dir}/rag-runtime-${release_id}.tar.gz.sha256" \
  "${release_dir}/rag-corpus-${corpus_id}.tar.gz" \
  "${release_dir}/rag-corpus-${corpus_id}.tar.gz.sha256" \
  "${RAG_SERVER}:${delivery}/"
```

## 3. 服务器校验、解包与 server-preflight.sh

以下命令由服务器 root 在已收权的唯一 delivery 中执行。先校验七文件，再解到
本次专用目录；解包器拒绝覆盖、符号链接、越界路径和摘要不一致：

```bash
cd "/data/tyf/RAG/incoming/${release_id}-${corpus_id}"
test "$(find . -maxdepth 1 -type f | wc -l)" -eq 7
sha256sum -c RELEASE_MANIFEST.sha256
sha256sum -c offline_bundle.py.sha256
python3 offline_bundle.py \
  "rag-runtime-${release_id}.tar.gz" \
  "rag-runtime-${release_id}.tar.gz.sha256" \
  extracted-runtime --top-level runtime
python3 offline_bundle.py \
  "rag-corpus-${corpus_id}.tar.gz" \
  "rag-corpus-${corpus_id}.tar.gz.sha256" \
  extracted-corpus --top-level corpus
runtime_dir="$(pwd -P)/extracted-runtime/runtime"
corpus_dir="$(pwd -P)/extracted-corpus/corpus"
bash "${runtime_dir}/verify-offline.sh"
bash "${runtime_dir}/server-preflight.sh"
```

`server-preflight.sh` 只读检查 Docker/Compose、store 模式、NVIDIA runtime、
GPU/显存、磁盘、8088/8091/8092、既有 `rag-*` 状态、固定目录和模型端点。
它只输出脱敏 JSON，不创建目录、不加载镜像、不启动容器，也不输出 token。
`FAIL` 必须停止；`WARN` 需由操作员确认，例如已有同套 `rag-*` 或首次目录状态。

## 4. install 与 deploy

安装只接受已验证的 runtime/corpus 绝对路径：

```bash
bash "${runtime_dir}/install.sh" "${runtime_dir}" "${corpus_dir}"
release_dir="/data/tyf/RAG/releases/${release_id}"
candidate="/data/tyf/RAG/shared/env/candidates/${release_id}.env"
test ! -e "${candidate}"
install -m 0600 "${release_dir}/.env.example" "${candidate}"
```

编辑 candidate，只填环境值；至少确认完整 40 位 `RAG_RELEASE_REVISION`、
三个固定镜像 tag、`RAG_ACCESS_MODE=shared_corpus`、绝对 bind mount、三个模型
endpoint 数组和令牌变量。禁止把 token 打到终端或日志。随后执行：

```bash
bash "${release_dir}/deploy.sh" "${candidate}"
```

active rag.env 只能由 deploy.sh 成功后发布。需要回滚时显式传入 active env：

```bash
bash "${release_dir}/rollback.sh" /data/tyf/RAG/shared/env/rag.env
```

deploy 先验证三份归档，再 `docker load`，随后按实际 store 校验 manifest/config
身份，最终只执行 `docker compose up -d --no-build --pull never`。失败时恢复旧
env、current、容器和 worker 状态；不删除 SQLite、Qdrant 或 corpus。

## 5. 冒烟验证

### 冒烟成功标准

- 只启动 `rag-app`、`rag-ocr` 和 `rag-qdrant`。
- `rag-worker` 必须不存在或保持停止。
- `/live` 必须返回 HTTP 200。
- Qdrant `/readyz` 必须返回 HTTP 200。
- OCR `/ready` 必须返回 HTTP 200。
- CUDA device count 必须大于 0。
- provisional 阶段 `/ready` 返回 HTTP 503 才是成功。
- 六份模型契约报告全部 `status=passed` 前，不得冻结检索参数或启动 `rag-worker`。

执行以下命令逐项验证：

```bash
docker ps --filter name=rag- --format '{{.Names}} {{.Status}}'
test "$(curl -fsS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/live)" = 200
docker exec rag-app python -c \
  "import urllib.request; assert urllib.request.urlopen('http://rag-qdrant:6333/readyz', timeout=2).status == 200"
docker exec rag-app python -c \
  "import urllib.request; assert urllib.request.urlopen('http://rag-ocr:8090/ready', timeout=2).status == 200"
docker exec rag-ocr python -c \
  'import paddle; assert paddle.device.cuda.device_count() > 0'
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8088/ready)" = 503
test -z "$(docker ps --filter name=rag-worker -q)"
```

embedding、reranker 和四个 LLM 未全部通过前不得声称 production ready。
完整备份、生产验收和清理步骤见上述完整参考。
