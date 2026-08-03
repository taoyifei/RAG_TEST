# DOCX RAG 离线部署

本部署只创建或更新 `rag-app`、`rag-worker`、`rag-ocr`、`rag-qdrant` 及
对应 `rag-*` 网络。只有 `rag-app` 发布宿主机 8088；OCR 与 Qdrant
只在内部网络可达。服务器脚本不 build、不 pull、不安装软件、不访问外网，
也不删除 `/data/tyf/RAG` 下的 bind mount 数据。

## 访问范围

V1 必须显式配置 `RAG_ACCESS_MODE=shared_corpus`。持有 query token 的所有
用户都能检索语料中全部 `active`/`official` 文档；系统没有用户级、租户级或
文档级授权。`permissioned` 未实现，配置缺失或填写任何其他值都会使应用在
启动配置校验阶段失败，不能把 query token 当作文档权限凭据。

## 发布身份约定

`revision` 始终表示完整 40 位小写 Git SHA；打包端推荐从已提交且干净的
源码树生成默认身份：

```bash
revision="$(git rev-parse HEAD)"
release_id="${revision:0:12}"
```

runtime 会分别保存 `SOURCE_REVISION` 和 `RELEASE_ID`。`package.sh` 在没有
显式设置 `RELEASE_ID` 时，默认把 `revision` 前 12 位写入
runtime `RELEASE_ID`；服务器必须以该文件为 `release_id` 的权威来源。
`release_id` 统一用于 release 目录、镜像 tag、归档名和 candidate env
文件名；候选文件中的 `RAG_RELEASE_REVISION` 使用完整 `revision`，不得填
`release_id`。

## 服务器步骤

1. 每个交付固定使用
   `/data/tyf/RAG/incoming/<release-id>-<corpus-id>/`，不得把不同版本平铺到
   `incoming/`，也不得使用共享的 `/incoming/extracted`。上传内容恰好七个
   文件：两个归档、三个 `.sha256` sidecar、`offline_bundle.py` 和
   `RELEASE_MANIFEST.sha256`。服务器 root 先只为本次交付创建
   `user4a:0700` 目录，并仅在上传窗口内把 `RAG/` 与 `incoming/` 设为
   `root:<user4a-primary-group>/0710` 供该用户穿越；WSL 上传结束后，root 立即
   将父目录和本次 delivery 收回为 `root:root/0700`，文件设为 0600。禁止对
   `/data` 或任一父目录执行 `chmod 777`。Embedding/Reranker 共享模型资产
   不属于这七个文件；服务器现有 `shared/model-services` 已通过独立清单、revision、
   服务健康和模型契约校验时直接复用，不重传、不重复解包或加载。fresh 服务器或
   任一校验不满足时，必须先完成独立模型服务部署，不能靠本 release 补齐。
2. 服务器必须使用 Docker Engine 29 和 containerd image store；
   `docker info --format '{{json .DriverStatus}}'` 必须包含
   `["driver-type","io.containerd.snapshotter.v1"]`。六列
   `IMAGE_ARCHIVES.tsv` 依次记录归档、tag、platform manifest digest、
   provenance、config digest 和 `linux/amd64`。加载后必须满足
   `.Id == .Descriptor.digest == 第三列`；不得再比较保存前 daemon 的本地 ID。
3. root 在唯一 delivery 内校验 `RELEASE_MANIFEST.sha256`、解包器 sidecar 和
   两个归档 sidecar，再原子解到该 delivery 自己的 `extracted/runtime` 与
   `extracted/corpus`。禁止覆盖或复用部分解包目录。随后只用绝对路径调用实际
   两参数安装器：

   ```bash
   (
   set -euo pipefail

   test "$(id -u)" -eq 0
   release_id='<本次 12 位 release-id>'
   corpus_id='<本次 corpus-id>'
   delivery="/data/tyf/RAG/incoming/${release_id}-${corpus_id}"
   runtime_dir="${delivery}/extracted/runtime"
   corpus_dir="${delivery}/extracted/corpus"

   test "$(cat "${runtime_dir}/RELEASE_ID")" = "${release_id}"
   test "$(cat "${corpus_dir}/CORPUS_ID")" = "${corpus_id}"
   bash "${runtime_dir}/verify-offline.sh"
   (
     cd "${corpus_dir}"
     sha256sum -c MANIFEST.sha256
   )
   bash "${runtime_dir}/install.sh" \
     "$(realpath -e "${runtime_dir}")" \
     "$(realpath -e "${corpus_dir}")"
   )
   ```

   创建父目录、七文件收权、原子解包和完整安装的唯一可复制命令见
   `design/public/offline-build-and-server-deployment.md` 第 6、7 节；不要从本摘要
   拼接零散命令。
