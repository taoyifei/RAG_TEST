# V3-05 History、会话、并发与离线包功能收口

日期：2026-09-10

公开性：本文只记录合成数据、非敏感身份、合同和门禁结果。真实问题/答案、私有
文档、原图、数据库、Secret、Authorization/Cookie、FULL Trace、模型 delta 和
本机私有路径不进入 Git 或 Review ZIP。

V3-05 在既有 V3 Word、Evidence、Operational Trace 和真流基础上完成用户侧功能
收口：History 与技术 Trace 现在共享可验证导出语义；Product 支持有作用域的多轮
会话与最小 Feedback；SAFE 非流式查询具备权限绑定的 singleflight；结果缓存与
Profile 资源有硬上限和退役生命周期；当前 React、OpenAPI、migration 与兼容清单
由同一个镜像内 Product 资产清单闭合，并由单一离线包完成 clean-room 验收。

本轮没有修改 Parser/Chunker、chunk 大小、top-k、Evidence 阈值或活动索引内容，
也没有调用真实 LLM、OCR 或视觉 Provider。源码合并就绪与生产发布、真实模型质量
继续分开判断。

## 最终状态

| 状态项 | 状态 | 依据与边界 |
| --- | --- | --- |
| `V3_05_ENGINEERING_DONE` | `PASS` | 必需功能、迁移、前后端、浏览器、性能、构建和离线包本地门禁完成 |
| `HISTORY_TRACE_EXPORT_READY` | `PASS` | 当前/旧/History-only、单条/批量、正文 opt-in、授权、大小及竞态合同均覆盖 |
| `CONVERSATION_READY` | `PASS` | owner/Project/KB/Revision 绑定、TTL、来源撤销、sync/stream final 提交和取消不污染 |
| `FEEDBACK_READY` | `PASS` | canonical upsert、有限原因码、旧 ID、作用域验证和可恢复 Trace 投影 |
| `SINGLEFLIGHT_CACHE_READY` | `PASS` | SAFE 非流式等价合流、独立 History/Trace、聚合取消、LRU/字节上限及资源退役 |
| `OFFLINE_BUNDLE_READY` | `PASS` | 同一镜像/清单、无源码挂载、内部网络、篡改拒绝和 Product DOCX smoke |
| `CURRENT_CHUNKING_PRESERVED` | `PASS` | 冻结 chunk identity 不变；未用分块或 Evidence 策略换取性能绿灯 |
| `SOURCE_MERGE_READINESS` | `PASS` | 默认关闭未验证能力时，源码与本地工程门禁满足隔离 V3-06 进入条件 |
| `PRODUCTION_RELEASE_READINESS` | `BLOCKED` | 当前镜像的 OS 风险需要责任人人工批准；不代签、不发布 |
| `LIVE_MODEL_QUALITY` | `BLOCKED` | 未授权真实 Provider；本轮真实调用分母为 0 |
| `V3_06_ENTRY` | `PASS` | Review 包、SHA、提交和全部 P0 进入条件可核验后进入隔离合并 |

机器可读的最终 SHA、镜像 ID、资产 digest、门禁退出码和 clean-room 身份位于本地
`artifacts/v3-05/`；本文不写入会因最后一次纯文档提交而自失效的伪造自引用 SHA。

## 根因与修复矩阵

