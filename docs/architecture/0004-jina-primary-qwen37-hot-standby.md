# ADR 0004：Jina 主用与 Qwen3.7 Embedding 双索引热备

## 状态

已接受，模型与字段于 2026-09-01 核对，P02 实施。

## 决策

查询 Dense 通道固定使用两个不可比较的向量空间：

- `primary` 使用 `jina-embeddings-v5-text-small`、1024 维、
  `retrieval.passage` / `retrieval.query` 和 `dense_primary`；
- `standby` 使用北京地域 DashScope 原生
  `qwen3.7-text-embedding`、1024 维、`document` / `query`、固定英文
  instruction 和 `dense_standby`；
- Reranker 固定为 `jina-reranker-v3.5`，不可用时保留 RRF 顺序。

同一个 Chunk 在一个 revision collection 中保存两个 named vectors。维度相同不代表
空间相同；查询向量必须和同 slot 的文档向量、vector name、模型角色与规范化策略一起
使用。任何跨 slot 搜索都以 `IndexCompatibilityError` 失败关闭。

## 激活与查询

默认 `activation_policy=all_required_slots_complete`。只有两个 slot 的数量、1024 维、有限
非零向量、schema 和抽样读回都完整，staging revision 才能激活。P02 只提供协调与 Fake
coverage 门，不创建或激活完整索引。

每个查询先验证 active primary coverage、Jina 出网和 circuit。Jina 成功时不调用备用；
只在 transient、response contract 或 auth/model 类失败后，继续验证 standby coverage、
阿里授权、本地日预算和 circuit，再生成 Qwen query 向量并搜索 `dense_standby`。两个
Provider 都不可用时返回 `DENSE_UNAVAILABLE`，后续检索层可继续 Exact 与 FTS5。

## 恢复与审计

Circuit 以 `(provider_id, operation, model)` 为键。默认连续两次失败打开 60 秒，半开同一
时刻只允许一个真实请求，连续三次成功后恢复。响应合同错误进入 `QUARANTINED`，直到
显式 health check 或配置变化重置。系统默认不做后台付费探测。

Trace 和异常只保存 slot、尝试顺序、原因码、circuit、coverage、次数和耗时，不保存 Key、
正文、完整向量或响应体。Embedding 与搜索缓存键都包含实际 slot。

## 影响

更换 Provider、模型、维度、role/task、instruction、规范化或 vector schema 都改变指纹并
要求新 `IndexRevision`。切换 Reranker 不要求重建向量索引，但会改变 serving fingerprint。
本决策不证明语义质量提升；质量结论需要真实模型和冻结数据集评测。
