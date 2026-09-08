# V3-02 默认 Product Operational Trace、多模态证据与固定 Chunk 基线

日期：2026-09-08 至 2026-09-09

公开性：本文只记录公开合成语料、代码身份、聚合指标和脱敏门禁结果，不包含
私有文档、文件名、逐文件私有 hash、原图、数据库、凭据、问题/答案正文、Prompt
或 FULL Trace 内容。

V3-02 在当前默认 `rag-app serve` 路径接入了独立 Operational Trace，同时保留
`ProductQueryHistory` 的加密正文和审计职责；本地/远程 OCR 现在共享一套有来源
类型的结果合同，图关系以待审 candidate 和新 Revision 发布；结构化 Parser/
Chunker 策略没有被查询质量问题反向调整。

本阶段没有 push、merge、生产部署或真实 Embedding/LLM/OCR/视觉调用。V3-01 的
最终候选镜像安全门仍为 `BLOCKED`，本阶段源码与 source-tree 验证不构成生产
发布批准。

## 状态

| 状态项 | 状态 | 证据边界 |
| --- | --- | --- |
| `V3_02_ENTRY_FROM_V3_01` | `PASS` | 仅源码开发入口；V3-01 release security 仍为 `BLOCKED` |
| `PRODUCT_HISTORY_PRESERVED` | `PASS` | History 加密正文、引用、usage、终态和重启恢复回归通过 |
| `PRODUCT_OPERATIONAL_TRACE_DEFAULT_PATH` | `PASS` | 当前 Product API/SDK/Application/React 默认链，不依赖 legacy runtime |
| `TRACE_ID_AND_STATUS_COMPATIBILITY` | `PASS` | 公共前缀 ID、旧 32 hex、六类 query 终态及 ingestion 成功态 |
| `TRACE_SAFE_DIAGNOSTIC_FULL` | `PASS` | 内容边界、fail-soft 与 FULL 执行前 fail-closed 均有回归 |
| `TRACE_SPAN_WATERFALL` | `PASS` | 父子、sequence、duration、重复 finish、重启中断 |
| `TRACE_CANDIDATE_FUNNEL` | `PASS` | 通道 rank、独立 score、RRF、选中/淘汰 reason |
| `TRACE_PROVIDER_CHILD_SPANS` | `PASS` | child 保存实际 Provider elapsed；cache/refuse/cancel 与三类调用专项覆盖 |
| `TRACE_ARTIFACT_AND_EXPORT` | `PASS` | 惰性 Artifact、完整性、canonical JSON、稳定 ZIP、限额与权限 |
| `QUERY_INGESTION_HISTORY_LINKAGE` | `PASS` | query/job/document/revision/history 双向公开 ID 贯通 |
| `TRACE_PRIVACY_RETENTION_MIGRATION` | `PASS` | TTL、prune、旧/空/部分 schema、重启与备份恢复 |
| `TRACE_REACT_ADMIN_UI` | `PASS` | React 单元测试与桌面/移动真实 Playwright |
| `TRACE_CAPTURE_PERFORMANCE` | `PARTIAL` | 54 次 consumer 微基线已量化；缺 Product HTTP no-trace/B0 可比数值 |
| `LOCAL_OCR_ADAPTER_CONTRACT` | `PASS` | 可选注入、固定身份、Bearer、限额、timeout/cancel/cache 合同 |
| `REMOTE_LOCAL_OCR_COEXISTENCE` | `PASS` | 同一 Port 与来源合同；不代表本机已安装 Paddle/GPU |
| `OCR_TEXT_PRODUCT_E2E` | `PASS` | 公开合成 adapter/消费者端到端；不声明真实识别质量 |
| `DIAGRAM_RELATION_CONTRACT` | `PASS` | candidate、ambiguity、review、occurrence 和新 Revision 发布 |
| `DIAGRAM_RELATION_LIVE_QUALITY` | `BLOCKED` | `BLOCKED_EXTERNAL`：未获真实视觉模型数据出网与预算授权 |
| `CURRENT_DOCX_CHUNKING_PRESERVED` | `PASS` | Parser/Chunker 身份不变，结构与来源快照回归通过 |
| `FIXED_CHUNK_BASELINE_FROZEN` | `PASS` | 同一 46 chunks/digest 的 B1 bundle 已封存；其中质量门为 `FAIL` |
| `DEV_RUNTIME_IDENTITY_GATE` | `PASS` | 最终 clean source-tree HEAD 的机器可读 JSON；镜像字段明确未运行 |
| `FULL_LOCAL_GATES` | `PASS` | 完整离线、loopback Qdrant、前端、OpenAPI 与浏览器门均完成 |
| `V3_03_ENTRY` | `PASS` | Product Trace 核心闭环完成；查询质量 `FAIL` 作为 V3-03 强制输入 |

