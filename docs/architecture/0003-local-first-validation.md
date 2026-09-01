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

## 后续最小运行栈

后续阶段采用 Qdrant Local 加 SQLite FTS5 作为无需独立服务器的最小运行栈：Qdrant
Local 承担向量能力，SQLite FTS5 承担词法检索和本地状态。阶段 0 只记录该决策，尚未
实现或迁移索引。

## 外部 API 边界

- 只有用户显式启用数据出站并通过环境变量提供密钥时，才允许运行手工外部 API
  测试；CI 和默认命令不会自动启用。
- Provider 必须设置连接/读取超时、有限重试、429/5xx 分类和请求批次上限。
- 密钥、请求正文、响应正文、文档片段不得写入 Git、日志、Trace、错误正文、截图或
  测试快照。
- 没有用户授权、真实账号和真实评测数据时，只报告代码与离线测试证据，不报告生产
  可用性或效果提升。
