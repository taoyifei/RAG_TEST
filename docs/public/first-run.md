# 首次使用

1. 用 `rag-app init-secrets --output <path>` 创建 0600 主密钥。
2. 在另一个 0600 文件中写入至少 16 个字符的 Bootstrap Token。
3. 设置 `RAG_DATA_DIR`、两个 Secret 文件路径，并执行 `rag-app serve`。
4. 打开浏览器，在首次使用向导中输入 Bootstrap Token。
5. 配置并验证 Jina 与百炼连接。
6. 创建项目和知识库，再创建、预览并确认检索方案。
7. 上传 DOCX，等待索引任务成功后开始查询。

默认内存 vector 模式不需要外部 Qdrant，未配置 Provider 时仍可用离线 FTS/Exact 基础
检索。浏览器刷新会恢复并轮换管理员会话；退出后 Cookie 立即失效。

首次向导不会保存 Bootstrap Token。浏览器存储只允许保存 project、knowledge base 和
revision 这类非敏感范围。