## 范围与身份

- 执行分支：`codex/universal-selective-word-v3`。
- V3-02 进入点：`0505fac076ee468916f988b35345c71027049678`。
- 固定当前 Chunk 标签提交：
  `5505e8b7b348ce17eb451eee77c0a7633d1ae0b4`；该提交只刷新 Evaluation V3
  公开合成语料的 Chunk 标签，不改变查询链。
- V3-02A：`c9184c9b58bc4590c8934850e6babb39e70691e2`。
- V3-02B：`adc0334e4fc6a07a61dbaefe413cad207898191d`。
- Provider 子调用耗时修复：
  `231fee2`；DEV identity 命令：`90a6c77`。
- main 只读参照：`af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`；Industry
  只读参照：`5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。两者均未 merge、
  cherry-pick 或修改。
- V3-01 的 Parser、Chunker、Document IR、Artifact、MediaScan、active Revision
  和主备向量空间合同保持不变；V3-02B 只在 OCR/关系正式发布时生成新 Revision。

固定基线绑定运行行为提交
`8f11d904d51041a94358c2b730f5e29a5fd19a9b`；其后的接口文档、测试、备份
权限与失败构造清理修复不改变固定 Chunk 或查询语义。最终 source-tree 身份以
`artifacts/v3-02/dev-runtime-identity.json` 为机器可读权威；报告提交后的最终 HEAD
由该命令重新读取，避免在 Git 提交内容中伪造自引用 SHA。

固定基线目录：
`artifacts/v3-02/fixed-chunk-baseline/final-b1-8f11d90/`。

- `manifest.json`：
  `sha256:faa125a5df8e6703407fb0f0be95a83d89aade73d432cf4ce7127c38696c5de8`；
- `report.json`：
  `sha256:e1f2936557ee7f59837ddf5706b6c869cae124d372b1d656340c6c531fd0d993`；
- `MANIFEST.sha256`：`sha256sum -c` 实际通过；
- Review ZIP 使用显式公开 allowlist、固定时间/权限和逐成员 SHA；最终文件名、
  外层 SHA 与成员验证结果随交付输出，不写入自身所包含的报告以避免循环摘要。

## 三方能力矩阵

| 能力 | main / Industry 只读规格 | 当前 Product 选择 | 结果 |
| --- | --- | --- | --- |
| 加密 Query History、正文重新鉴权、重启恢复 | 不是旧 Trace 的默认正文合同 | `KEEP_CURRENT` | History 继续保存请求、加密问题/答案、引用、usage 和 canonical 终态 |
| Trace root/span/store/candidate/artifact | legacy QueryService 已有；Industry 补过旧库迁移 | `ADAPT_TO_PRODUCT` | 由 Product coordinator 接入当前 SDK/Application 链，不恢复旧 QueryService |
| 单 writer、有界队列、fail-soft | main/Industry 同源 | `WIRE_EXISTING` | SAFE/DIAGNOSTIC 捕获失败标 incomplete；FULL 预检失败 fail closed |
| Trace 明文问题、Prompt 和模型输出 | legacy FULL 可保存明文 | `SAFER_EQUIVALENT` | 正文只走加密 History；Trace Artifact 不保存问题、回答、Prompt 或思考 |
| 候选漏斗、Provider 子调用与阶段时序 | 旧查询链埋点较完整 | `RESTORE_BEHAVIOR` | 在当前 RetrievalService、SDK、P09 hooks 和 ingestion event 上形成层级记录 |
| 旧 flat events | main/Industry/Product 均有 | `KEEP_CURRENT` | 继续兼容；缺层级历史标 `legacy_flat_events/capture_complete=false` |
| 管理 Trace API | legacy admin bearer 与旧路由 | `ADAPT_TO_PRODUCT` | Session/CSRF 或 scoped API token、project/KB 重鉴权、`no-store` |
| 调试 UI | 旧静态 `/debug/` | `RESTORE_BEHAVIOR` | React `/operational-traces`，并从 Query/History/Job/Document/Revision 跳转 |
| Trace backup/recovery | 独立 Store/TTL | `ADAPT_TO_PRODUCT` | 独立 Product Trace DB 纳入备份，恢复遗留 RUNNING 为 INTERRUPTED |
| 远程 OCR | 当前 Product 已有预算、批准、缓存和新 Revision | `KEEP_CURRENT` | 保留，不进行真实出网调用 |
| 本地 PaddleOCR 风格服务 | Industry 有固定 revision、Bearer、限额、bbox/confidence | `ADAPT_TO_PRODUCT` | 经当前 OCR Port 增加可选本地 adapter；普通 Product 不强制安装 GPU/Paddle |
| OCR bbox/confidence | 各路径字段不统一 | `SAFER_EQUIVALENT` | 只保存 Provider 实际返回值，缺失保持 `None`，不制造零框 |
| OCR cache identity | 旧键不足以区分 adapter 配置 | `ADAPT_TO_PRODUCT` | 迁移 0022 将 adapter/provider/config/revision/model/policy 纳入身份 |
| 图关系 | 无可直接搬回的完整 Product 合同 | `SAFER_EQUIVALENT` | candidate/review/occurrence/新 Revision；未接纳关系不能进入回答证据 |
| Industry 部署 journal/last-good/sidecar | 服务器正式发布用途 | `INDUSTRY_DEPLOYMENT_ONLY` | 不强加给本地开发身份门 |
| live OCR/视觉质量 | 旧环境证据不可移植 | `BLOCKED_EXTERNAL` | 合同和合成消费者可测；真实识别/语义质量未声明 |
| 固定 Chunk B0/B1 | 旧 evaluator 会为每个 variant 重建 | `SAFER_EQUIVALENT` | build-once、同 digest、三 Trace 模式、cold/warm；B0 失败收据不可回算 |

V3-02 明确要求的 Trace 项均已选择 `KEEP_CURRENT`、`WIRE_EXISTING`、
`ADAPT_TO_PRODUCT`、`RESTORE_BEHAVIOR` 或 `SAFER_EQUIVALENT`，没有以
`UNVERIFIED` 代替实现。只有依赖真实外部视觉模型的质量证据保持
`BLOCKED_EXTERNAL`。

## Trace ADR：双存储、单公开身份

### 决策

采用一个公开 `trace_id`、两个职责清晰的持久端：

```text
Product query / ingestion
  └─ ProductTraceCoordinator
       ├─ ProductQueryHistory
       │    请求、加密正文、引用、Provider usage、用户审计终态
       └─ Operational Trace
            root、span、candidate、Provider child、Artifact metadata
