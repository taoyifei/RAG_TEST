# 文档与版本语义

## 新逻辑文档

`POST .../documents` 为每个新的 `Idempotency-Key` 分配新的全局
`document_id`。内容字节即使与另一文档完全相同，也不会复用逻辑文档 ID；
content-addressed Artifact 可以安全复用。

## 新版本

`POST .../documents/{document_id}/versions` 保持 `document_id`，并按
`dver(document_id, content_sha256)` 计算不可变 `document_version_id`。相同字节
不会产生第二个版本；内容变化会创建新的确定性 IndexRevision，校验通过后原子切换
Active Revision。构建失败不改变旧 Active 指针。

## Rename

`PATCH .../documents/{document_id}` 只更新 `display_name`。它不创建新 dver，
不改变 Node/Chunk ID，也不触发 IndexRevision 构建。

## 删除

Document 与 Knowledge Base 删除只写入 `deleting` 状态和持久
`lifecycle_operations` 计划。API 不直接删除 Blob、Qdrant Collection 或任意路径；
物理回收继续由 P08.5 的 Tombstone、GC Plan、逐项重验和 Filesystem
Reconciliation 负责。
