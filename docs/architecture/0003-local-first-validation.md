# ADR 0003：Local-first 与默认离线验证

- 状态：Accepted
- 日期：2026-09-01

## 决策

1. 开发、单元测试和 CI 默认不得访问互联网，不要求真实 API Key、Docker、正在运行的
   Qdrant Server、OCR 服务或真实文档。
2. 统一入口为 `python scripts/dev.py doctor|check|smoke`。Windows PowerShell、
   Linux 和 macOS 使用同一 Python 命令；shell 脚本不进入默认验证链。
3. `check` 透明运行 compileall、Ruff、mypy、Google docstring 和离线 pytest；任何
   子命令的非零返回码原样返回并停止。
4. `smoke` 只运行合成 API、DOCX、chunk、RRF、rerank、answer 和架构边界测试。
5. pytest 在统一入口下禁止非 loopback socket。真实 Qdrant 测试保留为显式集成集合，
   不通过 mock 冒充已运行。
6. 直接运行 pytest 也默认启用离线守卫；只有手工设置 `RAG_TEST_NETWORK=live` 并显式
   选择 `live_provider` marker 时才解除公网 socket 限制。`local_integration` 只允许
   loopback 服务，不代表获得数据出网授权。

## 后续最小运行栈

普通离线开发和 CI 以 SQLite metadata/control、SQLite FTS5、
`InMemoryVectorStore`、Deterministic Embedding、Lexical Overlap Reranker、
Extractive Generator 和合成 DOCX 为权威验证栈。它只证明接口、状态机、隔离、来源和
错误路径正确，不证明语义检索质量。

本地实际使用可选择 Qdrant Local 或 Qdrant Remote。Qdrant Local 是无需独立服务的便利
adapter，不假设与 Server 高级能力完全等价，也不是离线测试的权威 Store。SQLite FTS5
承担词法检索；FTS5 `bm25()` 分值越小越相关，应用层必须先转成排名再参与 RRF，保证
Memory、Local、Remote Store 使用一致融合语义。阶段 0 只记录该决策，尚未实现或迁移
索引。

## 外部 API 边界

- 分别设置 `remote_document_embedding`、`remote_query_embedding`、
  `remote_reranking`、`remote_generation` 四类数据出网授权；每项默认拒绝，只有可信
  Profile/项目策略显式允许且环境变量密钥存在时才能调用。
- Local Provider 失败不得静默降级到公网 Provider；一个厂商失败不得把文档静默转发给
  另一个厂商。手工外部 API 测试必须显式选择对应 marker，CI 和默认命令不会启用。
- Provider 必须设置连接/读取超时、有限重试、429/5xx 分类和请求批次上限。
- 密钥、请求正文、响应正文、文档片段不得写入 Git、日志、Trace、错误正文、截图或
  测试快照。
- 没有用户授权、真实账号和真实评测数据时，只报告代码与离线测试证据，不报告生产
  可用性或效果提升。
