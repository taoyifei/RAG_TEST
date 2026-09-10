# 检索与问答

Product API 接受绑定 Project 与知识库的查询，返回本次冻结的活动 Revision、索引与
服务指纹、实际检索和模型数据面、类型化问题语义、引用、回答或拒答原因，以及可用于
History/Operational Trace 对账的 `trace_id`。同步与 `rag-answer-sse-v1` 流式接口共享
同一最终结果合同。

## 查询编排

每次请求先保留原问并结合有界 Conversation 上下文生成 `QueryAnalysis`。规则可以直接
识别定义、目的、职责、责任角色、事实、枚举、数量、序号、流程和章节摘要；标识符、
数字、单位、否定、来源限定和日期/版本作为硬约束保存。规则不确定时才尝试一次受控
`query.interpret` 或 retrieval-only rewrite；模型输出必须通过硬约束校验，且最多增加
一个查询变体。`UNKNOWN` 表示需要解释或更多上下文，不等价于“资料中没有答案”。

检索按配置使用 Exact、FTS、结构事实与 Dense 通道，经 RRF、可用的 Reranker 和邻居
展开后再构建 Evidence。`DENSE_UNAVAILABLE` 表示查询向量或所选向量空间不可用，不表示
知识库为空；Exact、FTS 和结构通道仍可继续。`INDEX_NOT_READY` 表示没有活动不可变
Revision；`INDEX_CORRUPT` 表示持久化通道身份无法从 canonical Chunk Store 回读，此时
失败关闭。

召回候选与答案证据是两个边界。系统允许保留有界相关候选，但只从中选择最小充分支持
集；一条完整正确证据不会再因为同批存在无关候选而被整体否决。每个已发布事实必须绑定
可回读的 canonical `SourceSpan`，模型改写、相关内容预览和 Provider 返回文本都不能自行
成为证据。

## 回答与终态

证据闭合时，本地结构化 renderer 可以在没有外部模型的情况下回答定义、目的、职责、
责任角色、枚举、数量、序号、流程和章节摘要，并保留表格行列、列表顺序、数字、单位、
否定与版本原词。配置和授权有效时，Grounded LLM 只接收最小支持集；生成结果仍要经过
claim、support ID、引用、对象、数字、单位、否定、数量和版本校验。

模型未配置、未授权、预算不足、超时或返回无效结构时，只要支持集完整，就发布
`generation_mode=extractive_fallback`，同时保留 `generation_reason_code` 与
`degraded_reason_codes`。支持集本身不完整时才返回拒答或澄清。响应层主要状态包括：

- `ANSWERABLE`：已发布经过验证的本地、fallback 或 LLM 回答；
- `INSUFFICIENT_EVIDENCE` / `AMBIGUOUS_NEEDS_CLARIFICATION`：来源不足或真实歧义；
- `CONFIGURATION_REQUIRED` / `BUDGET_BLOCKED` / `POLICY_DENIED`：能力被配置、预算或策略阻断；
- `PROVIDER_UNAVAILABLE`：已授权 Provider 本次不可用；
- `INDEX_NOT_READY` / `INDEX_CORRUPT`：索引生命周期或完整性不满足查询条件。

已发布 fallback 在 History 与 Operational Trace 中都结算为 `ANSWERED`；无答案的正常
拒答结算为 `REFUSED`；内部或持久化错误才是 `FAILED`。取消与重启恢复分别保留
`CANCELLED` / `INTERRUPTED`，不会被改写成资料无答案。

## 实际数据面

`data_plane` 取自本次冻结的运行时状态，而不是页面选择项。它包含活动 Retrieval
Profile/Index Revision、索引和服务指纹、实际 Embedding/向量空间、Reranker、
generation/interpret/rewrite 模型、Dense 校准、模型配置、语料授权、预算与降级原因。

没有活动 Profile 时返回 `retrieval_data_plane=default_local_fallback`，即使本地
deterministic embedding 存在，也不会显示成真实远程 Dense。Profile 与 Revision 不匹配、
向量覆盖不全、验证过期、校准缺失或重建未完成都会给出独立状态和修复入口。远程模型
配置存在但活动语料清单未批准或已失效时，本地可答路径仍工作，外部调用数为 0。

## 缓存、singleflight 与相关内容

进程内结果缓存键绑定 owner/scope、活动 Revision、Retrieval Profile、serving/semantic
版本、访问过滤、Conversation、原问与已接受语义、输出行为和 Trace 无关结果；不保存
原始 query 明文。非流式 SAFE 等价请求可共享 singleflight 计算，但各自仍有独立 History
和 Trace。流式、DIAGNOSTIC、FULL 或不同身份、Revision、会话的请求不合并。

`include_related_content=true` 最多返回三条已授权 canonical 原文预览，每条最多 240
字符。它们与 Evidence、答案分离，不能送入生成器或冒充答案。定向原文读取会重新检查
当前活动 Revision、文档删除状态和管理员权限；浏览器只按纯文本展示，不持久保存正文。

公开回归、阈值与不能外推的边界见
[评测与质量声明](evaluation-and-quality-claims.md)，模型与语料授权操作见
[配置模型服务](model-services.md)。