| 来源 | 最早根因 | 修复 | 负向保护 |
| --- | --- | --- | --- |
| 用户已指出：History 支持包 | History、Operational Trace 与旧 flat event 各走不同读取/导出入口，无法形成同一语义的支持包 | 统一当前、legacy 与 History-only 解析；单条/批量生成确定性 ZIP 和 canonical manifest | 跨 scope、正文未授权、来源删除、过期、重复 ID、成员/字节上限、clear/export 竞态 |
| 用户已指出：技术 Trace 兼容 | 技术导出绕过兼容 coordinator，旧 32hex 与 `trace_` 身份不一致 | 单条/批量均先 metadata 授权和来源复核，再在同一 lease 内物化 payload | 未授权 Artifact 不得在 403 前读取；损坏 zlib/SHA 安全映射为 503 |
| 用户已指出：清理语义 | UI 把 History clear 描述成删除所有 Trace | `DELETE /history` 仅删除 Query History 与旧 flat event；Operational Trace 独立 prune | 浏览器验证 clear 后当前 Trace 仍可读取；导出 lease 阻止并发 prune |
| 用户已指出：Conversation | Product 请求没有会话身份，后端也无受支持事实的持久边界 | 正式 migration、加密会话、8 轮/24h/16k 上限、最小已验证 claim 与独立来源身份 | 跨 owner/Project/KB、Revision 变化、来源撤销、取消/失败/partial 均不得污染下一轮 |
| 用户已指出：Feedback | 反馈仅是旧 Trace 字段，没有 canonical 所有权与恢复语义 | 主库 canonical upsert + outbox；只保存 useful 与有限原因码，投影到 Trace 供筛选 | ingestion/RUNNING/他人 Trace 拒绝；raw/prefixed ID 兼容；投影可重试 |
| 用户已指出：缓存/并发 | 进程缓存无数量/字节上限，等价并发重复 Provider 计算 | 512 entries/约 64 MiB LRU+TTL；SAFE 非流式 singleflight | key 绑定 owner、权限、scope、Revision、serving、query/context/filter/limit/生成行为；2/8/32 并发、取消和失败 |
| 用户已指出：资源退役 | invalidate 后旧 service 一直保留到 Runtime shutdown | generation lease/refcount；最后一个在途 lease 释放后关闭旧 cache/client/provider | 100 次轮换有界；轮换后新请求不能加入旧代际 singleflight |
| 用户已指出：离线资产 | V3-04 镜像缺 Product 完整资产清单；legacy `deployment/ASSETS.sha256` 与 React dist 不是同一语义 | 保留 legacy 清单；镜像构建后生成 `/app/product-assets.json`，覆盖 frontend/OpenAPI/compatibility/migrations | 镜像内 `--network none` 自检、成员篡改失败、同镜像离线包、无 build/source bind |
| 本轮额外发现：Trace 性能 | SAFE 每个事件同步写两个 SQLite 路径，高并发 P95 显著退化 | 非 FULL 请求私有有界缓冲；完成时单 writer/单事务写 canonical Trace 与兼容事件；FULL 仍同步 fail-closed | exact duration、事务回滚、队列有界、capture complete、0 dropped 与冻结非劣阈值 |
| 本轮额外发现：Docker clean-room | Docker Desktop 在所有网络均为 `internal` 时不建立宿主端口映射，即使容器 `/live` 为 200 | HTTP 探针在隔离 App 容器内通过回环执行；Bootstrap Token 不再读回宿主进程 | base URL/Origin 只允许 loopback；无源码 mount、无外联、真实登录/入库/查询 |

## History 与 Trace 导出合同

公开 Trace ID 统一为 `trace_<32hex>`，读取和导出也接受旧 `<32hex>`。同一批次同时
传入两种等价写法会被视为重复并返回 422，避免同一对象被计数两次。四个管理员
入口及数据边界详见
[History、Operational Trace 与会话边界](../../docs/public/history-trace-conversation.md)。

History 支持包永远是 ZIP。每个请求项固定包含安全 metadata，并按存在性加入
`history.json` 与 `operational-trace.json`；manifest 记录格式版本、正文是否包含、
缺失原因、成员 SHA256 与字节数。正文默认关闭，管理员显式请求后仍须通过 owner、
Project、KB、活动 Revision 与来源复核。Operational Trace 不保存问题、答案、
Prompt、思维链或模型 delta。

技术 Trace 单条可返回 JSON 或 ZIP，批量返回 ZIP。批量授权分成 metadata 与 payload
两阶段，并保持同一组 export lease：任意 ID 跨 scope 或来源失效时，在读取 FULL
Artifact 前整体失败。损坏 Artifact 不泄露压缩异常或私有正文，只返回稳定的
`TRACE_ARTIFACT_CORRUPT`。

