# `.60` 服务器 DOCX RAG 从零部署指南

目标服务器固定为 `user4a@10.242.180.60`，初始状态仅有可写空目录
`/data/tyf/RAG/`。本文从本地上传开始，部署 OCR、Embedding、Reranker、Qdrant、
RAG App，并调用 `.57/.58` 已运行的四个 `Qwen3-8B-AWQ` LLM。

实际拓扑：

- `.60` GPU 0：OCR；GPU 1：Embedding；GPU 2：Reranker。
- `.57:8000`、`.57:8001`、`.58:8000`、`.58:8001`：四个 LLM。
- `.60:8088`：RAG 前端和 API；`.60:8091/8092`：模型服务。

> `.60` 必须已有 Docker、Docker Compose plugin、NVIDIA 驱动和 NVIDIA
> Container Toolkit，并至少有 3 张可用 GPU、80 GiB 可用空间。
> 本流程使用 demo；demo 不是 production。

## 1. 本地确认上传源

所有本地命令都在 WSL Ubuntu 中执行：

```bash
cd /home/jerry/work/RAG
SERVER='user4a@10.242.180.60'
REV12='REPLACE_SHORT_GIT_SHA'
SIMPLE="/home/jerry/work/RAG/artifacts/simple-deploy/${REV12}"
MODEL_DIR='/home/jerry/work/RAG/artifacts/model-services/qwen3-embedding-reranker-0.6b-v1'
MODEL_BUNDLE="${MODEL_DIR}/rag-model-assets-qwen3-embedding-reranker-0.6b-v1.tar.gz"
test -d "${SIMPLE}" && test -f "${MODEL_BUNDLE}"
(cd "${SIMPLE}" && sha256sum -c -- *.sha256)
(cd "${MODEL_DIR}" && sha256sum -c -- "$(basename "${MODEL_BUNDLE}").sha256")
```

`SIMPLE` 包含 app、OCR、Qdrant、6 份 DOCX 和部署脚本；模型资产包包含
Embedding/Reranker 权重及两张服务镜像。LLM 已在 `.57/.58`，不要重复上传。

## 2. `.60` 基础设施和四个 LLM 预检

先从本地登录：

```bash
ssh user4a@10.242.180.60
```

确认终端提示符已经属于 `.60` 后，把下面整段直接粘贴到 `.60` 执行：

```bash
set -euo pipefail
ROOT=/data/tyf/RAG
test "$(uname -m)" = x86_64 && echo 'ARCH_OK=x86_64'
test -d "${ROOT}" && test -w "${ROOT}" && echo "ROOT_OK=${ROOT}"
docker version >/dev/null && echo 'DOCKER_OK'
docker compose version >/dev/null && echo 'COMPOSE_OK'
command -v nvidia-smi >/dev/null
gpu_count=$(nvidia-smi --query-gpu=index --format=csv,noheader,nounits | wc -l)
test "${gpu_count}" -ge 3 && echo "GPU_COUNT_OK=${gpu_count}"
free_gib=$(df --output=avail -BG "${ROOT}" | tail -1 | tr -dc 0-9)
test "${free_gib}" -ge 80 && echo "DISK_FREE_GIB_OK=${free_gib}"
for port in 8088 8091 8092; do
  ! ss -lnt | grep -Eq ":${port}[[:space:]]"
  echo "PORT_FREE=${port}"
done
endpoints=(
  http://10.242.180.57:8000 http://10.242.180.57:8001
  http://10.242.180.58:8000 http://10.242.180.58:8001
)
payload='{"model":"Qwen/Qwen3-8B-AWQ","messages":[{"role":"user","content":"只回复 ok"}],"max_tokens":8,"temperature":0}'
for endpoint in "${endpoints[@]}"; do
  models=$(curl -fsS --max-time 10 "${endpoint}/v1/models")
  grep -Fq 'Qwen/Qwen3-8B-AWQ' <<<"${models}"
  echo "LLM_MODELS_OK=${endpoint}"
  chat=$(curl -fsS --max-time 90 -H 'Content-Type: application/json' \
    -d "${payload}" "${endpoint}/v1/chat/completions")
  grep -Fq '"choices"' <<<"${chat}"
  echo "LLM_CHAT_OK=${endpoint}"
done
echo 'RAG_SERVER_PREFLIGHT_OK'
```

正确结果必须包含以下内容；GPU 数量和磁盘数值按实际结果变化，但不得小于 3 和 80：

```text
ARCH_OK=x86_64
ROOT_OK=/data/tyf/RAG
DOCKER_OK
COMPOSE_OK
GPU_COUNT_OK=<至少3>
DISK_FREE_GIB_OK=<至少80>
PORT_FREE=8088
PORT_FREE=8091
PORT_FREE=8092
LLM_MODELS_OK=http://10.242.180.57:8000
LLM_CHAT_OK=http://10.242.180.57:8000
LLM_MODELS_OK=http://10.242.180.57:8001
LLM_CHAT_OK=http://10.242.180.57:8001
LLM_MODELS_OK=http://10.242.180.58:8000
LLM_CHAT_OK=http://10.242.180.58:8000
LLM_MODELS_OK=http://10.242.180.58:8001
LLM_CHAT_OK=http://10.242.180.58:8001
RAG_SERVER_PREFLIGHT_OK
```

