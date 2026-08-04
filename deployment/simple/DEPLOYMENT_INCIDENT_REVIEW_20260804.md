# `.60` 简化 RAG 部署问题复盘

## 1. 文档范围

本文记录 2026-08-04 将 DOCX RAG 从本地 WSL 部署到
`user4a@10.242.180.60` 的实际问题、根因、修复和验证结果。目标目录为
`/data/tyf/RAG`，部署版本为 `fb1b994406ac`。

实际拓扑如下：

- `.60`：RAG App、Qdrant、OCR、Qwen3-Embedding-0.6B、
  Qwen3-Reranker-0.6B。
- `.57:8000/.57:8001/.58:8000/.58:8001`：四个
  `Qwen/Qwen3-8B-AWQ`。
- `.60:8088`：前端和查询 API。
- `.60:8091`：Embedding；`.60:8092`：Reranker。

本文只记录已经观察到的事实。标记为“待复验”的项目不能视为通过。

## 2. 当前结论

截至本文整理时，已经确认：

- `rag-app`、`rag-ocr`、`rag-qdrant`、`rag-embedding`、
  `rag-reranker` 均为 `healthy`。
- Embedding 实际向量维度为 1024。
- 全量索引任务 `job_e2097eefbab94d5b81f2eb57b802df62` 成功，
  `error_code=null`。
- 活动 alias 为 `rag-docx-active`，当前指向
  `rag-docx-dd16e57d6b39-e2097eefbab9`。
- `/ready` 返回 HTTP 200，所有组件 `ready=true`，四个 LLM 均健康。
- 四个 LLM 的普通聊天和严格 JSON Schema 请求均已逐个实测通过。

尚未确认：

- `docx-rag:fb1b994406ac-answer2048-hotfix` 是否确实为当前容器镜像，以及容器内
  `answer_output_tokens/repair_output_tokens` 是否均为 2048，已由后续 Trace 中的
  `max_output_tokens=2048` 间接确认；仍应补一次镜像名和配置文件的直接检查。
- 2048-token 查询已经完成两次模型调用，但因回答 Schema 与解析器语义不一致
  返回 `VALIDATION_FAILED`。本地契约修复已通过测试，服务器热修和最终
  `type=final,status=answered` 仍待执行。
- 当前运行模式是 `demo`，`production_ready=false`；没有完成生产参数冻结、
  正式模型 revision 固化、评测门禁和生产验收。

## 3. 部署过程中遇到的问题

### 3.1 原部署文档不是从零开始

**现象**

原文档默认服务器已经具备目录、镜像、模型资产和环境文件，没有针对
`.60` 上只有空目录 `/data/tyf/RAG/` 的情况给出完整步骤。

**处理**

将 `DEPLOYMENT_GUIDE.md` 改成固定服务器、固定用户、固定路径的从零流程，
明确区分：

- 本地 WSL 执行的上传命令。
- `.60` 执行的目录、镜像、模型和部署命令。
- `.57/.58` 上四个现有 LLM 的调用方式。

**遗留风险**

当前指南还需要在最终重新打包时吸收本文中的全部热修，不能继续把旧的
`fb1b994406ac` 原始镜像当成可重复部署的最终制品。

### 3.2 `/data/tyf/RAG` 目录权限和建目录失败

**现象**

首次使用 `install -d -m 0750` 创建目录时出现：

```text
install: cannot change permissions of '/data/tyf/RAG/simple': No such file or directory
install: cannot create directory '/data/tyf/RAG/shared': Permission denied
```

**根因**

部署用户对目标根目录及部分新建路径没有稳定的所有权/写权限；在该目录上
直接使用 `install -d -m` 也没有得到预期结果。

**处理**

- 先由有权限的账号将 `/data/tyf/RAG` 交给 `user4a`。
- 使用 `umask 027` 和 `mkdir -p` 创建子目录。
- 环境文件保持最小权限，不使用全局 `chmod 777`。

**验证**

服务器返回 `UPLOAD_DIRS_OK`，后续目录可以上传和写入。

### 3.3 大文件 rsync 中途断开

**现象**

简单部署包约 13.36 GB 上传完成后，模型资产包上传时 SSH 连接关闭：

```text
rsync: connection unexpectedly closed
rsync error: unexplained error (code 255)
```

**处理**