Review 包内的脱敏样例只使用合成身份和公开字段，分别展示：

- 当前 `trace_` 单条支持包；
- 当前 + legacy + History-only 批量支持包；
- 当前/legacy 技术 JSON 与 ZIP；
- 每个 ZIP 的 canonical `MANIFEST.json`、成员列表与外部 SHA256。

## Conversation 与 Feedback

Conversation 身份由 owner、Project、KB、conversation ID、活动 Revision 与上下文
digest 共同约束。正文沿用主密钥 AES-GCM 加密；最多保存 8 轮、24 小时空闲 TTL、
单问题 2000 字符、会话问题和事实摘要合计 16000 字符。服务只保存用户问题、最终
已验证 claim 的最小摘要和支持它的 Revision/Document/Version/Chunk 身份，不保存
Prompt、CoT 或流式 delta。

上下文只在显式代词且存在服务端会话事实时参与保守改写，数字、否定、时间、版本
与标识符仍受硬约束。下一轮读取前重新检查活动 Revision 和来源；删除的来源只使
相应 claim 失效，不把旧问题伪造成事实。同步回答在唯一成功结果后提交；SSE 仅在
final 已交付确认后提交；取消、断连、失败和 extractive failure 不提交，REFUSED
只保存问题。

Feedback 只接受 ANSWERED/REFUSED 查询。主库是 canonical truth，Trace 中
`feedback_useful` 是可恢复筛选投影。记录不含自由文本、问题、答案、Evidence、IP
或凭据；重复提交是 scope 内 upsert，投影状态为 `PENDING`、`APPLIED` 或
`NOT_APPLICABLE`。

## singleflight、缓存与资源生命周期

singleflight 只合并 SAFE 非流式请求的底层计算，不合并流或 DIAGNOSTIC/FULL。等价
身份覆盖 owner/访问身份、Project/KB、活动 Revision、index/serving/retrieval
fingerprint、原问与获准语义变体、会话摘要、过滤器、limit、related、Dense 与生成
行为。每个订阅者仍得到独立 Trace ID、History 和取消状态；follower 的 Provider
诊断不会冒充自己的调用。单个订阅者取消不终止其它订阅者，全部取消才协作取消
上游。

结果缓存采用 LRU、300 秒 TTL、512 entries 与约 64 MiB 双上限；统计只暴露数量、
估算字节、命中/未命中、淘汰和过期。Profile/Provider 使用 generation lease；
invalidate 后不再分配旧服务，最后一个在途请求释放后才关闭旧缓存、HTTP client
和 Provider。connection/credential binding 的安全 digest 进入 serving identity，
轮换后的相同问题不会加入旧代际 flight。

## 性能与当前 Chunk identity

性能门使用默认 Product HTTP 路径与确定性单 DOCX 离线语料，冻结 4 种 Trace 模式、
cold/warm、并发 1/8/16、unique/identical 共 48 cells。每 cell 至少 32 samples；
普通观测至少 5 waves，NONE/SAFE P95 比较至少 20 个独立 waves，且相邻并交替先后
顺序，避免一个共享调度慢波被误当成同批并发请求的多个独立尾样本。内存采样和
writer flush 均在计时区外，矩阵在独立 pytest 子进程执行以隔离前序用例状态。

冻结阈值包括：HTTP error 为 0、SAFE dropped 为 0、identical cold 每 wave 只有一个
fresh execution、缓存不超过 512，以及 SAFE P95 相对 NONE 不劣于
`max(10%, 10ms)`。最终报告记录 88 项判定、5312 个计时 HTTP 请求、stream 首协议/
首内容/总时延、cache/singleflight、线程/FD/RSS 和取消探针。具体值及精确 HEAD
写入本地性能 JSON，不把一次本机数值外推为生产容量。

