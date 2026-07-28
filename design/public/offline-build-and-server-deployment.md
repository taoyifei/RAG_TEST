# DOCX RAG 离线构建与服务器部署手册

本文给出由操作人员执行的完整流程：在 WSL 联网准备资产和构建
`linux/amd64` 镜像，生成 runtime/语料双包，分别校验后上传，在
`${RAG_SERVER}` 的 `/data/tyf/RAG` 下安装并完成启动、冒烟和回滚。
服务器不执行 build、pull、pip/apt/npm 安装，也不下载模型或字体。

V1 只处理 DOCX。图片 OCR 使用独立的单 GPU PaddleOCR 容器；EMF 不做
隐式转换，继续记录 `EMF_RASTERIZER_UNAVAILABLE`。当前检索参数仍是候选值，
冻结集验收完成前 `/ready` 必须保持严格，不能为了冒烟改成宽松成功。

## 1. 交付物和目录

一次发布产生以下文件，其中两个归档各有独立的外层摘要：

```text
artifacts/
├── offline_bundle.py
├── rag-runtime-<release-id>.tar.gz
├── rag-runtime-<release-id>.tar.gz.sha256
├── rag-corpus-<corpus-id>.tar.gz
└── rag-corpus-<corpus-id>.tar.gz.sha256
```

服务器目录固定如下。Docker 自身镜像层仍由服务器现有 Docker data-root
管理；项目文件、配置、语料、SQLite 和 Qdrant 数据全部位于本目录。

```text
/data/tyf/RAG/
├── incoming/                 # 上传和临时解包
├── releases/<release-id>/    # 只读发布内容
├── current -> releases/...   # 当前 release
├── shared/
│   ├── corpora/<corpus-id>/  # DOCX 和冻结评测集
│   └── env/                  # rag.env 和回滚记录
├── data/
│   ├── state/                # SQLite、任务和索引状态
│   └── qdrant/               # Qdrant 持久化数据
├── backups/                  # SQLite/Qdrant 备份登记
└── logs/                     # 人工验收的脱敏输出
```

其中语料版本目录的规范路径是
`shared/corpora/<corpus-id>/`，不得直接覆盖已经存在的 corpus ID。
持久化相对路径固定为 `data/state/` 与 `data/qdrant/`；备份和脱敏验收输出
分别只写入 `backups/` 与 `logs/`。

不得把 `.env`、回滚记录或持久化数据放进 release 目录。更新 release
不能覆盖语料或状态目录。

## 2. WSL 前置检查

在仓库根目录执行。三条 Docker 命令都必须成功，且当前分支需由操作人员
审核并提交；打包器会拒绝把未提交源码伪装成某个 Git revision。

```bash
export RAG_REPOSITORY='<WSL 中的源码仓库绝对路径>'
cd "${RAG_REPOSITORY}"
docker version
docker compose version
docker buildx version
python3.11 --version
git status --short
git rev-parse HEAD
```

先运行本地质量门：

```bash
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/ruff check .
.venv/bin/mypy --no-incremental src evaluation scripts
.venv/bin/python -m pytest -q
bash -n deployment/*.sh
docker compose --env-file deployment/.env.example \
  -f deployment/compose.yaml config -q
git diff --check
```

## 3. 联网准备固定资产

先拉取并检查三张固定 digest 基础镜像；不得换成浮动 tag：

```bash
python_base='python@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1'
ocr_base='paddlepaddle/paddle:3.3.0-gpu-cuda12.6-cudnn9.5@sha256:bb84347b6365c2980347cf784fc8be3eaa903472f5c40129cb65aaa634ebd776'
qdrant_ref='qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286'
docker pull "${python_base}"
docker pull "${ocr_base}"
docker pull "${qdrant_ref}"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' \
  "${python_base}" "${ocr_base}" "${qdrant_ref}"
```

三行平台都必须为 `linux/amd64`，镜像 ID 必须分别等于引用中的 digest。

应用 wheelhouse 必须由 Python 3.11 按 lock 重建。脚本只接受二进制
`linux/amd64` wheels，并检查项目 wheel 同时包含 worker 和 OCR 入口。

