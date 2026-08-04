# DOCX RAG 简单模块化部署指南

本指南是 `deployment/simple` 的唯一操作路径。它部署四个容器：
`rag-app`、`rag-worker`、`rag-ocr`、`rag-qdrant`。模型服务使用服务器已有端点；
DOCX、SQLite 和 Qdrant 数据均保存在服务器 bind mount 中。

> 本流程固定使用 `RAG_RUN_MODE=demo`。它只用于内网效果验证，不代表生产就绪，
> 不能替代参数冻结、生产验收或安全评审。

## 1. 本地前置检查

在本地仓库执行：

```bash
cd /home/jerry/work/RAG
git status --short
test -z "$(git status --porcelain --untracked-files=all)"
docker version
docker buildx version
docker image inspect qdrant/qdrant:v1.18.3
docker image ls docx-rag-ocr
test -x .venv/bin/python
```

最后一条 `docker image ls` 必须至少显示一张本地固定 OCR 镜像。构建过程使用
`--network none`，不会下载依赖、基础镜像或模型。

## 2. 第一次构建

```bash
cd /home/jerry/work/RAG
.venv/bin/python scripts/build_simple_bundle.py
HEAD12="$(git rev-parse --short=12 HEAD)"
PACKAGE_DIR="$(pwd)/artifacts/simple-deploy/${HEAD12}"
ls -lhA "${PACKAGE_DIR}"
```

输出目录必须包含以下独立文件：

```text
app-image.tar.gz
app-image.tar.gz.sha256
ocr-image.tar.gz
ocr-image.tar.gz.sha256
qdrant-image.tar.gz
qdrant-image.tar.gz.sha256
corpus.tar.gz
corpus.tar.gz.sha256
compose.yaml
.env.example
deploy.sh
update-app.sh
DEPLOYMENT_GUIDE.md
```

本地再次校验四个模块：

```bash
cd "${PACKAGE_DIR}"
sha256sum -c app-image.tar.gz.sha256
sha256sum -c ocr-image.tar.gz.sha256
sha256sum -c qdrant-image.tar.gz.sha256
sha256sum -c corpus.tar.gz.sha256
```

## 3. 上传第一次部署包

先设置服务器登录名和地址，再使用 `rsync` 上传完整目录：

```bash
SERVER="USER@SERVER_IP"
REMOTE_PACKAGE="/data/tyf/RAG/simple/${HEAD12}"
ssh "${SERVER}" "mkdir -p '${REMOTE_PACKAGE}'"
rsync -av --progress "${PACKAGE_DIR}/" "${SERVER}:${REMOTE_PACKAGE}/"
```

没有 `rsync` 时，可用同一路径执行：

```bash
scp -r "${PACKAGE_DIR}/." "${SERVER}:${REMOTE_PACKAGE}/"
```

## 4. 服务器创建完整 rag.env

登录服务器后执行：

```bash
cd "/data/tyf/RAG/simple/REPLACE_SHORT_GIT_SHA"
cp .env.example /data/tyf/RAG/rag.env
chmod 600 /data/tyf/RAG/rag.env
openssl rand -hex 32
```

把 `openssl rand -hex 32` 分别执行四次，替换四个 token。然后编辑
`/data/tyf/RAG/rag.env`。完整结构如下；镜像和 SHA 已由构建器填入，模型端点、
token 及服务器地址由部署人替换：

```dotenv
RAG_APP_IMAGE=docx-rag:REPLACE_SHORT_GIT_SHA
RAG_OCR_IMAGE=<包内.env.example中的固定OCR镜像>
RAG_QDRANT_IMAGE=qdrant/qdrant:v1.18.3
RAG_RELEASE_REVISION=REPLACE_FULL_GIT_SHA
RAG_SIMPLE_COMPOSE_FILE=/data/tyf/RAG/simple/REPLACE_SHORT_GIT_SHA/compose.yaml
RAG_PROJECT_ROOT=/data/tyf/RAG
RAG_STATE_PATH=/data/tyf/RAG/data/state
RAG_QDRANT_PATH=/data/tyf/RAG/data/qdrant
RAG_DOCS_PATH=/data/tyf/RAG/data/docs
RAG_LOGS_PATH=/data/tyf/RAG/logs
RAG_PORT=8088
RAG_QDRANT_ALIAS=rag-docx-active
RAG_ACCESS_MODE=shared_corpus
RAG_TRACE_MODE=SAFE
RAG_OCR_GPU_DEVICE_ID=0
RAG_QUERY_TOKEN=<随机64位十六进制值>
RAG_ADMIN_TOKEN=<不同的随机64位十六进制值>
RAG_QDRANT_API_KEY=<不同的随机64位十六进制值>
RAG_OCR_API_TOKEN=<不同的随机64位十六进制值>
RAG_EMBEDDING_ENDPOINTS='["http://实际embedding地址"]'
RAG_RERANKER_ENDPOINTS='["http://实际reranker地址"]'
RAG_LLM_ENDPOINTS='["http://LLM1","http://LLM2","http://LLM3","http://LLM4"]'
RAG_EMBEDDING_MODEL=Qwen3-Embedding-0.6B
RAG_RERANKER_MODEL=Qwen3-Reranker-0.6B
RAG_LLM_MODEL=Qwen/Qwen3-8B-AWQ
RAG_EMBEDDING_API_TOKEN=
RAG_RERANKER_API_TOKEN=
RAG_LLM_API_TOKEN=
RAG_MAX_EMBEDDING_CONCURRENCY=4
RAG_MAX_RERANKER_CONCURRENCY=4
RAG_MAX_LLM_CONCURRENCY=4
RAG_MAX_OCR_CONCURRENCY=1
```

