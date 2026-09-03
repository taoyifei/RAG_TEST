# Universal RAG HTTP API V1

P09 在保留旧 HTTP 接口的前提下新增 `/api/v1`。新接口只接受显式
Project、Knowledge Base 和 Document scope，不接收客户端文件系统路径。

## 鉴权

- 查询接口使用 Query Bearer Token。
- 生命周期、Job、Artifact、状态和 Probe 使用 Admin Bearer Token。
- Query 与 Admin Token 必须非空且不同。
- `/live` 与 `/ready` 不调用远程 Provider；`providers:probe` 同时要求 Admin
  Token、`X-Allow-Network: true` 和 `X-Request-Budget`。

## 生命周期接口

- `POST /api/v1/projects`
- `GET|PATCH|DELETE /api/v1/projects/{project_id}`
- `POST|GET /api/v1/projects/{project_id}/knowledge-bases`
- `GET|PATCH|DELETE /api/v1/projects/{project_id}/knowledge-bases/{kb_id}`
- `POST|GET /api/v1/projects/{project_id}/knowledge-bases/{kb_id}/documents`
- `GET|PATCH|DELETE .../documents/{document_id}`
- `POST|GET .../documents/{document_id}/versions`
- `GET .../documents/{document_id}/versions/{document_version_id}`

创建 Project、Knowledge Base、Document 和 DocumentVersion 时使用
`Idempotency-Key`。新文档的 `display_name` 是 UTF-8 查询参数；新版本始终复用当前
文档显示名，只有独立 Rename 接口可以修改它。上传正文是原始 DOCX 字节，
`Content-Type` 必须是 DOCX 或 `application/octet-stream`。默认上传上限为 32 MiB。

## 查询和回答

- `POST /api/v1/projects/{project_id}/knowledge-bases/{kb_id}:search`
- `POST /api/v1/projects/{project_id}/knowledge-bases/{kb_id}:answer`

公开响应包含实际 Active Revision、index/serving fingerprint、slot、vector name、
route reason、rerank mode、degraded reason、Evidence 数量、缓存状态和有界诊断摘要。
完整 `RetrievalDiagnostics` 只在显式启用的 Admin Debug 端点返回。
管理员还可通过 `/api/v1/admin/traces` 按 `trace_id`、`query_id` 或 `job_id` 三选一
读取脱敏 `TraceEvent`；该接口不返回正文、Prompt、Provider 原始响应或 fencing token。

Answer 请求设置 `stream=true` 时返回 SSE。事件顺序为 `meta`、`retrieval`、
`final`；`final` 是权威完整响应，delta 不伪装为模型 Token。

## 稳定错误

非成功响应统一放在 `error` 对象内，包含 `code`、安全 `message`、`stage`、
`retryable`、`trace_id` 和非敏感 `details`。P09 显式区分
`INDEX_NOT_READY`、`INDEX_CORRUPT`、`CHANNEL_UNAVAILABLE`、
`PROVIDER_UNAVAILABLE`、`POLICY_DENIED`、`REINDEX_REQUIRED`、
`DENSE_UNCALIBRATED` 与 `CONFLICT_ACTIVE_WRITER`。

权威机器合同见 `docs/public/openapi-v1.json`，使用
`python scripts/dev.py openapi-check` 校验。
