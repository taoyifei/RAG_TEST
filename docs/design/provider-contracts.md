# P02 Provider 合同

## 同步传输

`ProviderHttpClient` 持有长生命周期 `httpx.Client`。默认最多三次尝试，连接、读取、写入
和连接池 timeout 分离；只对 408、429、502、503、504 及受控连接/读取错误重试。401、
403、404、400、422 不在同一 Provider 内重试。退避使用指数 full jitter，并尊重
`Retry-After` 秒值或 HTTP-date。

响应必须在字节上限内、Content-Type 为 JSON 且可解码。每次最终成功或失败只产生脱敏
`ProviderCall`，包含 Provider、operation、model、host/path、尝试次数、状态类别、耗时、
输入数和估算 Token；不包含 query string、Header、正文、向量或响应体。`close()` 幂等。

## Jina Embedding

固定 endpoint `/v1/embeddings`、模型 `jina-embeddings-v5-text-small` 和 1024 维。
DOCUMENT 映射 `retrieval.passage`，QUERY 映射 `retrieval.query`；请求使用 API `task`，不再
手工添加 `Query:` 或 `Document:`。`truncate=false`，本地超限直接拒绝。

响应 `data[].index` 必须唯一完整覆盖输入，模型身份匹配，向量为 1024 维有限非零数字。
最终统一执行 `l2-v1`。

## Jina Reranker

固定 endpoint `/v1/rerank` 和模型 `jina-reranker-v3.5`。`top_n` 等于传入候选数，即使
应用层最终只消费更少结果。`results[].index` 必须完整，`relevance_score` 或兼容 `score`
必须有限；document 回显存在时只校验索引，不写 Trace。

Provider 不可用时返回 `BYPASS_KEEP_RRF`、空 items 和
`RERANK_BYPASSED_PROVIDER_UNAVAILABLE`。空 items 表示继续使用原 RRF 顺序，不表示分数
为零，也不会调用 Qwen Embedding 冒充 Reranker。

## Qwen3.7 原生 Embedding

固定北京业务空间原生路径
`/api/v1/services/embeddings/text-embedding/text-embedding`。DOCUMENT 请求只含
`text_type=document`、1024 维和 `output_type=dense`；QUERY 使用 `text_type=query` 并
加入固定英文 instruction。兼容 OpenAI 的 endpoint 不能覆盖原生 identity。

响应要求成功 `status_code`、空错误 code、`output.embeddings` 和唯一完整
`text_index`。向量执行与 Jina 相同的有限、非零、1024 维和 `l2-v1` 校验，但两个空间
仍不可比较。

## Batch、缓存与双槽调度

默认每批最多 16 条、估算 8192 Token、131072 字符；Qwen 官方上限 20 条，因此共同默认
保持 16。单条不能装入安全批次时拒绝，不截断。跨批结果按索引恢复，合并数量必须等于
输入数。

文档协调器按 slot 顺序执行两个独立 work stream。缓存 key 包含 slot、Provider、模型、
角色、维度、规范化和 `text_sha256`。任何 slot 失败只写入该 slot 的 retryable 结果，
不会把向量放入另一 slot，也不会激活不完整 revision。
