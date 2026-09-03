# P06 持久化 Embedding 状态

## Cache 身份

Cache key 包含 scope、slot、Provider、model、dimension、normalization、document/query role、
role policy identity、adapter revision 和 text SHA-256。默认 scope 为 project，跨项目不命中；
knowledge-base 与 global 只能由显式配置选择。向量以固定 `float32-le-v1` 编码保存，读取时
复核全部身份字段和字节长度。

## Missing-only 与恢复

每个 revision/chunk/slot 保存 pending、cached、embedded、vector_written 或 failed 状态，
并保留 cache key、attempt、错误类别和 retryable。DocumentEmbeddingService 先按输入顺序
读取 cache，只将 missing 按有界 batch 发送给对应 Provider。每个成功批次立即提交 cache、
进度和累计用量；后续批次失败不会丢失已完成批次。显式 retry 重走确定性 Build，但复用
已有 Artifact、解析结果、Chunk 和 cache，只补 missing。

Memory Vector Store 在进程重开后可用 `index-backfill` 从 SQLite cache 重建完整 Point。
Qdrant local-path 先校验既有 collection schema，再执行同样的完整 Point 回填。

## 预算与双槽

预算按 Job、Provider、slot 记录 requests、estimated tokens、chunks、elapsed 和状态类别。
预算在发送正文前预留；不足时不调用 Provider。远程 document embedding 还必须同时获得
slot 级出网授权。默认 P06 Profile 使用 Deterministic Provider，普通测试不访问公网。

Single 需要 primary 100% 完整。Hot-Standby 依次构建 primary、standby，两个 required slot
均须 expected、valid、coverage 与实际 Vector Store 计数完全一致；99.9% 不可激活。