当前 chunk identity 的公开 SHA256 随最终性能收据冻结；与 V3-04 当前 Parser、
Chunker 与 index policy 一致。本轮没有修改这些组件，也没有以改变 chunk、top-k
或 Evidence 阈值取得绿灯。main/Industry 没有在同语料、配置、模型、硬件和缓存
条件重跑，性能比较保持 `NOT_COMPARABLE`。

## Product 资产与离线包

legacy `deployment/ASSETS.sha256` 保持旧静态前端语义，不被 React dist 覆盖。Docker
构建在 React 完成后生成 Product 权威清单，固定相对路径、SHA256、size、mode 与
source revision；镜像启动不依赖宿主源码或 `node_modules`。

离线包 builder 只消费已构建的同一个 App image 和固定 Qdrant image，不二次构建
前端。包内包含镜像 tar、Compose、内部网络 override、Product 资产清单、canonical
receipt 与 `MANIFEST.sha256`，外部另有 archive sidecar。clean-room 在全新目录：

1. 校验外层 sidecar、归档集合、内部 manifest 与镜像 content ID；
2. 以 `--network none` 运行 Product 资产自检和篡改拒绝；
3. 扫描包、镜像 metadata/history 和 export 内容中的 Secret 形状；
4. 使用 `pull_policy=never`、命名卷、无源码 bind、内部网络启动 App/Qdrant；
5. 在 App 容器回环地址验证 `/live`、React 根节点、登录、建 Project/KB、合成 DOCX
   入库与离线查询；
6. 记录 source/image/manifest、活动 Revision 和 Trace 身份，然后删除容器、网络与卷。

V3-04 的 `APP_IMAGE_LACKS_FULL_ASSET_MANIFEST...` 失败保留为历史。本轮最初 clean-room
又真实暴露了 Docker Desktop internal-network 宿主端口不可达及 Compose 探针参数/
响应形状问题；这些失败也保留，不以最终通过覆盖。

## Migration、备份与兼容

正式 migration 增加 History export lease/journal 及 Product conversation/turn/support/
feedback/outbox 表。新库、重复初始化、目标旧库升级和部分 migration 继续 fail-closed。
备份包含 Conversation 与 Feedback canonical 表，并在恢复后验证 scope、TTL、turn
linkage 和 Trace projection；export spool、进程缓存及 in-flight singleflight 不进入
备份。旧客户端不传 `conversation_id` 时保持原单轮请求合同。

## 必测矩阵

