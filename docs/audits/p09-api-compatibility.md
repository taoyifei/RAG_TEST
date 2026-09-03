# P09 API 与 SDK 兼容审计

审计基线为 `feature/universal-rag@ae82ee1`，日期为 2026-09-03。审计只读取
当前分支、`main` 与 `Industry`，没有访问外部服务或推断仓库外调用方。

## 现有公共面

- HTTP 应用位于 `src/rag_app/api/app.py`。已有 `/live`、`/ready`、
  `/api/chat`、`/api/admin/debug/chat`、会话清理、反馈、旧索引 Job 和管理员
  Trace 接口；尚无 `/api/v1` 生命周期接口。
- `main` 未发现另一套 FastAPI 路由；`Industry` 未发现可移植的 Python API
  路由。现有前端调用 `/api/chat`，因此该路径必须保留。
- `RagEngine` 只公开组件、健康和关闭；`ingest/search/answer` 仍抛出
  `CAPABILITY_UNAVAILABLE`。P09 必须提供可工作的稳定 facade，同时保持旧构造方式。
- P06 已有 SQLite Project、KnowledgeBase、Document、DocumentVersion、
  IndexRevision、IngestionJob、Artifact Catalog 与全局 `document_id` scope 约束。
- P08.5 已有 FTS V2、全量 Vector Inventory、Writer Lease/Fencing、可恢复 GC、
  Pre-provider Cache、Query-aware Evidence、Confidence V2 与
  `RetrievalDiagnostics`。P09 只读取或编排这些合同。

## P09 新增面

新增附件指定的 `/api/v1/projects`、Knowledge Base、Document/Version、Artifact、
Job、Search/Answer、System/Provider、Debug Diagnostics 路由；新增同步 Python SDK
facade。SDK 与 HTTP 必须调用同一 Application Services，不能直接操作 SQLite、
Qdrant 或 FastAPI 类型。

## 兼容策略

- 保留所有现有路由、请求和 NDJSON 事件；P09 采用增量 `/api/v1` 命名空间。
- 新文档、新版本和 Rename 使用三个明确入口。旧模糊上传若存在，只保留为
  deprecated adapter；当前基线没有该入口。
- 公共响应只给检索摘要和最小证据；完整 `RetrievalDiagnostics` 仅在显式开发模式
  且管理员鉴权通过时返回。
- Core 错误码在 SDK 与 HTTP 共用映射。`INDEX_CORRUPT` 绝不降级为 200。
- OpenAPI 固定版本并生成 snapshot；交互式 Swagger/Redoc 继续关闭，不构成
  schema 不可用。

## 外部调用方与破坏性判断

仓库内只确认现有前端依赖 `/api/chat`；没有证据证明存在仓库外正式调用方。
本阶段不删除或修改旧路径，因而当前不需要破坏性 Schema 决策。若实现中发现必须
改变 P05.5/P08.5 身份、解析、Artifact、Lease 或公开 schema，立即转入 P09
Decision，不在 Controller 中补丁绕过。
