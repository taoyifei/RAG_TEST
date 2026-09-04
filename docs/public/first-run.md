# 首次使用

容器部署以 `docs/public/quickstart.md` 为准。它会用一个排他命令创建主密钥、
Bootstrap Token、Qdrant API Key 与 Qdrant 配置，避免手工生成文件遗漏或权限错误。

本地进程模式按以下步骤启动：

1. 用 `rag-app init-secrets --directory <path>` 创建完整 0600 Secret Bundle。
2. 设置 `RAG_DATA_DIR`、Bundle 中的 Secret 文件路径，并执行 `rag-app serve`。
3. 打开浏览器，在首次使用向导中输入 Bootstrap Token。
4. 配置并验证 Jina 与百炼连接。
5. 创建项目和知识库，再创建、预览并确认检索方案。
6. 上传 DOCX，等待索引任务成功后开始查询。

没有 Provider 时仍可用离线 FTS/Exact 基础检索，但不能声称 Live Ready。浏览器
刷新会恢复并轮换管理员会话；退出后 Cookie 立即失效。首次向导不会保存 Bootstrap
Token，浏览器存储只允许 project、knowledge base 和 revision 这类非敏感范围。
