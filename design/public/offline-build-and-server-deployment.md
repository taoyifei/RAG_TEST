# DOCX RAG 离线构建与服务器部署手册

本文给出由操作人员执行的完整流程：在 WSL 联网准备资产和构建
`linux/amd64` 镜像，生成 runtime/语料双包，分别校验后上传，在
`${RAG_SERVER}` 的 `/data/tyf/RAG` 下安装并完成启动、冒烟和回滚。
服务器不执行 build、pull、pip/apt/npm 安装，也不下载模型或字体。

V1 只处理 DOCX。图片 OCR 使用独立的单 GPU PaddleOCR 容器；EMF 不做
隐式转换，继续记录 `EMF_RASTERIZER_UNAVAILABLE`。当前检索参数仍是候选值，
冻结集验收完成前 `/ready` 必须保持严格，不能为了冒烟改成宽松成功。

## 1. 交付物和目录

一次发布只原子产生一个 release 输出目录；两个归档各有独立外层摘要，
目录本身另有逐文件摘要：

```text
artifacts/releases/<release-id>-<corpus-id>/
├── RELEASE_MANIFEST.sha256
├── offline_bundle.py
├── offline_bundle.py.sha256
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

runtime wheel 准备器先在三件套的同一父目录内完整生成并复核新
`wheelhouse`、`WHEELS.sha256` 和 `PROJECT_WHEEL.json`，确认项目 wheel
内嵌 revision 与 clean Git HEAD 一致后才开始替换。替换期间旧三件套先原子
移入事务备份；下载、构建、清单写入或任一移动失败都会恢复旧三者，不会先删除
旧 wheel 或留下新旧混合状态。

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

应用 wheel 内的 `SOURCE_REVISION` 还会在 `serve`、`worker` 和一次性
`index` 创建 Qdrant、SQLite 或 HTTP 客户端之前，与
`RAG_RELEASE_REVISION` 做精确比较。`development-unset`、缺失、非 40 位
小写 Git SHA 或错配都会使进程立即失败；`build-info` 仍可用于只读诊断。

## 5. 生成和校验双包

先由操作员把当前 DOCX exact set 冻结为外置 canonical manifest。推荐输出到
已被 Git 忽略的 `corpus-manifests/`；私有文件名不进入仓库记录。打包前工作树
必须干净，且 manifest 与当前 docs 的路径、大小和 SHA256 必须完全一致。
分别保存三个镜像，禁止通过 glob 或归档内任意名称批量加载镜像。

```bash
export RELEASE_ID="${release_id}"
corpus_id='frozen-docx-v1'
mkdir -p corpus-manifests
.venv/bin/python -m scripts.freeze_corpus_manifest freeze \
  --docs docs \
  --corpus-id "${corpus_id}" \
  --output "corpus-manifests/${corpus_id}.json"
export CORPUS_MANIFEST="$(
  realpath "corpus-manifests/${corpus_id}.json"
)"
export RAG_APP_IMAGE="docx-rag:${release_id}"
export RAG_OCR_IMAGE="docx-rag-ocr:${release_id}"
export RAG_QDRANT_IMAGE="${qdrant_ref}"
bash deployment/package.sh
release_output="artifacts/releases/${release_id}-${corpus_id}"
(cd "${release_output}" && sha256sum -c RELEASE_MANIFEST.sha256)
```

在 WSL 的全新临时目录验证两个外层摘要、tar 路径和内部逐文件清单：

```bash
release_output="artifacts/releases/${release_id}-${corpus_id}"
(cd "${release_output}" && sha256sum -c offline_bundle.py.sha256)
verify_root="$(mktemp -d)"
.venv/bin/python "${release_output}/offline_bundle.py" \
  "${release_output}/rag-runtime-${release_id}.tar.gz" \
  "${release_output}/rag-runtime-${release_id}.tar.gz.sha256" \
  "${verify_root}" --top-level runtime
.venv/bin/python "${release_output}/offline_bundle.py" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz.sha256" \
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
release_output="artifacts/releases/${release_id}-${corpus_id}"
ssh "<server-user>@${RAG_SERVER}" \
  'install -d -m 0700 /data/tyf/RAG/incoming'
scp \
  "${release_output}/RELEASE_MANIFEST.sha256" \
  "${release_output}/offline_bundle.py" \
  "${release_output}/offline_bundle.py.sha256" \
  "${release_output}/rag-runtime-${release_id}.tar.gz" \
  "${release_output}/rag-runtime-${release_id}.tar.gz.sha256" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz" \
  "${release_output}/rag-corpus-${corpus_id}.tar.gz.sha256" \
  "<server-user>@${RAG_SERVER}":/data/tyf/RAG/incoming/