确认 `RAG_SIMPLE_COMPOSE_FILE` 与实际上传目录完全一致。

## 5. 第一次部署并自动建立全量索引

`deploy.sh` 会校验四个 SHA、加载三张镜像、首次解压 corpus、设置 app UID
10001 的 bind mount 权限、启动三个常驻服务、用一次性 worker 执行 full index，
并等待 demo `/ready=200`：

```bash
cd "/data/tyf/RAG/simple/REPLACE_SHORT_GIT_SHA"
bash deploy.sh \
  /data/tyf/RAG/rag.env \
  "/data/tyf/RAG/simple/REPLACE_SHORT_GIT_SHA"
```

如果 `/data/tyf/RAG/data/docs` 已非空，脚本会拒绝覆盖。不要删除已有 SQLite、
Qdrant 或 corpus 来绕过错误。

## 6. 查看状态和日志

```bash
ENV_FILE=/data/tyf/RAG/rag.env
COMPOSE_FILE="/data/tyf/RAG/simple/REPLACE_SHORT_GIT_SHA/compose.yaml"
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps
docker logs --tail 200 rag-app
docker logs --tail 200 rag-ocr
docker logs --tail 200 rag-qdrant
curl -fsS http://127.0.0.1:8088/live
curl -fsS http://127.0.0.1:8088/ready
```

`/ready` 成功响应必须同时包含：

```json
{"ready":true,"run_mode":"demo","production_ready":false}
```

实际响应还会包含各依赖组件状态。

## 7. 打开前端并提交测试问题

浏览器打开：

```text
http://SERVER_IP:8088/
```

也可在服务器直接提交一次流式问题：

```bash
QUERY_TOKEN="$(awk -F= '$1=="RAG_QUERY_TOKEN"{print substr($0,index($0,"=")+1)}' /data/tyf/RAG/rag.env)"
curl -N \
  -H "Authorization: Bearer ${QUERY_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"demo-001","question":"请概括文档中的主要要求，并给出引用。"}' \
  http://127.0.0.1:8088/api/chat
```

## 8. 手工创建或重建全量索引

第一次部署已经自动执行一次 full index。DOCX 变更或收到
`REINDEX_REQUIRED` 提示时，执行：

```bash
docker compose \
  --env-file /data/tyf/RAG/rag.env \
  -f "/data/tyf/RAG/simple/REPLACE_SHORT_GIT_SHA/compose.yaml" \
  --profile index run --rm --no-deps rag-worker \
  index full --idempotency-key "manual-full-$(date +%Y%m%d%H%M%S)"
curl -fsS http://127.0.0.1:8088/ready
```

## 9. 后续只构建和更新 app

本地代码提交后执行：

```bash
cd /home/jerry/work/RAG
test -z "$(git status --porcelain --untracked-files=all)"
.venv/bin/python scripts/build_app_update.py
NEW12="$(git rev-parse --short=12 HEAD)"
APP_UPDATE_DIR="$(pwd)/artifacts/app-update/${NEW12}"
sha256sum -c "${APP_UPDATE_DIR}/app-image.tar.gz.sha256"
rsync -av --progress "${APP_UPDATE_DIR}/" \
  "${SERVER}:/data/tyf/RAG/app-update/${NEW12}/"
```

服务器默认只重建 `rag-app`：

```bash
NEW12="<app-update目录名>"
bash "/data/tyf/RAG/app-update/${NEW12}/update-app.sh" \
  "/data/tyf/RAG/app-update/${NEW12}/app-image.tar.gz" \
  "/data/tyf/RAG/app-update/${NEW12}/app-image.tar.gz.sha256" \
  /data/tyf/RAG/rag.env
```

只有明确需要用新代码镜像重启常驻 worker 时，追加：

```bash
bash "/data/tyf/RAG/app-update/${NEW12}/update-app.sh" \
  "/data/tyf/RAG/app-update/${NEW12}/app-image.tar.gz" \
  "/data/tyf/RAG/app-update/${NEW12}/app-image.tar.gz.sha256" \
  /data/tyf/RAG/rag.env \
  --restart-worker
```

更新脚本不会操作 OCR、Qdrant、DOCX、SQLite 或向量数据。新 app 若未在 60 秒内
通过 `/live`，脚本会原子恢复旧 env 并重新启动旧 app；不要手工删除旧镜像。

## 10. demo 边界

- demo 不是 production，只能用于内网效果验证。
- 日志中的 `DEMO_MODE_ACTIVE` 表示当前允许 provisional 参数做内网验证。
- demo 仍要求模型端点、Qdrant、活动 alias/manifest 和引用校验真实有效。
- `production_ready=false` 是预期结果；不得把 demo 截图或指标写成生产验收结论。
- 转生产必须回到独立的冻结、验收和运维流程，本指南不提供捷径。