```bash
.venv/bin/python scripts/prepare_runtime_wheels.py
(cd deployment/runtime/wheelhouse && \
  sha256sum --check ../WHEELS.sha256)
sha256sum --check deployment/ASSETS.sha256
```

仅在 OCR 固定资产尚未装配或批准版本发生变化时执行以下下载。下载脚本按
来源清单、大小和 SHA256 校验，不从服务器下载：

```bash
.venv/bin/python scripts/download_ocr_assets.py
.venv/bin/python scripts/download_ocr_wheels.py
(cd deployment/ocr/assets && sha256sum --check MANIFEST.sha256)
(cd deployment/ocr/assets/wheelhouse && \
  sha256sum --check ../../WHEELS.sha256)
(cd deployment/ocr/assets && sha256sum --check ../MODELS.sha256)
```

任何模型文件变化都必须先更新来源、revision 和固定摘要并复核，不能只改
摘要求通过。

## 4. 构建和断网自检

将当前提交写入镜像的 OCI revision；两个自建镜像必须使用同一个 40 位
revision。Qdrant 使用已批准的 digest，不使用浮动 tag。

```bash
revision="$(git rev-parse HEAD)"
release_id="${revision:0:12}"
docker buildx build --network none --platform linux/amd64 --load \
  --build-arg "VCS_REF=${revision}" \
  --tag "docx-rag:${release_id}" .
docker buildx build --network none --platform linux/amd64 --load \
  --build-arg "VCS_REF=${revision}" \
  --file deployment/ocr/Dockerfile \
  --tag "docx-rag-ocr:${release_id}" .
```

构建后确认平台、revision，并在完全断网条件下做资源自检。OCR GPU 推理
不在 WSL 执行，留到服务器冒烟阶段。

```bash
docker image inspect \
  --format '{{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "docx-rag:${release_id}" "docx-rag-ocr:${release_id}"
docker run --rm --network none "docx-rag:${release_id}" asset-selfcheck
docker run --rm --network none --entrypoint python \
  "docx-rag-ocr:${release_id}" -c \
  'from rag_app.ocr.main import main; assert callable(main)'
```

预期两行平台均为 `linux/amd64`，revision 均等于 `git rev-parse HEAD`，
两条断网命令退出码均为 0。

## 5. 生成和校验双包

打包前工作树必须干净。分别保存三个镜像，禁止通过 glob 或归档内任意名称
批量加载镜像。

```bash
export RELEASE_ID="${release_id}"
export CORPUS_ID='frozen-docx-v1'
export RAG_APP_IMAGE="docx-rag:${release_id}"
export RAG_OCR_IMAGE="docx-rag-ocr:${release_id}"
export RAG_QDRANT_IMAGE="${qdrant_ref}"
bash deployment/package.sh
```

在 WSL 的全新临时目录验证两个外层摘要、tar 路径和内部逐文件清单：

```bash
verify_root="$(mktemp -d)"
.venv/bin/python artifacts/offline_bundle.py \
  "artifacts/rag-runtime-${release_id}.tar.gz" \
  "artifacts/rag-runtime-${release_id}.tar.gz.sha256" \
  "${verify_root}" --top-level runtime
.venv/bin/python artifacts/offline_bundle.py \
  "artifacts/rag-corpus-${CORPUS_ID}.tar.gz" \
  "artifacts/rag-corpus-${CORPUS_ID}.tar.gz.sha256" \
  "${verify_root}" --top-level corpus
bash "${verify_root}/runtime/verify-offline.sh"
(cd "${verify_root}/corpus" && sha256sum -c MANIFEST.sha256)
```

上述过程会拒绝错误外层 SHA、路径穿越、链接/设备成员、重复成员、额外文件和
任一内部文件摘要漂移。验证完成后可删除该临时目录。

## 6. 上传到服务器

以下命令由操作人员在 WSL 执行。只上传本次两个归档、两个 sidecar 和固定
解包器，不连接或改动其他服务器目录。

```bash
test -n "${RAG_SERVER}"
ssh "<server-user>@${RAG_SERVER}" \
  'install -d -m 0700 /data/tyf/RAG/incoming'
scp \
  artifacts/offline_bundle.py \
  "artifacts/rag-runtime-${release_id}.tar.gz" \
  "artifacts/rag-runtime-${release_id}.tar.gz.sha256" \
  "artifacts/rag-corpus-${CORPUS_ID}.tar.gz" \
  "artifacts/rag-corpus-${CORPUS_ID}.tar.gz.sha256" \
  "<server-user>@${RAG_SERVER}":/data/tyf/RAG/incoming/
```