- 上传使用 `rsync -av --partial --info=progress2`，中断后重跑。
- 不根据 rsync 最后一行主观判断，必须在服务器执行 SHA256 校验。

**验证**

服务器最终返回：

```text
rag-model-assets-qwen3-embedding-reranker-0.6b-v1.tar.gz: OK
```

### 3.4 SHA256 清单在错误目录执行

**现象**

校验热修包时 `DEBS.sha256` 本身存在，但清单内的 `debs/*.deb` 全部报告
`No such file or directory`；另一次出现：

```text
sha256sum: HOTFIX_MANIFEST.sha256: No such file or directory
```

**根因**

SHA256 清单记录的是相对于热修包根目录的路径，但命令在其他目录执行。

**处理**

每次先进入具体热修目录，再运行校验：

```bash
cd /data/tyf/RAG/uploads/ocr-libgl-hotfix/libgl-runtime-20260804
sha256sum -c HOTFIX_MANIFEST.sha256
sha256sum -c DEBS.sha256
```

EMF 热修同理，必须在 `emf-runtime-20260804` 根目录执行。

### 3.5 `rag.env` 权限导致读取失败

**现象**

```text
awk: fatal: cannot open file '/data/tyf/RAG/rag.env' for reading: Permission denied
```

**根因**

环境文件的属主与实际部署用户不一致。

**处理**

将文件所有权修正给 `user4a`，权限保持 `0600`。后续读取查询 token 时优先从
运行容器读取，避免扩大文件权限：

```bash
QUERY_TOKEN="$(docker exec rag-app printenv RAG_QUERY_TOKEN)"
```

### 3.6 OCR 容器反复重启：缺少 `libGL.so.1`

**现象**

`rag-ocr` 已重启 14 次且 `unhealthy`，日志持续出现：

```text
ImportError: libGL.so.1: cannot open shared object file: No such file or directory
```

**根因**

OCR 镜像内的 OpenCV/PaddleOCR 运行时依赖 `libGL.so.1`，原离线镜像没有携带
对应系统库及其依赖。

**处理**

- 生成完全离线的 `libgl-runtime-20260804` 热修包，共 11 个 `.deb`。
- 热修包可先传到 `.54`，再传到
  `/data/tyf/RAG/uploads/ocr-libgl-hotfix/libgl-runtime-20260804/`。
- 基于原 OCR 镜像构建
  `docx-rag-ocr:d6e38d57aab1-smoke-20260804011642-libgl-hotfix`。

**验证**

OCR 容器随后进入 `healthy`，PaddleOCR 可以启动。

### 3.7 DOCX 中的 EMF 图片无法进入 OCR

**现象**

第一次索引诊断发现 18 个 EMF 媒体项为：

```text
OCR_REQUEST_REJECTED
```

另有一张 PNG 的 OCR 置信度约为 `0.79818`，略低于 `0.8`，被标记为
`OCR_LOW_CONFIDENCE`。

**根因**

OCR 接口只接受可直接解码的栅格图片；DOCX 中的 `image/emf` 是矢量格式，
原镜像没有安全、离线的 EMF 转 PNG 能力。

**处理**

- 制作 `emf-runtime-20260804`，包含 LibreOffice Draw 及其完整离线依赖，
  共 108 个 `.deb`。
- 增加受限的 `rag-emf-to-png` 转换入口和冻结 registry。
- 使用真实失败样本
  `f5a479e3868a5b43aa5161363df7830e1cb35fab3d94255f5e9aae10ccd9ba10`
  验证 10240 字节 EMF 可转换为 794×1123、7741 字节 PNG。
- 最终 OCR 镜像为
  `docx-rag-ocr:d6e38d57aab1-smoke-20260804011642-emf-hotfix`。

**重要结论**

EMF 是真实的内容完整性问题，但不是后续全量索引任务失败的唯一或直接主因。
代码会记录单个 OCR 媒体失败并继续处理文档；真正导致多数文档任务失败的是
Embedding 批次大小不兼容，见 3.10。

### 3.8 服务器缺少可用的 Docker buildx

**现象**

```text
ERROR: BuildKit is enabled but the buildx component is missing or broken.
```

**根因**

Docker Engine 可用，但没有安装或没有可用的 buildx 组件。

**处理**

