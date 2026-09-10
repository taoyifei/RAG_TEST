# Product Operational Trace v2 可观测契约

Operational Trace v2 服务于当前默认 `rag-app serve`、Product API、SDK 和
React 控制台。它按同一个公开 `trace_id` 与 `ProductQueryHistory` 关联，但不
参与检索、排序、生成、readiness 或评测判定，也不记录或推断模型隐藏思维过程。

History 与 Operational Trace 的职责不同：History 保存用户可见请求、终态、
加密问题/答案、引用和 Provider usage；Operational Trace 保存技术身份、层级
span、候选漏斗、Provider 子调用和有限调试制品。Trace 不重复结算请求，不替代
History 的正文保留与重新鉴权策略。

## 内容与失败边界

- `SAFE` 是默认模式，只保存版本身份、问题摘要、父子 span、独立耗时、阶段
  计数、终态、稳定 reason code 和 Provider operation/model；不保存问题、回答、
  证据、Prompt、原始 delta、Secret、Cookie、向量或模型思考。
- `DIAGNOSTIC` 增加候选 ID、通道 rank、分数类型、RRF contribution、rerank
  score 和淘汰原因；仍不保存正文或 Prompt。
- `FULL` 只接受显式管理员调试请求。执行前先检查 Store、writer queue、磁盘和
  1 MiB Artifact 预留容量；准入失败时 fail closed，不执行查询。正文优先通过
  加密 History 读取，Trace Artifact 不独立捕获问题、回答、整份文档或 Prompt。

所有模式都禁止 Authorization、API key、Cookie、未经净化的异常、原始
embedding、图片二进制、OCR base64 和模型思考。Artifact 使用 zlib 压缩并保存
SHA-256、media type、原始/压缩字节数；读取时复核完整性，不截断后声称完整。

普通 `SAFE`/`DIAGNOSTIC` 捕获失败不能改变正常查询结果：记录
`capture_complete=false`、稳定失败码和 dropped/high-water 计数。`FULL` 的开始
或最终持久化失败按严格模式返回安全错误。根终态支持 `ANSWERED`、`REFUSED`、
`FAILED`、`CANCELLED`、`INTERRUPTED` 和 ingestion 的 `SUCCEEDED`；重复 finish
保持幂等。启动时将遗留 `RUNNING` root/span 恢复为 `INTERRUPTED`。

## 持久化、保留与恢复

Trace 使用 Product data root 下独立的 `product-traces.sqlite3`，不与 Query
History 正文、任务数据库或评测证据混写。数据库采用受控目录、拒绝 symlink、
0600、WAL、`synchronous=FULL`、外键、唯一 sequence 和有界分页。单 writer
队列默认容量 256；span、candidate、stage decision、Artifact 和批量导出均有
硬上限，超限记录摘要而非无限增长或阻塞查询。

`SAFE`/`DIAGNOSTIC` 根 Trace 默认保留 30 天，`FULL` 默认保留 72 小时；History
正文与 Trace Artifact 按各自策略独立保留。prune 是幂等事务，活动导出持有读取
租约，不会与清理交错。Product 备份包含独立 Trace 数据库；恢复先校验 manifest、
成员 hash 与目标安全性，再在冷启动迁移和恢复未完成状态。

旧平面 Job/History event 继续可读。没有层级信息的旧记录显示为
`legacy_flat_events` 且 `capture_complete=false`，不会伪造历史 span。Trace schema
通过 Product migration 独立升级；部分迁移失败会阻止 Store 被当作健康实例使用。

## 身份、Span 与候选漏斗

Product 公共 ID 继续使用 `trace_<32 hex>`，Store 同时兼容旧 `<32 hex>` 读取。
根身份绑定 query/ingestion kind、project/KB、owner 摘要、request/job/document/
revision、profile、index/serving/pipeline fingerprint 和 source revision。

当前共享执行链记录 admission、snapshot/context、analysis/normalization、rewrite、
cache/singleflight、exact/lexical/dense、fusion/hydration、plan、rerank、neighbor、
evidence、confidence/abstention、generation、claim/citation validation、repair、publish
和 History 结算。Ingestion 记录 upload/spool、format、DOC 转换或原生 DOCX、parse、
IR、媒体增强、chunk、embedding、lexical/vector persist、revision validation、
activation、cancel/retry 和资源释放。

每次真实 Provider 调用是所属阶段的 child span，独立记录 operation、provider、
model、attempt、实际 elapsed、status、usage 的 actual/unknown 和稳定 fallback/
circuit reason。候选决策区分 exact/lexical/dense rank、score type、RRF contribution、
rerank、evidence 和 drop reason；同一数值字段不混用不同分数语义。

本地 Span 可映射到 OpenTelemetry，但默认不安装 OTel SDK 或部署 Phoenix：

| 本地 SpanKind | 含义 | 可选 OTel 映射 |
| --- | --- | --- |
| `CHAIN` / `HTTP` | 根链、准入和编排 | server/internal span |
| `RETRIEVER` / `EMBEDDING` | 检索与向量调用 | retriever/client span |
| `RERANKER` / `LLM` | 重排和生成调用 | model client span |
| `GUARDRAIL` | 证据、校验、拒答和发布 | internal span |
| `STORAGE` | Trace、History 和缓存写入 | storage client span |

可选 exporter 失败不影响本地 Store 或查询语义；外部观测系统也不替代本项目的
reason code、候选表或安全 Artifact 合同。

## API、权限与 React

当前 Product 路由为：

- `GET /api/v1/admin/operational-traces`：有界分页和身份/状态/模式过滤；
- `GET /api/v1/admin/operational-traces/{trace_id}`：根、waterfall、候选、Provider
  和 Artifact metadata；
- `GET .../{trace_id}/artifacts/{artifact_id}`：惰性读取并重新鉴权；
- `GET .../{trace_id}/export`：单条 canonical JSON；
- `POST /api/v1/admin/operational-traces:export`：稳定排序、manifest 和 SHA-256 的
  有界 ZIP；
- `POST /api/v1/admin/operational-traces:prune`：仅管理员会话。

管理员 Session 服从同源与 CSRF；API Token 分别要求 `trace:summary`、
`trace:detail`、`trace:full`、`trace:export`，并按绑定的 project/KB 重新校验范围。
普通 query token 没有 Trace 管理能力。响应统一 `Cache-Control: no-store` 和
`X-Content-Type-Options: nosniff`；来源删除、撤权或 scope 不匹配后不再返回受
保护 Artifact 或导出。

React 的 `/operational-traces` 是默认管理页面，不恢复 legacy `/debug/`：支持
列表、过滤、分页、capture completeness、waterfall、candidate funnel、Provider/
usage、惰性 Artifact、单条/批量导出，以及 Query、History、Job、Document 和
Revision 的双向跳转。大 Trace 采用有界展开，业务值按文本渲染，不使用 HTML
注入。旧 `/api/admin/traces*` 与静态 debug 页面仅是 legacy 规格来源，不是当前
Product 默认入口。

## 多模态 Trace 边界

媒体事件明确区分 `native_text`、`ocr_text`、`diagram_relation` 和
`derived_caption_or_association`。本地/远程 OCR 共用结果合同；Provider 未返回
bbox/confidence 时保持 `None`。视觉关系首先是带 occurrence、media SHA、模型/
策略和 review state 的 candidate，只有版本化策略接纳后才能成为回答证据。
OCR 文本正确不证明连线、方向或层级正确，Trace 和 UI 不能把待审 candidate
冒充 Word 原文或正式关系。