4. 候选配置始终在 release 外。首次部署从 `.env.example` 创建，升级从 active
   `rag.env` 复制；两种情况都必须先执行
   `test ! -e /data/tyf/RAG/shared/env/candidates/<release-id>.env`，禁止覆盖既有
   candidate。只编辑新 candidate，设置四个互不相同且至少 32 字符的令牌、经实测的模型端点、三个 bind
   mount 路径、`RAG_RELEASE_REVISION`、`RAG_ACCESS_MODE=shared_corpus`、
   普通查询 `RAG_TRACE_MODE` 和 OCR 使用的宿主 GPU ID；其中
   `RAG_RELEASE_REVISION` 必须等于 runtime 的完整 `SOURCE_REVISION`，
   `RAG_DOCS_PATH` 必须指向本次 corpus。模型端点应复用上述已验证服务，不得仅为
   新 release 重传 8 GB 级共享模型包。文档集合变化时必须使用新的 corpus ID。
5. 只把 candidate 传给 deploy；active `rag.env` 只能由 `deploy.sh` 成功后发布：

   ```bash
   (
   set -euo pipefail

   test "$(id -u)" -eq 0
   release_id='<本次 12 位 release-id>'
   release_dir="/data/tyf/RAG/releases/${release_id}"
   candidate="/data/tyf/RAG/shared/env/candidates/${release_id}.env"

   test -f "${candidate}"
   test ! -L "${candidate}"
   test "$(stat -c '%a' "${candidate}")" = 600
   bash "${release_dir}/deploy.sh" "${candidate}"
   )
   ```

   脚本先校验 Docker 29/containerd 与六列镜像身份，再按白名单执行
   `docker load --platform linux/amd64`，最后只启动 app、OCR 和 Qdrant。
   默认 Compose 路径不启动 worker。部署、回滚与失败补偿均按 deadline 等待：
   Qdrant 端口 health、容器内 Qdrant `/readyz`、app health 和 app
   `/live` 各最多 60 秒，OCR 最多 240 秒；OCR 的期限覆盖 90 秒 start
   period 与后续 12×10 秒健康重试窗口。`/readyz` 由 `rag-app` 容器内
   Python 使用容器环境中的 URL 和 API key 请求，不发布 Qdrant 宿主端口。

   脚本在 `docker load` 前严格分类部署状态：全部发布元数据、核心容器、
   worker 和 rollback state 都不存在才是 fresh；合法 active/current 配合
   完整核心容器是 installed；合法 active/current 配合全无核心、可选同旧 app
   image 的 worker 是 degraded。其余组合一律拒绝。fresh 不允许遗留 rollback
   state；installed/degraded 的旧 release 会在部署前和失败补偿前重新执行
   `verify-offline.sh`。
6. 检查 Compose、应用存活和容器内 OCR readiness：

   ```bash
   (
   set -euo pipefail

   docker compose \
     --env-file /data/tyf/RAG/shared/env/rag.env \
     -f /data/tyf/RAG/current/compose.yaml ps
   curl -fsS http://127.0.0.1:8088/live
   docker exec rag-ocr python -c \
     "import urllib.request; print(urllib.request.urlopen(
     'http://127.0.0.1:8090/ready').read().decode())"
   )
   ```
   安装后的模型验证器固定为
   `/data/tyf/RAG/current/evaluation/runtime/scripts/verify_model_contracts.py`。
   必须从当前 app image 执行，使用 `rag-egress`，把
   `/data/tyf/RAG/current/evaluation/runtime` 只读挂载到
   `/contract-runtime`；令牌只通过 active `rag.env` 的 `--env-file` 和
   验证器的 `--token-env` 变量名传入。embedding、reranker 和四个 LLM
   端点分别执行，LLM 固定读取
   `/app/deployment/config/retrieval.json` 与
   `/app/deployment/assets/tokenizers/llm/tokenizer.json`。完整命令模板见
   current `README.md` 的“启动与 GPU 冒烟”；每次尝试在
   `/data/tyf/RAG/logs/model-contract-<release-id>.<unique>/` 使用唯一目录，六份
   脱敏报告的汇总只读取该精确目录，不得复用旧 passed 报告，也不得输出令牌、
   探测问题或完整模型响应。

   ### 冒烟成功标准

   - 默认路径只启动 `rag-app`、`rag-ocr` 和 `rag-qdrant`。
   - `rag-worker` 必须不存在或保持停止。
   - 应用 `/live` 必须返回 HTTP 200。
   - Qdrant `/readyz` 必须返回 HTTP 200。
   - OCR `/ready` 必须返回 HTTP 200，且 CUDA device count 必须大于 0。
   - provisional 阶段 `/ready` 返回 HTTP 503 才是成功预期。
   - 六份模型契约报告全部 `status=passed` 前，不得冻结检索参数或启动 `rag-worker`。
     六份分别对应 embedding、reranker 和四个 LLM。

