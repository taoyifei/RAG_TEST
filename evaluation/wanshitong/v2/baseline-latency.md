# WB08R-00 真实延迟基线

本页只冻结既有真实 Trace，不代表 WB08R 有性能或质量改进。原请求中的
`问题trace1(1).zip` 未在本机找到；实际读取的是
`问题trace1.zip`，SHA-256 为
`d909c7cf2f8d00b8aa20710af64b15d4dbf942329012c0a4d6b3ac7f8e477b4c`。
ZIP 的 MANIFEST 含两组 History 与 Operational Trace，内部文件摘要均核对一致。

| 真实问法 | History 总耗时 | Embedding | Reranker | Generation | 估算生成输入 | 调用 | 最终行为 |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 自然短问 | 10.519 秒 | 42 毫秒 | 613 毫秒 | 4.564 + 4.678 秒 | 6,091 + 5,801 token | Embedding 1、Reranker 1、Generation 2 | `REFUSED / INSUFFICIENT_EVIDENCE` |
| 显式长标题 | 6.294 秒 | 47 毫秒 | 615 毫秒 | 4.823 秒 | 2,638 token | Embedding 1、Reranker 1、Generation 1 | `ANSWERED / ANSWERABLE`，一条模板目录引用 |

两条 Trace 的 `interpret_reason_code` 均为 `INTERPRET_NOT_CONFIGURED`，
`REWRITE_NOT_CONFIGURED`；语义解释与改写均无 Provider 调用。
自然短问两次生成均留下 `CATALOG_CLAIM_UNSUPPORTED`，不能把第二次调用
未经核实地计成独立 Corrective Repair。显式长标题问法只引用模板目录身份，
不证明模板正文已入库。

## 指标口径

统一字段在 `schema.json` 的 `metric_record` 中定义，真实观测值与不可得值
在 `baseline-trace-analysis.json` 中并列保存。`request_total_ms` 取 History
`duration_ms`；`final_ms` 取 Operational Trace `duration_ms`，二者时钟与
起点不同，不相减归因。`generation_ms` 是 Generation Provider 调用耗时之和；
`prompt_tokens_estimated` 仅求和这些调用的估算输入，不含 Embedding/Reranker。
`exact_ms`、`lexical_ms` 取 History `stage_timings` 的通道耗时；
`embedding_ms`、`rerank_ms` 取 Provider 诊断耗时。

`vector_channel` 包含通道工作，不能等同 `dense_ms`，也不能与 Provider
Embedding 时间相加解释请求总耗时。Trace 中 `retrieval.*` 的零毫秒 span
是事件标记，不能作为检索阶段延迟。现有 `plan` 是轻量计划阶段，
`INTERPRET_NOT_CONFIGURED` 下不等同语义 Planner；因此 `planner_ms` 留空。
客户端首状态、首条已验证 Claim、实际 Completion token、Repair 专项耗时与
质量人工评分均无可核实值，统一用 `null`，不填零。

后续测量以请求开始为 `stage_status_first_ms`、`first_validated_claim_ms`
和 `final_ms` 的零点；`request_total_ms` 是服务端完整请求时长。
`exact_ms`、`lexical_ms`、`dense_ms` 分别记录各检索通道自身耗时，
`embedding_ms` 单列 Provider 耗时，避免重复计入 Dense。
`planner_calls`、`generation_calls`、`repair_calls` 是可验证的调用次数；
`null` 表示未观测，`0` 表示确认没有调用。`route_mode` 记录实际路由模式，
`cache_hit` 记录真实命中状态。

质量字段只作为后续评分合同：`answer_naturalness`、
`citation_faithfulness`、`official_term_preservation`、
`number_condition_negation_preservation`、`verbatim_copy_ratio` 的范围为
0～1；`unsupported_detail_count` 是无来源支持的细节数。
`completion_tokens` 必须来自 Provider 实际用量，不能用估算输入 token
代替。没有完成逐条质量复核前不填任何质量分数。

## WB07R 既有缓存边界

本地 `.wb07r-traces-149-a89935a.ndjson` 中 149 个 Trace ID 唯一且
`capture_complete`，对应候选 `a89935ab`：115 条 `ANSWERED`、34 条
`REFUSED`，Generation 0/1/2 次分别为 2/98/49 条。配套问答文件按
`finished_at - started_at` 得到运行器墙钟 p50 14.121 秒、p95 37.741 秒、
p99 63.146 秒；该数字不是服务端 `request_total_ms`，不能与 ZIP 两条
History 延迟合并成同一个分布。两条 ZIP Trace 的运行版本为 `9babb049`，
也不是该 `a89935ab` 候选。

当前 60 的缓存身份未核实：本地文件没有来源主机字段，经 54 跳板只读
读取 60 文件时 SSH banner exchange 超时。本轮不以本地缓存冒充 60 当前
运行态，也没有新发真实问答请求。