```

## 7. 服务器校验和安装

登录服务器后执行。解包器会再次校验 sidecar 和内部 manifest，且目标存在时
拒绝覆盖。不要使用 `tar --overwrite`。

```bash
release_id='<前述 12 位 release-id>'
corpus_id='frozen-docx-v1'
cd /data/tyf/RAG/incoming
sha256sum -c RELEASE_MANIFEST.sha256
sha256sum -c offline_bundle.py.sha256
sha256sum -c "rag-runtime-${release_id}.tar.gz.sha256"
sha256sum -c "rag-corpus-${corpus_id}.tar.gz.sha256"
install -d -m 0700 extracted
python3 offline_bundle.py \
  "rag-runtime-${release_id}.tar.gz" \
  "rag-runtime-${release_id}.tar.gz.sha256" \
  extracted --top-level runtime
python3 offline_bundle.py \
  "rag-corpus-${corpus_id}.tar.gz" \
  "rag-corpus-${corpus_id}.tar.gz.sha256" \
  extracted --top-level corpus

test "$(cat extracted/runtime/RELEASE_ID)" = "${release_id}"
test "$(cat extracted/corpus/CORPUS_ID)" = "${corpus_id}"
install -d -m 0700 \
  /data/tyf/RAG \
  /data/tyf/RAG/releases \
  /data/tyf/RAG/shared/corpora \
  /data/tyf/RAG/shared/env \
  /data/tyf/RAG/data/state \
  /data/tyf/RAG/data/qdrant \
  /data/tyf/RAG/backups \
  /data/tyf/RAG/logs
sudo bash extracted/runtime/install.sh \
  "$(realpath extracted/runtime)" \
  "$(realpath extracted/corpus)"
sudo chown -R 10001:10001 \
  /data/tyf/RAG/data/state \
  /data/tyf/RAG/logs
sudo chmod 0700 \
  /data/tyf/RAG/data/state \
  /data/tyf/RAG/data/qdrant \
  /data/tyf/RAG/logs
```

`install.sh` 必须由 root 执行。它在复制前后分别校验 runtime 的逐文件摘要和
corpus 的 `MANIFEST.sha256`、`CORPUS_MANIFEST.json`，将 corpus staging
递归设为 `10001:10001`、目录 0700、文件 0400，再以不覆盖的原子 rename
发布；不再需要安装后手工修 corpus owner。release 已存在时，只有
`SOURCE_REVISION`、`MANIFEST.sha256` 和完整文件集合均相同才只读复用；
corpus 同样只允许完全一致时复用。因此同一 runtime 可以安装新 corpus，
相同 runtime/corpus 可幂等重跑，任一漂移、复制后篡改或发布竞态都会非零退出，
且不会删除既有目标或留下伪完整 staging。

候选配置始终在 release 外。候选目录固定为 0700；首次部署从新 release
样例安装候选文件，升级则从 active `rag.env` 复制到新 release 的候选文件。
两种路径都只编辑候选文件：

```bash
install -d -m 0700 /data/tyf/RAG/shared/env/candidates

# 首次部署：
install -m 0600 \
  "/data/tyf/RAG/releases/${release_id}/.env.example" \
  /data/tyf/RAG/shared/env/candidates/${release_id}.env

# 升级：
test ! -e /data/tyf/RAG/shared/env/candidates/${release_id}.env
cp -- /data/tyf/RAG/shared/env/rag.env \
  /data/tyf/RAG/shared/env/candidates/${release_id}.env
chmod 0600 /data/tyf/RAG/shared/env/candidates/${release_id}.env

editor /data/tyf/RAG/shared/env/candidates/${release_id}.env
```

至少替换四个不同的随机令牌、三个模型端点数组、镜像 tag 和
`RAG_DOCS_PATH`。首次部署与升级命令二选一，不要覆盖既有候选文件。固定
持久化路径应为：

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
`--no-build --pull never` 启动。当前 provisional release 的默认路径只启动
app、OCR 和 Qdrant，不启动 worker；这时 `/ready=503` 是正确结果。

在第一条 `docker load` 前，脚本会把现场唯一分类为 fresh、installed 或
degraded。fresh 要求 active env、current、三个核心容器、worker 和 rollback
state 全不存在；installed 要求合法 active/current 与完整核心容器；degraded
要求合法 active/current、核心全无，并只允许不存在 worker 或保留 image 等于
旧 app image 的 worker。其他组合均为 invalid。fresh 若残留旧 rollback state
也会失败；installed/degraded 的旧 release 会在部署前和失败补偿前重新执行
`verify-offline.sh`。

```bash
bash "/data/tyf/RAG/releases/${release_id}/deploy.sh" \
  /data/tyf/RAG/shared/env/candidates/${release_id}.env
