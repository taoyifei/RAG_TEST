# Legacy Industry 部署迁移计划

P10.5 将 `rag-app serve` 切到 Product Runtime。根 `Dockerfile` 默认命令继续是
`serve`，因此构建出的新应用镜像进入产品路径。`deployment/product/compose.yaml` 是
新的最小部署入口。

原 `deployment/compose.yaml`、OCR、Worker、发布包、回滚与离线验收脚本仍保留，避免
丢失历史能力。该 Compose 的应用已显式使用 `legacy-serve`，其旧 state/manifest/trace
目录不得映射为 `RAG_DATA_DIR`。README 不再把它作为首次启动入口。

P11 应按以下顺序完成迁移：

1. 冻结一份 Legacy 发布与回滚演练证据。
2. 为 Product Runtime 增加真实 Qdrant 和批准后的 Provider 装配。
3. 把需要保留的 OCR/Worker 能力适配到 Product Runtime 的 Profile/Job 边界。
4. 验证数据迁移、备份恢复和回滚，不复用不兼容 SQLite 目录。
5. 更新正式部署脚本后，再删除或归档 Legacy Compose。

本阶段不修改 0001—0010，不迁移正式凭据数据库，也不调用真实 Provider。