```

Operational Trace 使用 Product data root 下独立的 `product-traces.sqlite3`；
History 和控制面继续使用当前 Product SQLite。选择独立 Trace DB 是为了隔离高频
诊断写入、TTL/prune 与正文事务，避免把技术查询列表变成正文读取旁路。两个 Store
共享公开 ID、scope、active revision 和终态，但由 coordinator 保证只结算一次。

### 一致性与失败语义

1. Query 先建立权威 History，再开始 Operational Trace。History 开始失败时不
   假装请求已记录。
2. SAFE/DIAGNOSTIC Trace 开始或写入失败时，查询可以继续；coordinator 记录
   `TRACE_CAPTURE_FAILED`、`capture_complete=false` 和 dropped 计数。
3. FULL 在查询前检查 writer、Store、磁盘和 Artifact 预留；失败即拒绝执行。
4. Query 原错误、取消或拒答优先保留；Trace 结算错误不能覆盖原业务终态。
5. 重启按各自合同恢复未完成状态；close 先停止新任务、排空/关闭 Trace writer，
   再关闭 Store 与其他运行资源。

### Store、迁移与导出

- schema v2 支持 Product scope、`INTERRUPTED`、capture incomplete、dropped count、
  queue high-water、独立 score type 和 Provider child span；旧 32 hex 与
  `trace_<32 hex>` 都可读。
- 有界单 writer、外键、唯一 sequence、WAL、busy timeout、0600、拒绝 symlink；
  空库、重复初始化、旧表迁移、部分迁移失败、旧记录读取和 prune 均有回归。
- SAFE/DIAGNOSTIC 默认 30 天，FULL 默认 72 小时；导出读取租约保护正在导出的
  Trace 不被 prune。
- 单条导出为 canonical JSON；批量 ZIP 固定顺序、时间、权限、manifest、成员
  SHA 和总量上限。Artifact 惰性读取并重新校验完整性和来源权限。
- Product backup manifest 纳入独立 Trace DB；恢复先验证成员与 hash，再进入迁移
  和未完成状态恢复。

详细、持续维护的接口与边界见 `design/public/trace-observability.md`。

## 默认 Product 查询与 ingestion 埋点

Query 共享链覆盖 admission/auth 摘要、scope/snapshot、history/context、analysis/
normalization、rewrite、cache/singleflight、exact/lexical/dense、fusion/hydration、
intent/plan、rerank、neighbor、evidence、confidence/refusal、generation、claim/
citation validation、repair、publish 和 History 结算。每个真实 Provider 调用是所属
阶段的 child span，记录 operation、provider/model、attempt、actual/unknown usage、
实际 elapsed、fallback/circuit reason；不保存请求正文或 Secret。

Ingestion 在既有 Job event 上形成 upload/spool、format、DOC 转换或原生 DOCX、
parse、IR validation、media enrichment、chunk、embedding、lexical/vector persist、
revision validation、activation、failure/cancel/retry 和资源释放的层级时间线。阶段
结束与业务 outcome 分开，retry 新建 attempt，不覆盖旧失败。

修复提交 `231fee2` 专门纠正 Provider child span 的时长裁剪：父阶段允许使用历史
持续时间恢复，但 child span 必须保存 Provider 实际报告的 elapsed，不能被父阶段
的历史时长截断。专项回归覆盖 cache hit、拒答、取消和三个 Provider 子调用。

## 权限、OpenAPI 与 React 使用

当前 Product 管理接口使用 `/api/v1/admin/operational-traces`：

- 管理员 Session 服从同源与 CSRF；
- API Token 分别要求 `trace:summary`、`trace:detail`、`trace:full`、
  `trace:export`，并绑定 project/KB；
- query token 不能访问管理列表、FULL Artifact 或批量导出；
- list 默认不返回问题/答案正文；Artifact 与导出在读取时重新验证 scope、来源
  是否仍存在及撤权状态；
- 全部响应 `no-store`、`nosniff`，业务文本不按 HTML 渲染。

React 管理员进入“运行追踪”或直接打开 `/operational-traces`：

1. 可按 trace/job/document/revision ID、时间、kind、status、capture mode、是否
   complete、错误/拒答和 feedback 过滤与分页。
2. 详情按需加载 root、waterfall、candidate funnel、Provider/usage 和 Artifact
   metadata；Artifact 只有点击后才读取。
3. 单条导出 canonical JSON，勾选多条可导出稳定 ZIP。
4. Query、History、失败 Job、Document 和 Revision 页面提供双向跳转；大 Trace
   采用分页、stage 过滤和有界展开。

`docs/public/openapi-v1.json` 和 `frontend/src/api/schema.d.ts` 已由同一 schema
生成；legacy `/api/admin/traces*` 与旧静态 `/debug/` 不再是默认 Product 入口。

## OCR、图关系与证据发布政策

### 四类来源

| source kind | 含义 | 可否直接当原文 |
| --- | --- | --- |
| `native_text` | Word/OOXML 可证明文本与 SourceSpan | 可以，仍需正常引用与权限 |
| `ocr_text` | 指定媒体 occurrence 的 OCR 行/块 | 不冒充原生 Word；以 OCR 来源引用 |
| `diagram_relation` | 已发布的结构关系 | 仅接纳且进入新 Revision 后可用于回答 |
| `derived_caption_or_association` | 图注、caption 或派生关联 | 单独引用，不能替代 OCR/连线事实 |

本地与远程 OCR 统一为行级 text、可选 confidence/bbox、media SHA、occurrence、
adapter/provider/config/model/revision/policy 身份。远程或本地未实际返回 bbox/
confidence 时保持 `None`。本地 adapter 使用固定 revision、Bearer、输入字节/像素、
并发、超时、redirect、hash/type 和 ready/live 边界；未配置时明确 unavailable，
普通 Product 不安装或启动 Paddle/GPU 服务。

图片解码、压缩炸弹、EMF/WMF 适配和不可取消超时继续遵守有界资源合同；超时但
尚未结束的工作保持占槽，不把线程提前释放成可超售容量。主应用不获取 Docker
socket、privileged、宿主根目录或 Secret 目录。

图关系优先读取可证明的 Drawing/SmartArt/connector 信息；否则视觉模型只能产生
带 source/target、type/direction、figure occurrence、media SHA、model/policy、
定位、ambiguity 和 review state 的 candidate。断线、交叉、侧箭头、同框和方向
不明保持 unknown/ambiguous，不能靠 OCR 名称或企业常识补层级。

只有人工接纳或满足版本化发布政策的 relation 才生成新 Revision，并以
`diagram_relation` 进入可检索/可引用证据。仅改变真实连线时，旧 Revision 和
缓存不被原地污染；改图、改注、删除、撤权或换 KB 会使相关 OCR/关系/回答缓存与
Artifact 失效。原图预览/放大仍走 server-controlled URL 和重新鉴权。

本阶段的公开合成回归证明消费者合同、来源分离、review/new Revision 和缓存失效
行为；没有真实视觉 Provider 调用，因而不声明 OCR 识别率或图关系语义质量。

## 固定 Chunk B0/B1 方法与结果

### 冻结方法

V3-02C 不修改 Parser、`docx-structural-v3`、ChunkingPolicy 或查询策略。runner
只构建一次 52-case 公开合成语料，冻结每个文档、Chunk 内容与 SourceSpan 摘要、
active Revision、embedding slot/model/dimension、profile、retrieval/generation
policy、index/serving fingerprint、依赖锁、解释器、硬件和进程身份。随后在同一
Chunk digest 上执行：

- `fts5-only` 当前 Production Retrieval 的全部 52 case；
- exact、正文列举、多级列表、表格、口语/同义、数字/单位/否定/例外、无答案、
  OCR-only 和 accepted-relation consumer 九个公开切片；
- SAFE、DIAGNOSTIC、FULL × cold/warm 共 54 次 Trace consumer 查询；
- 质量签名、Provider 调用量、Revision、profile、Chunk digest 跨模式必须完全
  一致；SAFE 不允许 Artifact 或同步大对象读取。

离线 deterministic 路径不调用外部服务，OCR-only/relation 只测已知公开结构的
消费者，不模拟模型成功，也不形成视觉质量声明。P50/P95 是本机单进程小样本的
工程基线，不外推为生产容量或与 main/Industry 的数值性能比较。

### B0 与上游 Evaluation V3

B0 绑定进入点 `0505fac`，实际使用只刷新固定标签的 `5505e8b` 执行。该查询链
在候选选择阶段明确失败，留下 `state=failed` 收据；由于 02C runner 不是进入点
源码的一部分，不允许在 B1 代码上反向回算一个伪 B0。B1 的既有全候选选择也在
同一门失败。源码三方 diff 已确认固定标签提交到 V3-02B 之间的语料、Profile、
RetrievalService、store 和 SDK 查询链无差异，因此该失败不是 Trace 引入回归，
但也不能写成 `PASS` 或 `NOT_RUN`。

固定对象共有 46 个 chunks，digest 为
`sha256:f0f5f6188b082844ff35ddc73cbe94c5d609cf15517214490c8b1fb6b1a13c34`；
一次构建耗时 4755.121 ms。52 次 Production Retrieval 与 54 次 Trace consumer
均为 `external_service_call_count=0`、`provider_call_count=0`。

| B1 Production Retrieval（fts5-only，52 case） | 结果 |
| --- | ---: |
| Fusion Recall@5 | 1.000000 |
| Citation chunk precision | 0.511905 |
| Source range precision / recall / F1 | 0.906250 / 0.591837 / 0.716049 |
| Answerable accuracy | 0.634615 |
| Refusal F1 | 0.512821 |
| Irrelevant evidence count | 3 |
| 质量状态 | `FAIL` |

SAFE、DIAGNOSTIC、FULL × cold/warm 的九切片质量签名完全相同：Evidence
Recall@5、Citation accuracy、Answer accuracy 均为 0.375；错误拒答率 0.625，
不可答错误作答率 0。三种模式 Provider 调用数完全相等且均为 0，Trace dropped
均为 0。

| 模式 | cache | 总时延 P50/P95 ms | 直接捕获 P50/P95 ms | 相对 SAFE P50/P95 ms | 逻辑存储 bytes | Artifact |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| SAFE | cold | 9.292 / 18.130 | 0.454 / 0.604 | 0 / 0 | 50,862 | 0 |
| SAFE | warm | 2.279 / 3.565 | 0.281 / 0.297 | 0 / 0 | 32,133 | 0 |
| DIAGNOSTIC | cold | 8.897 / 18.527 | 0.376 / 0.597 | -0.394 / +0.396 | 55,708 | 0 |
| DIAGNOSTIC | warm | 2.505 / 3.558 | 0.285 / 0.388 | +0.226 / -0.007 | 32,187 | 0 |
| FULL | cold | 75.976 / 96.695 | 64.750 / 82.922 | +66.684 / +78.565 | 73,839 | 9 / 15,070 B |
| FULL | warm | 39.995 / 46.714 | 37.486 / 43.266 | +37.715 / +43.148 | 39,163 | 9 / 3,916 B |

逻辑存储由 canonical export 的实际内容字节计算，不使用 SQLite page allocation
冒充单次写入量。DIAGNOSTIC 的小幅负差属于单机九样本抖动，不代表性能提升；
FULL 包含显式调试 Artifact 成本。该 consumer 微基线没有 Product HTTP no-trace
控制组、首协议事件、首条已验证内容或取消后无效工作量，故
`TRACE_CAPTURE_PERFORMANCE=PARTIAL`，不能外推生产容量。

B0 与 B1 上游 `scripts/dev.py eval-run` 都实际退出 1，错误为“没有候选通过
Evaluation V3 安全与精度门”，冻结失败收据 SHA 均为
`sha256:a489ade2af1c65d9b9ed9aa27fc4f421d34468323edb4c51c3d0d0e5bc2db528`。
B0 不存在可比的数值报告，因此不回算；B1 bundle 的 `status=FAIL` 如实保留。

质量未通过的直接输入冻结给 V3-03；V3-02 不调整全局 chunk size、overlap、阈值，
不恢复固定字符切片，也不关闭 Trace 来制造通过。

## DEV_RUNTIME_IDENTITY_GATE

`scripts/dev.py runtime-identity` 用一条命令输出稳定 JSON，并核对：Git HEAD、可选
build context/source revision、OCI revision label、容器 image ID、运行时公开
build revision、profile/index/serving fingerprint、前端兼容身份和当前 OpenAPI
SHA-256。source-tree 开发未构建镜像时，相应字段明确为 `NOT_RUN`/
`NOT_APPLICABLE`，不能冒充 image gate。

最终文档提交后在 clean tracked HEAD 执行：

```text
.venv/bin/python scripts/dev.py runtime-identity \
  --profile configs/profiles/dev-offline.json \
  --output-json artifacts/v3-02/dev-runtime-identity.json
