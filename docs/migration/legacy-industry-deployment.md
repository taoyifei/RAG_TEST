# Legacy Industry 部署迁移计划

P10.5 将 `rag-app serve` 切到 Product Runtime。P11 已把根 `Dockerfile` 与根
`compose.yaml` 建立为新的默认入口，`deployment/product/compose.yaml` 仅保留为
过渡期历史合同。

原 `deployment/compose.yaml`、OCR、Worker、发布包、回滚与离线验收脚本仍保留，避免
丢失历史能力。该 Compose 的应用已显式使用 `legacy-serve`，其旧 state/manifest/trace
目录不得映射为 `RAG_DATA_DIR`。README 不再把它作为首次启动入口。

正式迁移按 `docs/migration/from-industry.md` 执行。历史计划如下：

1. 冻结一份 Legacy 发布与回滚演练证据。
2. 为 Product Runtime 增加真实 Qdrant 和批准后的 Provider 装配。
3. 把需要保留的 OCR/Worker 能力适配到 Product Runtime 的 Profile/Job 边界。
4. 验证数据迁移、备份恢复和回滚，不复用不兼容 SQLite 目录。
5. 更新正式部署脚本后，再删除或归档 Legacy Compose。

P11 不修改 0001—0014，只新增 0015。正式凭据和真实 Provider 仍受独立 Live Gate
控制，不能由离线迁移测试推断为已通过。
