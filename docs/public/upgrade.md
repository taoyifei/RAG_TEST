# 升级与回滚

P11 使用单调 SQLite Migration。0001—0014 是已发布历史，P11 只新增
`0015_provider_observability.sql`。每个已执行文件的 SHA-256 会保存在
`schema_migrations`；历史文件内容变化、版本缺口、未知更高版本或 SQL 失败都会
阻止服务进入可写状态。

## 升级

1. 停止写流量和后台索引作业。
2. 使用 `rag-app backup create` 创建包含 SQLite、Blob、Qdrant Snapshot 和兼容
   清单的备份，再运行 `rag-app backup verify`。
3. 单独备份主密钥。普通备份包有意不包含主密钥。
4. 更新镜像并启动。Product Runtime 会在开放端口前事务执行未应用 Migration。
5. 检查 `/live`、`/ready`、Schema 版本、向量 Inventory、FTS 查询和引用。

P08.5、P09、P10 和 P10.5 的受控数据夹具会在 CI 中升级到当前 Schema。旧
`chunks_fts` V1 行只保留为 Legacy 数据，不会自动复制到 `chunks_fts_v2` 或冒充
新索引；必须建立新 Revision 并完成显式 Reindex 后再激活。

## 回滚边界

P11 Schema 15 对旧应用不是向后兼容目标。应用回滚必须同时恢复升级前已验证的
SQLite、Blob、Qdrant Snapshot 和匹配的兼容清单，不能只替换容器镜像。Provider
Credential 的 AES-GCM AAD 与 key version 在 0015 中没有变化；恢复时仍必须提供
原主密钥。Migration SQL 失败会回滚当前事务并阻止启动，但这不能替代升级前备份。

任何涉及唯一正式数据的破坏性迁移都不在自动流程内，必须先建立单独 Decision 并
获得明确授权。