## 7. 服务器校验和安装

登录服务器后执行。解包器会再次校验 sidecar 和内部 manifest，且目标存在时
拒绝覆盖。不要使用 `tar --overwrite`。

```bash
release_id='<前述 12 位 release-id>'
CORPUS_ID='frozen-docx-v1'
cd /data/tyf/RAG/incoming
sha256sum -c "rag-runtime-${release_id}.tar.gz.sha256"
sha256sum -c "rag-corpus-${CORPUS_ID}.tar.gz.sha256"
install -d -m 0700 extracted
python3 offline_bundle.py \
  "rag-runtime-${release_id}.tar.gz" \
  "rag-runtime-${release_id}.tar.gz.sha256" \
  extracted --top-level runtime
python3 offline_bundle.py \
  "rag-corpus-${CORPUS_ID}.tar.gz" \
  "rag-corpus-${CORPUS_ID}.tar.gz.sha256" \
  extracted --top-level corpus

test "$(cat extracted/runtime/RELEASE_ID)" = "${release_id}"
test "$(cat extracted/corpus/CORPUS_ID)" = "${CORPUS_ID}"
test ! -e "/data/tyf/RAG/releases/${release_id}"
test ! -e "/data/tyf/RAG/shared/corpora/${CORPUS_ID}"
install -d -m 0700 \
  /data/tyf/RAG/releases \
  /data/tyf/RAG/shared/corpora \
  /data/tyf/RAG/shared/env \
  /data/tyf/RAG/data/state \
  /data/tyf/RAG/data/qdrant \
  /data/tyf/RAG/backups \
  /data/tyf/RAG/logs
mv extracted/runtime "/data/tyf/RAG/releases/${release_id}"
mv extracted/corpus "/data/tyf/RAG/shared/corpora/${CORPUS_ID}"
sudo chown -R 10001:10001 \
  "/data/tyf/RAG/shared/corpora/${CORPUS_ID}" \
  /data/tyf/RAG/data/state \
  /data/tyf/RAG/logs
sudo find "/data/tyf/RAG/shared/corpora/${CORPUS_ID}" \
  -type d -exec chmod 0700 {} +
sudo find "/data/tyf/RAG/shared/corpora/${CORPUS_ID}" \
  -type f -exec chmod 0400 {} +
sudo chmod 0700 \
  /data/tyf/RAG/data/state \
  /data/tyf/RAG/data/qdrant \
  /data/tyf/RAG/logs
```

首次部署时创建外置配置；升级时复用并审查原文件，不从新 release 覆盖它：

```bash
install -m 0600 \
  "/data/tyf/RAG/releases/${release_id}/.env.example" \
  /data/tyf/RAG/shared/env/rag.env
editor /data/tyf/RAG/shared/env/rag.env
```

至少替换四个不同的随机令牌、三个模型端点数组、镜像 tag 和
`RAG_DOCS_PATH`。固定持久化路径应为：

```text
RAG_APP_IMAGE=docx-rag:<release-id>
RAG_OCR_IMAGE=docx-rag-ocr:<release-id>
RAG_QDRANT_IMAGE=rag-qdrant:<release-id>
RAG_STATE_PATH=/data/tyf/RAG/data/state
RAG_QDRANT_PATH=/data/tyf/RAG/data/qdrant
RAG_DOCS_PATH=/data/tyf/RAG/shared/corpora/frozen-docx-v1/docs
```

## 8. 启动与 GPU 冒烟

部署脚本按白名单依次 `docker load` 三个归档，复核平台和 revision，再以
`--no-build --pull never` 启动。它只创建或更新 `rag-*` 容器和网络。

```bash
bash "/data/tyf/RAG/releases/${release_id}/deploy.sh" \
  /data/tyf/RAG/shared/env/rag.env
```

检查容器、应用存活和 OCR GPU 就绪：

