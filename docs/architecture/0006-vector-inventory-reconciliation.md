# 0006 Vector inventory reconciliation

## 决策

Revision 激活前，Vector Store 必须把命名空间中的每一个原始 Point 与
SQLite canonical Chunk 全量对账。计数相等不再代表验证通过。

每个 Point 必须同时满足以下不变量：

- Point ID 等于 `vector_point_id(revision_id, chunk_id)`；
- `project_id`、`knowledge_base_id`、`revision_id`、`document_id` 和
  `chunk_id` 与 canonical Chunk 一致；
- `dense_primary` 与 `dense_standby` 名称、维度和有限数值符合 Revision
  的向量 Schema；
- 非空 canonical 集合中的向量不得全为零；
- 不存在缺失 Point、额外 Point、重复身份或无法解析的原始记录。

## 实现边界

`VectorStorePort.audit_revision()` 返回安全的 `VectorRevisionInventory`。
它只包含计数、身份、维度、原因码和规范摘要，不包含向量、正文或 payload
原值。旧的 aggregate validator 保留为兼容入口，但 `RevisionValidator`
以 inventory tuple 等价为激活门禁。

Memory 与 Qdrant adapter 共用同一套 Point 规则。Qdrant Scroll 必须覆盖
所有页，并拒绝重复 offset、无前进 offset、无法转换的记录和不完整页面；
无法解析的 raw Point 仍计入审计失败，不能在统计前被丢弃。

## 结果

这一决策证明的是受控命名空间与 canonical SQLite 清单的一致性。它不证明
远程 Qdrant 的可用性或性能。本阶段没有启动 Qdrant Server。