| 测试项 | 状态 | 主要自动化证据 |
| --- | --- | --- |
| `HISTORY_SINGLE_SUPPORT_EXPORT` / `HISTORY_BATCH_SUPPORT_EXPORT` | `PASS` | `tests/api/test_history_trace_export.py`、桌面/移动 Chromium |
| `TECH_TRACE_SINGLE_EXPORT_COMPAT` / `TECH_TRACE_BATCH_EXPORT_COMPAT` | `PASS` | `tests/api/test_operational_trace_product.py`、浏览器 JSON/ZIP 内容核对 |
| `LEGACY_FLAT_TRACE_EXPORT` / `HISTORY_ONLY_PRE_V3_EXPORT` | `PASS` | 当前、raw 32hex、legacy 与 History-only fixtures |
| `TRACE_ID_PREFIXED_AND_32HEX` | `PASS` | bare alias、重复归一化和深链接 API/浏览器回归 |
| `EXPORT_CANONICAL_MANIFEST_AND_SHA` | `PASS` | 确定性 ZIP、成员 SHA/size/mode/path 校验 |
| `EXPORT_SIZE_COUNT_AND_DUPLICATE_LIMITS` | `PASS` | 单项/总量/压缩/成员数/100 ID 和 duplicate 负向测试 |
| `EXPORT_AUTHORIZATION_AND_BODY_PRIVACY` | `PASS` | metadata-first 授权、body opt-in、删除/撤权/过期与 payload-before-auth 负向测试 |
| `EXPORT_DELETE_PRUNE_RACE` / `HISTORY_TRACE_CLEAR_SEMANTICS` | `PASS` | 持久 lease、并发 clear/prune、浏览器 clear 后 Trace 保留 |
| `PRODUCT_CONVERSATION_SINGLE_AND_MULTI_TURN` | `PASS` | sync/stream、保守代词改写和新会话测试 |
| `CONVERSATION_SCOPE_TTL_REVISION_REVOCATION` | `PASS` | owner/Project/KB、TTL、Revision、来源删除、幂等与密文测试 |
| `CONVERSATION_STREAM_FINAL_COMMIT` | `PASS` | final ACK 后提交，取消/断连/失败不提交 |
| `PRODUCT_FEEDBACK_UPSERT_AND_FILTER` / `FEEDBACK_SCOPE_AND_LEGACY_COMPAT` | `PASS` | canonical/outbox/recovery、raw ID、筛选和浏览器回归 |
| `CACHE_BOUNDEDNESS_AND_EVICTION` | `PASS` | LRU/byte/TTL/close/统计与超过容量测试 |
| `PRODUCT_SINGLEFLIGHT_EQUIVALENCE` | `PASS` | 2/8/32 并发、一次底层计算、独立 Trace/History |
| `SINGLEFLIGHT_CANCELLATION_AND_SCOPE` | `PASS` | follower/leader/all cancel、错误传播、scope/Revision/conversation/credential 隔离 |
| `HISTORY_PAGINATION_BOUNDED_DECRYPT` | `PASS` | 10k 行只解密当前页；关键词候选扫描有界并返回 complete/cursor |
| `PROFILE_RESOURCE_RETIREMENT` | `PASS` | 在途 lease、100 次轮换和 client/cache 关闭 |
| `OFFLINE_PRODUCT_ASSET_MANIFEST` | `PASS` | 镜像 30 个当前 Product 资产自检及篡改拒绝 |
| `OFFLINE_BUNDLE_CLEAN_ROOM_SELFCHECK` | `PASS` | 全新目录、同镜像、内部网络、真实 DOCX smoke 收据 |
| `ROOT_STATUS_AND_DOC_TRUTH` | `PASS` | 根状态、README、quickstart、DOC 矩阵和本报告 |
| `PRODUCT_HTTP_PERFORMANCE_GATE` | `PASS` | 48 cells / 88 checks 冻结非劣门 |
| `CURRENT_CHUNKING_PRESERVED` | `PASS` | Parser/Chunker/index identity 与冻结基线一致 |
| `FULL_LOCAL_GATES` | `PASS` | 完整离线 Python、前端、OpenAPI、Chromium、脚本与 Compose 门禁 |
| `BUILD_CONTEXT_GATE` | `PASS` | clean committed HEAD 的不可变 Git context + BuildKit |
| `RELEASE_SECURITY_GATE` | `BLOCKED` | dependency/Secret/SBOM/Trivy 分项独立记录；OS 风险无人为批准 |
| `RELEASE_ACCEPTANCE` | `NOT_RUN` | 安全门阻断后不运行生产 release acceptance |
| `V3_06_ENTRY` | `PASS` | P0 功能和 Source Merge Readiness 均通过；生产/Live 阻断不伪装成源码失败 |

## 提交与交付边界

行为提交按 Trace 导出、会话/并发、前端、离线资产、性能、文档与 clean-room 探针
拆分。最终起止 SHA、完整提交列表和每个门禁 exit code 由 Review 包中的收据冻结。
所有提交只在本地 `codex/universal-selective-word-v3`；V3-05 不 push、不合并、不部署。

只有本报告列出的八项 V3-06 进入状态和 Review 包校验均通过，才创建仓库外隔离
worktree，从最新 `origin/feature/universal-rag` 执行 `--no-ff` 合并并在 merge commit
重跑门禁。生产发布仍必须等待责任人对精确镜像 OS 风险作出真实决定；真实 LLM、
OCR 和视觉质量仍须单独授权、单独取证。