```bash
docker compose \
  --env-file /data/tyf/RAG/shared/env/rag.env \
  -f "/data/tyf/RAG/releases/${release_id}/compose.yaml" ps
curl -fsS http://127.0.0.1:8088/live
docker exec rag-ocr python -c \
  "import urllib.request; print(urllib.request.urlopen(
  'http://127.0.0.1:8090/ready', timeout=3).read().decode())"
docker exec rag-ocr python -c \
  "import paddle; print(paddle.device.get_device()); \
  print(paddle.device.cuda.device_count())"
```

OCR 必须报告 GPU 设备且 CUDA 设备数大于 0。随后用管理令牌创建一次全量
任务；幂等键应包含本次 release 或操作日期：

```bash
admin_token='<RAG_ADMIN_TOKEN>'
curl -fsS -X POST http://127.0.0.1:8088/api/index/jobs \
  -H "Authorization: Bearer ${admin_token}" \
  -H 'Content-Type: application/json' \
  --data "{\"idempotency_key\":\"initial-${release_id}\",\"kind\":\"full\"}"
```

轮询返回的 `job_id`：

```bash
curl -fsS \
  -H "Authorization: Bearer ${admin_token}" \
  "http://127.0.0.1:8088/api/index/jobs/<job-id>"
curl -sS -o /tmp/rag-ready.json -w '%{http_code}\n' \
  http://127.0.0.1:8088/ready
cat /tmp/rag-ready.json
```

索引激活、manifest 一致、冻结检索参数存在且全部模型端点满足健康策略前，
`/ready` 返回 503 是正确结果。全部满足后才应返回 200。服务器不得为了得到
200 修改阈值、manifest 或健康策略。

## 9. 参数冻结后的生产验收

上一节只证明基础设施可启动。先由人工冻结集确定检索参数，把
`retrieval.json` 的状态和摘要按评审结果冻结并重建索引；不得仅修改状态字段。
随后准备人工核过的 `frozen-results.jsonl`，不能让同一 LLM 自评。运行时评测
会从活动 alias、SQLite manifest 和 Qdrant 现场重新导出可信证据，不接收自由
构造的证据清单：

```bash
active_release="$(readlink -f /data/tyf/RAG/current)"
corpus_root="/data/tyf/RAG/shared/corpora/${CORPUS_ID}"
app_image="$(awk -F= '$1 == "RAG_APP_IMAGE" {print $2}' \
  /data/tyf/RAG/shared/env/rag.env)"
docker run --rm --network rag-internal \
  --env-file /data/tyf/RAG/shared/env/rag.env \
  --volume "${active_release}/evaluation/runtime:/opt/eval:ro" \
  --volume "${corpus_root}/evaluation:/opt/frozen:ro" \
  --volume /data/tyf/RAG/data/state:/state:ro \
  --volume /data/tyf/RAG/logs:/evidence \
  --env PYTHONPATH=/opt/eval \
  --entrypoint python "${app_image}" \
  /opt/eval/evaluation/evaluate.py \
  --dataset /opt/frozen/dataset.json \
  --results /evidence/frozen-results.jsonl \
  --qdrant-url http://rag-qdrant:6333 \
  --qdrant-alias rag-docx-active \
  --manifest-database /state/manifest.sqlite3 \
  --active-evidence-output /evidence/active-evidence.json
```

评测必须满足冻结门槛：Recall@20≥95%、rerank Recall@5≥90%、可答误拒≤10%、
不可答误答≤5%，引用 ID/原文匹配和提示注入防护均为 100%。

再执行 10 万 synthetic chunk Qdrant 基准。它使用独立随机 collection，
结束后按明确的 `--delete-after` 删除该基准 collection，不触碰活动 alias：

```bash
docker run --rm --network rag-internal \
  --env-file /data/tyf/RAG/shared/env/rag.env \
  --volume "${active_release}/evaluation/runtime:/opt/eval:ro" \
  --volume /data/tyf/RAG/logs:/evidence \
  --env PYTHONPATH=/opt/eval \
  --entrypoint sh "${app_image}" -c \
  'python /opt/eval/scripts/benchmark_qdrant.py \
  --url http://rag-qdrant:6333 --api-key "$RAG_QDRANT_API_KEY" \
  --count 100000 --queries 200 --output /evidence/qdrant-100k.json \
  --delete-after'
```

