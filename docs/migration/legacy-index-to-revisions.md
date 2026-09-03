# Legacy Index 到 P06 Revision 的迁移边界

P05.5 前没有正式 P06 SQLite、Blob 或 Qdrant 数据，因此默认迁移策略是创建新的 data root，
从 migration 0001 开始构建。不得把工具缓存或旧服务数据库当成 P06 权威数据。

旧 content-only dver 与 `dver(document_id, content_sha256)` 不等价，禁止原地改名冒充新
身份。若部署现场发现未声明旧数据，必须暂停、备份、盘点 schema/引用/collection，再提交
单独迁移决策。P06 不执行不可逆自动升级。

旧单向量索引不能只修改 collection schema 冒充 Hot-Standby。新 Revision 必须用完整
required named-vector schema 重建，每个 Point 一次包含全部 required vectors，并通过实际
Store coverage 与回读验证。

Parser Policy 改变创建新 Revision，不创建新 DocumentVersion。物理 Artifact 可以跨逻辑
文档和 Revision 复用；逻辑 dver、node、chunk 仍保持 document scope。