没有看到最后一行就不要上传。RAG 配置填写 origin，不能带 `/v1`。

若缺少 `ROOT_OK`，让管理员或有 sudo 权限的账号在 `.60` 执行：

```bash
sudo chown user4a:"$(id -gn user4a)" /data/tyf/RAG && sudo chmod 0750 /data/tyf/RAG
test -w /data/tyf/RAG && echo 'RAG_ROOT_PERMISSION_OK'
```
正确返回必须是 `RAG_ROOT_PERMISSION_OK`；否则不要继续。

## 3. 服务器创建目录，然后从本地上传

仍在 `.60` 时直接执行：

```bash
set -euo pipefail
umask 027
mkdir -p /data/tyf/RAG/{simple,uploads,model-services,shared/env,shared/model-services}
test -w /data/tyf/RAG/shared/env
echo UPLOAD_DIRS_OK
```

正确返回 `UPLOAD_DIRS_OK` 后执行 `exit` 回到本地 WSL，再执行上传：

```bash
rsync -av --partial --info=progress2 "${SIMPLE}/" \
  "${SERVER}:/data/tyf/RAG/simple/${REV12}/"
rsync -av --partial --info=progress2 "${MODEL_BUNDLE}" "${MODEL_BUNDLE}.sha256" \
  "${SERVER}:/data/tyf/RAG/uploads/"
rsync -av deployment/model-services/{compose.yaml,preflight.sh,.env.example} \
  "${SERVER}:/data/tyf/RAG/model-services/"
```
传输量约 21.8 GB；中断后重跑 rsync 会续传。没有 rsync 时可用 `scp -r`。

## 4. 服务器校验、解包并加载模型镜像

上传完成后，从本地执行 `ssh user4a@10.242.180.60`。登录成功后，把下面整段
直接粘贴到 `.60`：

```bash
set -euo pipefail
ROOT=/data/tyf/RAG
REV12=REPLACE_SHORT_GIT_SHA
cd "${ROOT}/simple/${REV12}"
sha256sum -c -- *.sha256
cd "${ROOT}/uploads"
sha256sum -c rag-model-assets-qwen3-embedding-reranker-0.6b-v1.tar.gz.sha256
tar -xzf rag-model-assets-qwen3-embedding-reranker-0.6b-v1.tar.gz \
  -C "${ROOT}/shared/model-services"
ASSET_ROOT="${ROOT}/shared/model-services/qwen3-embedding-reranker-0.6b-v1"
docker load -i "${ASSET_ROOT}/images/ghcr.m.daocloud.io_huggingface_text-embeddings-inference_1.9.tar"
docker load -i "${ASSET_ROOT}/images/covlink-rerank-api_server.tar"
docker image inspect \
  ghcr.m.daocloud.io/huggingface/text-embeddings-inference:1.9 \
  covlink-rerank-api:server >/dev/null
echo MODEL_IMAGES_LOAD_OK
```

四个简单包和模型资产必须各自显示 `OK`；两次 `docker load` 必须显示
`Loaded image` 或 `Loaded image ID`；最后必须显示 `MODEL_IMAGES_LOAD_OK`。

## 5. 在 `.60` 启动 Embedding 和 Reranker

以下代码仍然直接在 `.60` 执行：

```bash
set -euo pipefail
ROOT=/data/tyf/RAG
cat > "${ROOT}/shared/env/model-services.env" <<'EOF'
RAG_MODEL_ASSET_ROOT=/data/tyf/RAG/shared/model-services/qwen3-embedding-reranker-0.6b-v1
RAG_MODEL_BIND_ADDRESS=0.0.0.0
RAG_EMBEDDING_PORT=8091
RAG_RERANKER_PORT=8092
RAG_EMBEDDING_GPU_DEVICE_ID=1
RAG_RERANKER_GPU_DEVICE_ID=2
EOF
chmod 600 "${ROOT}/shared/env/model-services.env"
bash "${ROOT}/model-services/preflight.sh" \
  "${ROOT}/shared/env/model-services.env"
docker compose --env-file "${ROOT}/shared/env/model-services.env" \
  -f "${ROOT}/model-services/compose.yaml" \
  up -d --no-build --pull never --wait --wait-timeout 300
curl -fsS http://127.0.0.1:8091/health
curl -fsS http://127.0.0.1:8091/info
curl -fsS http://127.0.0.1:8092/health
```

预检必须输出 `RAG_MODEL_SERVICES_PREFLIGHT_OK`。

## 6. 创建当前版本的 rag.env

以下代码直接在 `.60` 执行：

