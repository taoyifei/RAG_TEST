# Legacy Upload API 迁移

P09 不删除旧 `/api/chat`、旧管理端点或 P00—P08 Adapter。新调用方应迁移到明确的
`/api/v1` scope 和三种互斥操作。

| 目的 | P09 操作 |
| --- | --- |
| 创建新的逻辑文档 | `POST .../documents` |
| 为既有文档创建新版本 | `POST .../documents/{document_id}/versions` |
| 只修改显示名 | `PATCH .../documents/{document_id}` |

旧的含糊 Upload 语义不会进入 `/api/v1`。调用方必须保存服务端返回的 Project、KB、
Document、Version 和 Job ID，并为写请求提供稳定 `Idempotency-Key`。旧 content-only
dver、两参数 Parser、客户端路径和从 display name 推导 document ID 均不接受。

FTS V1 Active Revision 不会在查询请求内原地升级。系统状态返回
`reindex_required=true`，查询返回 `REINDEX_REQUIRED`；运维方必须显式发起受 Writer
Lease 保护的新 Revision 构建。
