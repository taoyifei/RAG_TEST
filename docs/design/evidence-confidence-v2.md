# Evidence and Confidence V2

## 数据流

`RetrievalService` 把规范 Query、Query kind、候选通道、Fusion/Rerank 结果、
实际路由和安全诊断传给 Evidence V2。Evidence 不再仅按文档多样性重排；它先
判定相关性，再在合格候选内部保持 Rerank 顺序并应用去重和预算。

Evidence item 必须来自实际入选 Chunk 的 canonical `SourceSpan`。相同 Chunk
默认最多发布一个 Span，Source identity 去重后再应用总 item、文档、Section
和 token 上限。表格或数值 Query 优先保留包含 Query 单位或数值的 Span。
多样性不能把低相关文档提前。

## 相关性边界

Exact phrase、identifier、CJK token 和表格/数值重合构成可审计的 lexical
support。候选还必须达到同批最高相关性分数的受控相对下限。只有显式声明
semantic score 已校准、且与当前 Provider/向量空间 identity 绑定的受控路径，
才允许 lexical overlap 为零的 dense-only Evidence。

## Confidence V2

Confidence 只读取最终 Evidence 对应候选的支持信息，不再用未发布的 Rerank
候选抬高置信度。支持数按唯一 Chunk 计算，多个 Span 不能重复充数。

- Exact 或 Lexical 支持达到策略下限时可以进入回答判定；
- 未校准 dense-only 路径必须拒答；
- 测试专用的 calibrated fake 路径在 identity、score 和 policy 都满足时可回答；
- 冲突、来源无效、支持不足或预算失真继续 fail closed。

## Pre-provider Result Cache

Cache key 在 snapshot、Query analysis 和 plan 确定后生成，并绑定 project、KB、
active Revision、index/serving fingerprint、规范 Query、conversation identity、
rewrite policy identity 和 cache schema。查找发生在 Query Embedding、Dense
Provider 和 Reranker 之前。

可回答结果默认 TTL 300 秒；稳定的 insufficient result 默认 TTL 30 秒；
通道损坏、Provider 错误和其他不稳定状态不缓存。Cache hit 生成新 trace ID，
显式设置 `cache_hit=true`，实际 provider/embedding/reranker call 均为零。

## 异常分类

`ChannelUnavailable` 与 `ChannelRateLimited` 可以触发策略允许的通道降级。
SQLite corruption、Schema 错误、malformed canonical JSON、普通 RuntimeError
和编程错误必须 fail closed；不得用空结果掩盖数据损坏。