```bash
set -euo pipefail
ROOT=/data/tyf/RAG
REV12=REPLACE_SHORT_GIT_SHA
ENV_FILE="${ROOT}/rag.env"
cp "${ROOT}/simple/${REV12}/.env.example" "${ENV_FILE}"
QUERY_TOKEN=$(openssl rand -hex 32)
ADMIN_TOKEN=$(openssl rand -hex 32)
QDRANT_KEY=$(openssl rand -hex 32)
OCR_TOKEN=$(openssl rand -hex 32)
sed -i \
  -e "s/REPLACE_WITH_RANDOM_QUERY_TOKEN/${QUERY_TOKEN}/" \
  -e "s/REPLACE_WITH_RANDOM_ADMIN_TOKEN/${ADMIN_TOKEN}/" \
  -e "s/REPLACE_WITH_RANDOM_QDRANT_API_KEY/${QDRANT_KEY}/" \
  -e "s/REPLACE_WITH_RANDOM_OCR_API_TOKEN/${OCR_TOKEN}/" \
  -e 's|^RAG_EMBEDDING_ENDPOINTS=.*|RAG_EMBEDDING_ENDPOINTS='\''["http://10.242.180.60:8091"]'\''|' \
  -e 's|^RAG_RERANKER_ENDPOINTS=.*|RAG_RERANKER_ENDPOINTS='\''["http://10.242.180.60:8092"]'\''|' \
  -e 's|^RAG_LLM_ENDPOINTS=.*|RAG_LLM_ENDPOINTS='\''["http://10.242.180.57:8000","http://10.242.180.57:8001","http://10.242.180.58:8000","http://10.242.180.58:8001"]'\''|' \
  "${ENV_FILE}"
unset QUERY_TOKEN ADMIN_TOKEN QDRANT_KEY OCR_TOKEN
chmod 600 "${ENV_FILE}"
! grep -n 'REPLACE_' "${ENV_FILE}"
grep -E '^RAG_(APP_IMAGE|RELEASE_REVISION|EMBEDDING_ENDPOINTS|RERANKER_ENDPOINTS|LLM_ENDPOINTS)=' "${ENV_FILE}"
```

四个 LLM 地址必须保持上述顺序且不含 `/v1`；当前服务不配置 API token。

## 7. 启动 RAG、导入文档并建立索引

以下代码直接在 `.60` 执行：

```bash
set -euo pipefail
ROOT=/data/tyf/RAG
REV12=REPLACE_SHORT_GIT_SHA
ENV_FILE="${ROOT}/rag.env"
cd "${ROOT}/simple/${REV12}"
bash deploy.sh "${ENV_FILE}" "${ROOT}/simple/${REV12}"
```

脚本会加载 app/OCR/Qdrant 镜像、创建数据目录、解压 6 份 DOCX、启动三个常驻
容器，并用一次性 worker 执行 `index full`。不要另开第二个 worker。

## 8. 验证状态和实际问答

以下代码直接在 `.60` 执行：

```bash
set -euo pipefail
ROOT=/data/tyf/RAG
ENV_FILE="${ROOT}/rag.env"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -fsS http://127.0.0.1:8088/live
curl -fsS http://127.0.0.1:8088/ready
QUERY_TOKEN=$(awk -F= '$1=="RAG_QUERY_TOKEN"{print substr($0,index($0,"=")+1)}' "${ENV_FILE}")
curl -N -H "Authorization: Bearer ${QUERY_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{"conversation_id":"demo-001","question":"请概括文档中的主要要求，并给出引用。"}' \
  http://127.0.0.1:8088/api/chat
```

浏览器打开 `http://10.242.180.60:8088/`。`/ready` 必须包含
`{"ready":true,"run_mode":"demo","production_ready":false}`。

```bash
docker logs --tail 300 rag-app
docker logs --tail 300 rag-ocr
docker logs --tail 300 rag-qdrant
docker logs --tail 300 rag-embedding
docker logs --tail 300 rag-reranker
nvidia-smi
```

## 9. 重建索引与后续 app 更新

重建索引时直接在 `.60` 执行：

```bash
set -euo pipefail
ROOT=/data/tyf/RAG
REV12=REPLACE_SHORT_GIT_SHA
ENV_FILE="${ROOT}/rag.env"
docker compose --env-file "${ENV_FILE}" \
  -f "${ROOT}/simple/${REV12}/compose.yaml" \
  --profile index run --rm --no-deps rag-worker \
  index full --idempotency-key "manual-full-$(date +%Y%m%d%H%M%S)"
```

后续先在本地运行 `scripts/build_app_update.py` 并上传输出目录；服务器执行
`bash /data/tyf/RAG/app-update/NEW12/update-app.sh /data/tyf/RAG/app-update/NEW12/app-image.tar.gz /data/tyf/RAG/app-update/NEW12/app-image.tar.gz.sha256 /data/tyf/RAG/rag.env`。

仅需同时重启 worker 时追加 `--restart-worker`。完整包重建入口仍为
`scripts/build_simple_bundle.py`。更新脚本不会修改 OCR、Embedding、Reranker、
Qdrant、DOCX、SQLite 或向量数据。