```

结果为 `overall_status=PASS`、`network_calls=0`。Git/source-tree、Profile、index/
serving fingerprint、前端 compatibility identity 与 OpenAPI SHA 均完成核对；本阶段
未构建镜像或启动候选容器，因此 build context、OCI label、container image ID 与
运行时公开 build revision 明确为 `NOT_RUN`/`NOT_APPLICABLE`，没有伪装成镜像门。

这不是 V3-04 `FINAL_RELEASE_IDENTITY_GATE`：未运行 Industry 服务器 package state
machine、未重建最终候选镜像，也未复用 V3-01 旧 Trivy/人工批准。

## Migration、重启、缓存与索引范围

- Trace：独立 Store schema v2 兼容旧 flat event/32 hex ID，启动恢复 RUNNING；
  Product backup/restore 增加 Trace DB。无 Document/Chunk/Embedding 重建。
- OCR：migration 0022 版本化 adapter identity/cache/source 字段；配置或模型身份
  改变只失效相应 OCR 缓存，并在正式发布 OCR 内容时新建 Revision。
- 图关系：migration 0023 保存 candidate/review/published revision；candidate 审核
  不改 active index，接纳后才构建新 Revision并失效关联缓存。
- Source 删除、撤权、改图、改注、换 KB 后，Trace Artifact 读取重新鉴权；相关
  OCR/关系/回答缓存失效。技术 hash 可按 TTL 保留，但不继续公开受保护正文。
- Parser/Chunker 有效语义未变，当前普通段落、列表、长表、figure/child/group、
  SourceSpan 和主备向量空间不重建。

## 实际门禁

- `.venv/bin/python scripts/dev.py check`：退出 0；compileall、Ruff、mypy
  （369 source files）、Google docstring（0 missing）通过；离线 pytest 为
  `2717 passed, 89 deselected, 4 warnings in 745.41s`，并确认进程自然退出。
- `.venv/bin/python scripts/dev.py doctor`：PASS；Python 3.11.15、source-tree import、
  SQLite FTS5 与临时目录正常，Node 属于该命令声明的 later-phase optional skip。
- `dev.py smoke`：`72 passed, 1 warning`；`dev.py product-check`：hardcode audit
  通过且 `74 passed, 1 warning`；`dev.py product-smoke`：`6 passed, 1 warning`。
- OpenAPI 重新生成 57 paths 且 tracked diff 为空；前端 `api:check`、lint、typecheck、
  18 files/74 tests 和 Vite build（1853 modules）均通过。
- 真实 Chromium Playwright：`9 passed, 3 skipped in 29.3s`；3 个 skip 是 desktop/
  mobile viewport 互斥分工，两个 viewport 的 Operational Trace 用例均通过。
- 两个仅绑定 `127.0.0.1:6333/6334` 的临时 `qdrant/qdrant:v1.18.3`：Qdrant
  双槽/快照、Product backup/restore、真实 BuildKit 共 `3 passed, 7 warnings`；
  容器与临时 Key 均已移除。
- 首次 loopback 恢复测试真实发现 Trace SQLite 被恢复成非 0600；修复后相关离线
  备份套件 `41 passed`，同一 loopback 门重跑通过。另一次完整门发现无效 OCR
  配置会泄漏两个非守护 Trace writer；新增线程计数回归后专项 `2 passed`，最终
  完整门正常退出。两项均未用 skip、超时或缩小范围掩盖。
- Product migration inventory 为 23；0024 failure injection、旧库/空库/重复启动、
  restart、backup/restore 均由完整门或 loopback 门覆盖。
- `git diff --check`、staged release safety、Review allowlist/成员 Secret scan、ZIP
  CRC/路径/顺序/manifest/SHA 与外层 SHA 在封存时通过。

`check_release_safety.py repository .` 不是本阶段成功门禁：历史 tracked tree 含既有
文档、DOCX fixture 和内网示例，会稳定报告旧存量；本阶段使用 staged 安全检查、
Review allowlist 和逐成员 Secret scan。V3-01 最终候选镜像的 High/Critical 无可用
修复元组与人工审查缺口仍是发布阻塞，未降低阈值或伪造批准。

## 本地提交与下一阶段入口

本地提交按功能意图拆分：

- `c9184c9`：V3-02A Product Operational Trace；
- `231fee2`：Provider child elapsed 修复；
- `adc0334`：V3-02B OCR/图关系证据合同；
- `90a6c77`：DEV runtime identity；
- `2dc0f1d`、`265881d`、`bdbfcef`、`8f11d90`：V3-02C 固定 Chunk、缓存质量、
  直接捕获开销和逻辑存储统计；
- `c61d806`、`abed1ac`：接口文档与 migration/来源快照回归；
- `6dc52c8`：备份恢复 SQLite 私有权限；
- `6aa6b25`：无效 OCR 配置的 Trace writer 生命周期；
- 本报告与持续 Trace 契约由最终文档提交封存；准确 SHA 见最终 Git/identity 输出。

`V3_03_ENTRY=PASS` 只表示默认 Product Trace 核心闭环与 V3-02 本地门已完成。
Production Retrieval、九切片质量和上游 Evaluation V3 的 `FAIL` 必须原样进入
V3-03 查询链恢复；V3-03/V3-04 在本阶段均未开始。V3-01 release security 仍
`BLOCKED`，因此不存在生产发布批准。