```

deploy.sh 只接受候选文件；active rag.env 只能由 deploy.sh 成功后发布。
后续 Compose、备份和 rollback 继续读取固定的
`/data/tyf/RAG/shared/env/rag.env`。

Compose 的 Qdrant 端口 healthcheck 只用于启动顺序。deploy、rollback 和失败
补偿还会在 `rag-app` 容器内，以容器环境中的 Qdrant URL/API key 有界请求
`/readyz`；只有 HTTP 200 才允许提交 env/current。命令、日志和错误不输出
API key 或响应正文，Qdrant 也不发布宿主端口。

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
任务前，必须先完成检索参数冻结和模型 revision 核验，并在包含冻结配置的新
release 上显式启动 `index` profile。禁止在服务器上直接修改 provisional
配置或绕过 worker 的严格索引门禁：

```bash
docker compose --profile index \
  --env-file /data/tyf/RAG/shared/env/rag.env \
  -f "/data/tyf/RAG/releases/${release_id}/compose.yaml" \
  up -d --no-build --pull never rag-worker
```

worker 稳定运行后才能创建全量任务；幂等键应包含本次 release 或操作日期：

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

完整索引发布已创建并登记 Qdrant collection snapshot。版本升级前还必须使用
release 内固定的 `backup.sh` 备份 SQLite 与 Qdrant bind mount。禁止再使用
手工 `tar -czf` 流程：它无法可靠保证权限提升后的文件所有权、归档完整性和
失败后的原服务集合恢复。

```bash
active_release="$(readlink -f /data/tyf/RAG/current)"
backup_id="$(date -u +%Y%m%dT%H%M%SZ)"
bash "${active_release}/backup.sh" \
  "${backup_id}" \
  /data/tyf/RAG/shared/env/rag.env
backup_dir="/data/tyf/RAG/backups/${backup_id}"
(cd "${backup_dir}" && sha256sum -c MANIFEST.sha256)
stat -c '%U:%G %a %n' \
  "${backup_dir}/state.tar.gz" \
  "${backup_dir}/qdrant.tar.gz" \
  "${backup_dir}/MANIFEST.sha256"
```

脚本记录 app、worker、Qdrant 备份前的真实运行状态，停止并确认写入服务后，
通过 `sudo tar | gzip` 只提升源数据读取权限；最终文件归原调用用户所有且权限
为 0600。两个归档必须非空、gzip/tar 可读、成员路径和类型安全，并通过
`MANIFEST.sha256` 后才原子发布。成功或失败都会尝试恢复原运行集合；原来未
运行的 worker 不会被启动。恢复失败时脚本非零退出，但不会删除已经验证成功的
备份，也不会删除任何历史备份。

每个正式备份还包含进入 `MANIFEST.sha256` 的
`BACKUP_METADATA.json`：记录 UTC 创建时间、release ID、源码 revision、
app/OCR/Qdrant 实际 image ID、外置 active env 的 SHA256（不含 env 内容）、
两个归档 SHA；活动 manifest SQLite 可安全只读时还记录唯一活动 collection
和 manifest SHA，否则该字段明确为 `null`。app 恢复与 Qdrant 一样使用固定
30 次上限的有界轮询，不以单次 `/live` 请求判定恢复失败。

本轮 Agent 只实现并使用 fake 命令测试 `backup.sh`，没有在服务器执行备份、
恢复或回滚。操作人员实际部署后必须运行上述命令并保存退出码、manifest 校验、
所有权、权限和服务恢复证据，才能开始下一版部署。

## 11. 回滚

非首次部署会在 `/data/tyf/RAG/shared/env/rollback-images.env` 原子记录上一版
release 和三个实际镜像 ID。回滚脚本会重新执行旧 release 的
`verify-offline.sh`，校验 Compose、三个本地镜像、app/OCR OCI revision 和
Qdrant 固定身份；任一预检失败都不会修改共享 env、`current` 或回滚记录。
确认该文件存在后执行：

```bash
bash "/data/tyf/RAG/releases/${release_id}/rollback.sh" \
  /data/tyf/RAG/shared/env/rag.env
readlink -f /data/tyf/RAG/current
curl -fsS http://127.0.0.1:8088/live
```

成功后共享 env 中的 `RAG_APP_IMAGE`、`RAG_OCR_IMAGE`、
`RAG_QDRANT_IMAGE` 和已有的 `RAG_RELEASE_REVISION` 会持久保存旧 release
选择；后续普通 Compose up/restart 不会切回新镜像。回滚前实际运行的 worker
会通过 `index` profile 使用旧 app 镜像恢复，原来未运行的 worker 不会被新增。
env 与 `current` 任一提交或最终复核失败时，脚本会补偿恢复原元数据。

回滚不删除或覆盖 SQLite、Qdrant、语料。
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
