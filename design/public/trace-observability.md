# Query Trace v1 可观测契约

Query Trace v1 用于管理员按 `trace_id` 复盘查询流水，不参与检索、排序、生成、
readiness 或生产评测。它记录可观察输入输出、分数、阈值、稳定 reason code 和
确定性校验结果，不记录或推断模型隐藏思维过程。

## 内容边界

普通 `/api/chat` 由运维配置为 `SAFE` 或 `DIAGNOSTIC`，query token 不能选择
`FULL`：

- `SAFE` 保存版本身份、父子 span、独立耗时、阶段计数、终态和非敏感失败码；
- `DIAGNOSTIC` 另保存问题/历史/resolved query 的 SHA256 与 token 数，以及候选
  的通道 rank、同通道 raw score、RRF contribution、rerank score、reason；
- `FULL` 只由 `/api/admin/debug/chat` 开启，保存准确 context、改写请求响应、
  候选、evidence、Prompt、原始模型输出、validation、repair 和最终回答。

`FULL` 默认保留 72 小时，其他模式保留 30 天。单 Trace 的原始 artifact 总量
上限为 5 MiB；超限停止写入新大对象并标记不完整，不截断内容冒充完整记录。
artifact 使用 zlib 压缩，同时保存 SHA256、原始字节数和压缩字节数。

所有模式禁止保存 Authorization、API key、Cookie、未经净化的异常、原始
embedding 向量、图片二进制、OCR base64 或 SQLite 密钥。候选矩阵和完整输入
输出保存在独立表；span attributes 只保存小型安全字段和 artifact 引用。

## 持久化与失败语义

`RAG_TRACE_DATABASE` 指向独立 SQLite 文件，不与任务、manifest 或评测证据库
共用。数据库使用 0600、WAL、`synchronous=FULL`、外键和有界分页。单 writer
队列串行写入，固定周期清理到期根 Trace 及级联数据。

普通查询中 Store、队列或身份读取失败只写非敏感 `TRACE_CAPTURE_FAILED`/
`TRACE_QUEUE_FULL` 审计，查询结果不变。管理员 FULL Debug 在查询提交前检查
Store 与容量，不可用时返回 503。查询中途异常会关闭活动 span，保留此前 span、
候选漏斗、外部调用和稳定 `failure_stage`。

所有 `/api/admin/traces*` 接口只接受 admin token，并返回
`Cache-Control: no-store`。artifact 必须同时匹配 trace ID；过期内容返回 410。
canonical export 只包含所请求 Trace，禁止成为生产 evaluator 的活动证据输入。

## Span 与 OpenTelemetry 映射

本轮只提供无第三方依赖的 `TraceExporter` Protocol 和默认
`NullTraceExporter`，不安装 OpenTelemetry SDK，也不部署 Phoenix。

| 本地逻辑 | SpanKind | 后续 OTel/Phoenix 映射 |
| --- | --- | --- |
| `rag.query`、context、rewrite、route、evidence | `CHAIN` | root/internal span |
| `embedding.query` | `EMBEDDING` | embedding client span |
| retrieve、Qdrant、RRF、neighbor | `RETRIEVER` | retriever/client span |
| rerank | `RERANKER` | reranker client span |
| answer、rewrite LLM、repair | `LLM` | LLM client span |
| validation、publish | `GUARDRAIL` | deterministic guardrail span |
| conversation/Trace SQLite | `STORAGE` | storage client span |

32 位小写十六进制 trace ID、16 位小写十六进制 span ID、parent、kind、status、
attributes 和 artifact SHA 均可映射到 OTel。后续可通过 OTLP 把小型 span 导出
到可选的内网 Phoenix；Phoenix 不参与 RAG readiness，不替代本项目的 decision
reason、候选表或 artifact Store，exporter 失败也不得影响本地 Store 或查询。

## 管理页面

`/debug/` 只加载本地 HTML/CSS/JS。页面展示稳定倒序列表、父子 waterfall、
候选漏斗、完整 artifact 标签、chunk 定位和机械诊断摘要。所有业务内容通过
`textContent` 或新建 DOM 文本节点展示，不使用 `innerHTML`。

expected chunk ID 只保存在当前浏览器会话：

- 未进入 recall 候选：`retrieval loss`；
- 进入 recall、未进 rerank final：`rerank loss`；
- 进入 rerank、未进 evidence：`assembly loss`；
- 进入 evidence、未引用：`generation/validation loss`。

该临时标注不会写入 SQLite、冻结集或评测可信根。诊断摘要只描述观察到的
`RETRIEVAL_EMPTY`、`RERANK_DROP`、`EVIDENCE_BUDGET_DROP`、
`PROMPT_INJECTION_ONLY`、`MODEL_UNAVAILABLE`、`VALIDATION_FAILED` 或
`ANSWERED`，没有人工标签时不声称语义根因。