最后执行 5 并发 30 分钟 chat 验收。查询令牌只从容器环境展开，命令和日志
不写令牌、问题或答案：

```bash
docker run --rm --network rag-internal \
  --env-file /data/tyf/RAG/shared/env/rag.env \
  --volume "${active_release}/evaluation/runtime:/opt/eval:ro" \
  --volume "${corpus_root}/evaluation:/opt/frozen:ro" \
  --volume /data/tyf/RAG/data/state:/state:ro \
  --volume /data/tyf/RAG/logs:/evidence \
  --env PYTHONPATH=/opt/eval \
  --entrypoint sh "${app_image}" -c \
  'python /opt/eval/scripts/load_test_chat.py \
  --url http://rag-app:8088 --token "$RAG_QUERY_TOKEN" \
  --dataset /opt/frozen/dataset.json \
  --qdrant-url http://rag-qdrant:6333 \
  --qdrant-alias rag-docx-active \
  --manifest-database /state/manifest.sqlite3 \
  --concurrency 5 --duration-seconds 1800 \
  --output /evidence/chat-load.json'
```

验收要求错误率 `<1%`、召回加重排 p95≤2 秒、引用验证后的答案 p95≤60 秒，
并确认 10 分钟以上观察窗口内无 OOM、容器重启或 GPU 异常。三条命令任一
非零都不得标记生产就绪。

## 10. 备份

完整索引发布已创建并登记 Qdrant collection snapshot。版本升级前还应在停止
写入后备份 SQLite 与 Qdrant bind mount，避免把运行中的文件直接复制：

```bash
active_release="$(readlink -f /data/tyf/RAG/current)"
backup_id="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="/data/tyf/RAG/backups/${backup_id}"
install -d -m 0700 "${backup_dir}"
docker compose \
  --env-file /data/tyf/RAG/shared/env/rag.env \
  -f "${active_release}/compose.yaml" stop rag-worker rag-app rag-qdrant
tar --format=posix -C /data/tyf/RAG/data \
  -czf "${backup_dir}/state.tar.gz" state
tar --format=posix -C /data/tyf/RAG/data \
  -czf "${backup_dir}/qdrant.tar.gz" qdrant
(cd "${backup_dir}" && sha256sum state.tar.gz qdrant.tar.gz \
  > MANIFEST.sha256)
docker compose \
  --env-file /data/tyf/RAG/shared/env/rag.env \
  -f "${active_release}/compose.yaml" \
  up -d --no-build --pull never
```

确认两个归档非空、`sha256sum -c MANIFEST.sha256` 通过且服务恢复后，才能
开始下一版部署。恢复前应先停止三个写入相关容器，并把现有目录保留为另一份
可回退副本；不得覆盖唯一副本。

## 11. 回滚

非首次部署会在 `/data/tyf/RAG/shared/env/rollback-images.env` 原子记录上一版
release 和三个实际镜像 ID。确认该文件存在后执行：

```bash
bash "/data/tyf/RAG/releases/${release_id}/rollback.sh" \
  /data/tyf/RAG/shared/env/rag.env
readlink -f /data/tyf/RAG/current
curl -fsS http://127.0.0.1:8088/live
```

回滚只切换容器镜像和上一版 compose，不删除或覆盖 SQLite、Qdrant、语料。
如果新版本已执行不兼容的数据迁移或索引变更，应按对应 IndexManifest 记录的
Qdrant snapshot 恢复；不得通过删除 bind mount 目录实现回滚。

## 12. 停止条件

遇到以下任一情况立即停止当前依赖项，不继续部署：

- 外层 sidecar、内部 manifest、OCR 模型摘要或 frozen SHA 不一致；
- app/OCR 镜像不是 `linux/amd64`，或 OCI revision 不等于源码提交；
- 服务器需要 build、pull、安装软件或访问外网才能启动；
- `/data/tyf/RAG` 之外出现项目配置、语料或持久化数据；
- OCR 容器未识别 GPU、126 个媒体缺少成功或明确失败状态；
- 活动 alias、SQLite manifest、Qdrant pipeline metadata 不一致；
- 检索参数仍未冻结却试图放宽 `/ready`。

保留命令、退出码、SHA256、镜像 ID、job/trace ID 和阶段耗时作为交付证据；
不要记录原文、问题、答案或令牌。