7. 当前 provisional 配置下 `/ready=503` 是正确结果，不得绕过 worker 的严格
   索引门禁。完成检索参数冻结和模型 revision 核验、生成对应 release 后，显式
   启动单索引 worker，再通过管理 API 创建任务：

   ```bash
   (
   set -euo pipefail

   docker compose --profile index \
     --env-file /data/tyf/RAG/shared/env/rag.env \
     -f /data/tyf/RAG/current/compose.yaml \
     up -d --no-build --pull never rag-worker
   )
   ```

`/ready` 只有在活动索引与 manifest 一致且全部模型健康时才返回 200。

升级或回滚前使用当前 release 内的可靠备份脚本；不要手工对运行中的 bind
mount 执行 `tar -czf`：

```bash
(
set -euo pipefail

bash /data/tyf/RAG/current/backup.sh \
  "$(date -u +%Y%m%dT%H%M%SZ)" \
  /data/tyf/RAG/shared/env/rag.env
)
```

脚本只读取固定的 `data/state` 和 `data/qdrant`，验证归档与 SHA 后原子发布，
并恢复备份前实际运行的 app、worker、Qdrant 集合。

应用把 Query Trace 单独写入 `/state/traces.sqlite3`；它随现有 state bind
mount 持久化，但不与任务或 manifest 表共库。管理员通过 `/debug/` 和
`/api/admin/traces*` 查询，FULL Debug 仅走 admin token。Trace Store 不加入
RAG readiness：普通查询捕获失败继续回答，显式 FULL Debug 则在执行前返回
503。详细内容边界与 72 小时/30 天保留策略见
`design/public/trace-observability.md`。

执行下面整块可切回部署前记录的版本：

```bash
(
set -euo pipefail

test "$(id -u)" -eq 0
project_root='/data/tyf/RAG'
active_env="${project_root}/shared/env/rag.env"
test -f "${project_root}/current/rollback.sh"
test -f "${active_env}"
test ! -L "${active_env}"
bash /data/tyf/RAG/current/rollback.sh \
  /data/tyf/RAG/shared/env/rag.env
)
```

回滚脚本使用部署前记录的
应用、OCR 和 Qdrant 镜像 ID。脚本先重验旧 release、Compose、镜像 digest
与 OCI revision，再按回滚前实际状态决定是否恢复 worker；全部存活与镜像
检查通过后，才原子持久化共享 env 和 `current`，提交失败会恢复原元数据。
SQLite/Qdrant bind mount 不会被删除。索引数据恢复仍以应用 manifest 中记录的
Qdrant snapshot 为准，不能通过删除数据目录回滚。

过期 DOCX 只接受数据负责人给出的 `docs/` 下精确相对路径；打包前先移动到
Git 已忽略的 `artifacts/docx-quarantine/<id>/`，文件集合变化后必须使用新的
corpus ID。只有新 release 健康且 `current` 精确指向它后，才能永久删除该精确
quarantine。不得根据当前六个文件的名称或日期猜测过期项。

旧 release、incoming、corpus 和镜像同样只能在新版本健康后清理。必须保护
active release/env/corpus，以及 `rollback-images.env` 记录的上一版 release、
三个 image ID 和旧 env 引用的 corpus；禁止 `docker image prune`，禁止删除
`data/state`、`data/qdrant`、`backups`、`logs` 或
`shared/model-services`。当前无效 c2 资产的精确受保护清理命令见完整手册
“过期 DOCX 与旧失败发布清理”。

资产下载、断网构建、双包、GPU 冒烟到回滚的完整命令见
`design/public/offline-build-and-server-deployment.md`；PaddleOCR 专用入口
保留在 `design/public/paddleocr-offline-deployment.md`。
