# 从 Legacy Industry 部署迁移

根 `compose.yaml` 与根 `Dockerfile` 是 V1 默认产品路径。`deployment/` 下的
七文件、13GB Runtime、GPU/OCR、Worker、四模型 Endpoint、离线 Wheelhouse 和
`legacy-serve` 仅保留历史与迁移价值，不是新部署入口。

迁移时先冻结旧环境备份与回滚证据，再建立全新的 `rag_data`、`qdrant_data` 和
`rag_secrets`。不要把旧 control/state/manifest/trace SQLite 目录直接挂载为新的
`RAG_DATA_DIR`，也不要复制旧 Secret 到源码或 `.env`。

建议顺序：

1. 导出旧文档清单、源文件、活动索引身份和应用版本，保留原环境只读。
2. 按 V1 Quickstart 初始化新 Secret；主密钥与旧系统分离。
3. 运行支持的 Migration 和旧版本夹具升级门禁；FTS V1 明确 Reindex。
4. 在页面重新创建并验证 Provider Connection 与 Retrieval Profile。
5. 重新导入 DOCX，确认双槽覆盖、FTS、SourceSpan、引用与删除隔离。
6. 完成重启、备份到新实例恢复、真实 Provider 和浏览器验收后再切流量。

当前自动升级覆盖 P08.5、P09、P10、P10.5 到 Schema 15。旧 Industry 运行时的
GPU/OCR/本地模型拓扑没有自动等价迁移；需要这些能力时必须单独设计适配，不得让旧
Compose 冒充 V1 验收。