- 没有在线安装 buildx，也没有扩大服务器依赖。
- 将热修 Dockerfile 中的 `COPY --chmod/--chown` 改为兼容旧 builder 的
  `COPY` 加 `RUN chmod/chown`。
- 使用 `DOCKER_BUILDKIT=0 docker build --network none` 完全离线构建。

**验证**

本地旧 builder 构建通过，并完成 EMF 沙箱自检：

```text
EMF_SANDBOX_SELFTEST_OK 10240 7741
```

### 3.9 Docker socket 权限不足

**现象**

```text
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

**根因**

`user4a` 尚未进入有权访问 Docker socket 的用户组，或加组后当前 SSH 会话还没
重新加载组成员关系。

**处理**

将 `user4a` 加入 `docker` 组并重新登录 SSH；确认 socket 为
`root:docker 0660`，再继续构建。

### 3.10 全量索引失败：Embedding 批次超过 TEI 上限

**现象**

索引任务先后出现：

```text
job_f24c3c52aa344c8ba66510b069d89263  INDEX_RUNTIMEERROR
job_236b1e1b3324438e9589f90595ff15b0  INDEX_RUNTIMEERROR
```

任务明细显示 6 份文档中只有一个 3-chunk 文档成功，其余文档均为
`ExternalRequestRejectedError`。

**根因**

`.60` 的 TEI `/info` 明确返回：

```text
max_client_batch_size=8
```

但 RAG 运行时默认 `embedding_max_batch_size=32`，Compose 又没有向 App 和
Worker 传入覆盖值。小文档不超过 8 条输入所以成功，大文档一次发送超过 8 条
后被 TEI 以 4xx 拒绝。

**处理**

在 App、Worker 和环境模板中统一设置：

```dotenv
RAG_EMBEDDING_MAX_BATCH_SIZE=8
```

**本地验证**

- `tests/test_simple_deployment.py`：`5 passed`。
- `git diff --check`：通过。
- `docker compose ... config --quiet`：通过。

**服务器验证**

```text
job_e2097eefbab94d5b81f2eb57b802df62
state=succeeded
error_code=null
```

活动 alias 随后切换到本次任务生成的物理 collection。

### 3.11 `/ready=503`：Embedding/Reranker 健康契约写错

**现象**

索引成功且容器均为 `healthy`，但 `/ready` 返回 503：

```text
embedding ready=false healthy_endpoints=0
reranker  ready=false healthy_endpoints=0
llm       ready=true  healthy_endpoints=4
```

**根因**

App 使用通用健康探针，错误地要求 Embedding 和 Reranker 都提供
`/v1/models`。实际契约是：

- TEI Embedding：`/health`、`/info`、`/v1/embeddings`。
- Reranker：`/health`、`/rerank`。
- 只有 OpenAI 兼容 LLM 使用 `/v1/models`。

**处理**

- Embedding/Reranker 的运行时 readiness 只执行其可用性检查。
- LLM 继续执行 `/health` 和 `/v1/models` 的精确 model ID 检查。
- 服务器构建小层镜像
  `docx-rag:fb1b994406ac-ready-hotfix`，只重建 `rag-app`，不重建索引。

**本地验证**

`tests/test_runtime_construction.py` 和 `tests/test_health_api.py` 合计
`9 passed`。

**服务器验证**

`/ready` 返回 HTTP 200，Embedding、Reranker 和四个 LLM 全部 `ready=true`。

### 3.12 宽泛问答等待约 150 秒后拒答

**现象**

查询 trace `9ef9efe53439466680b8cbc3f05ce5fd` 的前四阶段正常：

- retrieve：200 ms，24 个候选。
- rerank：1181 ms，24 个候选得到 6 个最终结果。
- assemble：1198 ms，6 条证据、2525 evidence token。
- LLM 阶段等待到 150254 ms 后返回
  `status=refused, refusal_code=MODEL_UNAVAILABLE`。

**排除结果**

对四个 LLM 并行实测：

- 普通请求均为 HTTP 200，约 0.33 秒。
- 严格 `response_format=json_schema` 请求均为 HTTP 200，约 1.09 秒。
- 四路返回的 model、`finish_reason=stop` 和 usage 格式均正确。

因此不是 LLM 宕机，也不是 JSON Schema 不兼容。

**最符合现象的根因**

真实问题要求概括多份资料并逐条给引用，输出预算原为 1024 token。四个端点
运行相同模型且 `temperature=0`；若每一路都生成到 1024 token 后以
`finish_reason=length` 截断，客户端会把响应视为无效并依次尝试四路。
按实测约 25–27 token/s 计算，`1024 / 27 × 4 ≈ 152 秒`，与实际 149 秒高度
吻合。由于失败响应的原始 `finish_reason` 没有被 App 记录，这一根因仍需由
2048-token 复验最终确认。

**处理**

- `answer_output_tokens`：1024 → 2048。
- `repair_output_tokens`：1024 → 2048。
- `RAG_LLM_TIMEOUT_SECONDS`：默认 60 → 180。
- 构建镜像 `docx-rag:fb1b994406ac-answer2048-hotfix`。
- 只重建 `rag-app`，不重建索引。

**当前验证**

- 容器内 `RAG_LLM_TIMEOUT_SECONDS=180`。
- 当前 App `/ready=true`，所有依赖均就绪。
- 当前容器镜像名和两个 2048-token 配置值仍需用不依赖 stdin 的命令复核。
- 原宽泛问题的最终 `answered` 结果仍待复验。

### 3.13 `production_ready=false` 的含义

当前 `/ready` 包含：

```json
{"ready":true,"run_mode":"demo","production_ready":false}
```

`ready=true` 表示当前服务可以索引、检索和问答；`production_ready=false` 不会
阻止 `/api/chat` 或前端使用。它表示当前检索参数仍为 `provisional`，模型
revision、冻结决策、正式评测和生产发布证据尚未完成。

不能只把 `RAG_RUN_MODE` 改成 `production`。生产模式会要求冻结配置和正式
证据，直接改变量会导致索引或 readiness 被生产门禁拒绝。

### 3.14 交互式命令中的 `set -e` 导致 SSH 会话退出

**现象**

若整段交互命令启用 `set -e`，任一校验或 Docker 命令失败后，远程 shell 会
直接结束，看起来像“服务器把用户踢出”。

**处理**

- 故障处理阶段拆成短命令块。
- 使用 `if command; then ...; else ...; fi` 控制后续动作。
- 只有经过验证的自动化脚本内部保留 `set -euo pipefail`。

### 3.15 2048-token 回答仍失败：Schema 与解析器语义不一致

**现象**

第二次宽泛问答 trace `ae4e3bba84614c4e95253d3901269293` 显示：

```text
max_output_tokens=2048
prompt_tokens=4829
completion_tokens=1960
first_validation_code=INVALID_ANSWER_SCHEMA
repair_triggered=true
repair_validation_code=INVALID_ANSWER_SCHEMA
model_calls=2
```

这证明 2048 预算已经用于真实 LLM 调用；首次回答和一次修复都成功返回，但都被
应用语义校验拒绝。

**根因**

原 JSON Schema 只要求顶层同时存在 `status`、`claims` 和 `refusal_reason`，却
没有表达以下状态联动：

- `answered`：`claims` 必须非空，`refusal_reason` 必须为 `null`。
- `refused`：`claims` 必须为空，`refusal_reason` 必须是非空字符串。

因此 guided decoding 可以生成“符合 JSON Schema、但必然被
`parse_answer_response()` 拒绝”的对象；修复调用继续使用同一份缺陷 Schema，
所以再次得到 `INVALID_ANSWER_SCHEMA`。增加 token、延长超时或重复调用都不能
解决这一问题。

**本地处理**

- 条件 Schema 方案随后被真实四端点探针否决：四个端点虽然均返回 HTTP 200，
  但 `anyOf` 分支下都省略了必填 `claims`，`SEMANTIC_OK=False`。因此不能依赖
  vLLM 对 `anyOf/oneOf/if-then` 的实现差异。
- 最终将模型内部协议收敛为顶层只有 `claims` 的无条件 Schema。模型不再输出
  `status/refusal_reason`；应用根据 `claims=[]` 或全部引用校验结果生成既有 API
  的 `answered/refused` 状态。
- Schema 固定最多 5 条 claim、每条最多 2 个 support，并限制 claim/quote 长度；
  空 claims 直接返回 `EVIDENCE_INSUFFICIENT`，不再执行第二次 repair。
- 原有 evidence ID、逐字 quote、source span、数字、重复项和低置信 OCR 门禁保持
  不变；非空输出校验失败时仍最多 repair 一次。
- SAFE/DIAGNOSTIC Trace 只新增 JSON 是否可解析、顶层键、claims 数量、调用阶段、
  endpoint、token 和校验码；raw output 仍只允许进入 FULL artifact。
- `retry_count=0` 只表示本次成功逻辑调用没有发生 endpoint failover，不能描述为
  “四个端点全部重试”。readiness 对四端点的健康探测也不是回答生成调用。
- 更新实际 prompt revision、`pipeline.json` 和 `ASSETS.sha256`。回答预算没有继续
  增加，也没有重建索引。

**本地验证**

- 新回归测试在旧 Schema 上先以 `KeyError: anyOf` 失败。
- 修复后回答契约相关测试：`64 passed`。
- 扩展部署/运行时测试：`121 passed`。
- mypy：通过。
- Ruff：通过。
- `deployment/ASSETS.sha256`：全部通过。
- `git diff --check`：通过。

**待服务器验证**

本轮禁止访问服务器。claims-only app 更新包生成后，仍需由用户上传三文件并只
重建 `rag-app`，再用新 conversation 重试真实问题；只有最终
`type=final,status=answered` 且 claims 引用有效才算服务器问答通过。

## 4. 当前服务器关键配置

```text
ROOT=/data/tyf/RAG
REV12=fb1b994406ac
ENV_FILE=/data/tyf/RAG/rag.env
COMPOSE_FILE=/data/tyf/RAG/simple/fb1b994406ac/compose.yaml
RAG_EMBEDDING_MAX_BATCH_SIZE=8
RAG_LLM_TIMEOUT_SECONDS=180
RAG_QDRANT_ALIAS=rag-docx-active
```

已使用或已构建的热修镜像：

```text
OCR: docx-rag-ocr:d6e38d57aab1-smoke-20260804011642-emf-hotfix
Readiness App: docx-rag:fb1b994406ac-ready-hotfix
Answer candidate: docx-rag:fb1b994406ac-answer2048-hotfix
```

`Answer candidate` 是否为当前运行镜像，以及其两个输出预算是否均为 2048，仍按
本文第 6 节第 1 项复核，不能仅根据 `/ready=true` 推断。

## 5. 不应重复执行的操作

- 文档目录已经解压后，不要重新运行首次 `deploy.sh`；它会因 docs 非空拒绝。
- 索引已经成功发布后，不要因为 App readiness 或回答超时重新建立索引。
- 不要删除 Qdrant、SQLite、docs 或失败 collection 来“清故障”。
- 不要扩大 `rag.env` 到 0777；保持正确属主和 0600。
- 不要为临时修复在线 pull、在线 apt 安装或重新上传未变化的模型权重。
- 不要把 `production_ready=false` 简单改成 true 或只切换运行模式。

## 6. 后续必须完成的事项

1. 上传 claims-only app 更新包的 `app-image.tar.gz`、SHA256 sidecar 和
   `update-app.sh`，只更新 `rag-app`；用 `docker inspect` 确认新 App 镜像，
   随后用新 `conversation_id` 重试原宽泛问题。只有最后出现
   `type=final,status=answered` 且 claims 引用有效才能标记真实问答通过。
2. 将两层 OCR 热修和两层 App 热修合并回正式源码/Dockerfile/config，不能把
   服务器临时镜像作为长期唯一来源。
3. 用新的 Git revision 重建 App/OCR/simple bundle，重新生成所有 SHA256，
   让全新服务器按一份指南即可完成部署，不再手工叠热修层。
4. 在新制品上重新执行离线校验、容器健康、全量索引、alias、readiness 和真实
   问答验收。
5. 若要对外生产使用，再完成模型 revision 固化、检索参数评测冻结、
   `FREEZE_DECISION.json`、生产 acceptance 和回滚验证。

## 7. 最终判定规则

只有同时满足以下条件，才可以称为本次 Demo 部署完整通过：

- 五个 RAG/模型容器全部健康。
- `/ready` HTTP 200 且所有组件 `ready=true`。
- 活动 alias 与成功索引任务生成的 collection 一致。
- 实际问题依次出现 `rewrite/retrieve/rerank/assemble/validate/complete`。
- 最后一行是 `type=final,status=answered`，并包含非空 claims 和有效引用。

生产就绪是另一套更严格的验收，不能由上述 Demo 结果替代。
