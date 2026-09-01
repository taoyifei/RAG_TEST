# 阶段 0 进度：集成分支与治理基线

## 状态

- 开发分支：`codex/p00-bootstrap`。
- 集成分支：`feature/universal-rag`，从
  `origin/main@af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` 创建。
- `main` 与 `Industry` 始终只读；未向两者提交、合并或推送。
- 本文件记录阶段分支合并前证据；最终阶段提交、merge commit 和远程 SHA 以最终交付
  输出及 Git 远程引用为准。

## 已完成改动

- 审计 Industry 的 14 个独有提交和 107 个差异文件，形成逐提交/模块移植矩阵。
- 修复 main 的模型契约探针调用漂移和 Google docstring 基线。
- 最小移植并回归三类通用修复：
  - 精确来源编号、多个显式编号及流式发布门禁；
  - Embedding/Reranker/OCR 无效响应的有限 failover，LLM 默认不重放；
  - app-update、runtime fixture 与发布安全测试对齐现行合同。
- 新增 `scripts/dev.py doctor|check|smoke`，同一 Python 入口支持 Windows、Linux 和
  macOS；默认测试网络守卫阻止非 loopback 连接。
- 新增 core 反向依赖、敏感文件、用户文档、归档包和 Industry 配置边界测试。
- 建立通用目标、ports/adapters、local-first、分支和决策门文档。

## 阶段分支提交

- `a3a85b0`：补齐 LLM 契约探针问题画像。
- `9bcf8e0`：补齐回答链 Google docstring，AST 行为不变。
- `45f08d2`：对齐现行离线构建与 runtime 测试合同。
- `cbf1d48`：非生成式无效响应有限端点切换。
- `eb99adf`：精确校验显式来源编号。
- `4286a67`：默认离线开发入口和边界守卫。

## 实际验证

### 起始完整基线

```text
.venv/bin/python -m pytest -q
906 passed, 111 failed, 61 warnings in 685.63s
```

其中 63 项为 12 个真实 Qdrant 文件在 `127.0.0.1:6333` 收到 502；其余 48 项
main 基线漂移已按最小补丁修复。未把真实 Qdrant 测试伪装成离线通过。

### 统一入口

```text
.venv/bin/python scripts/dev.py doctor
OK python: 3.11.15
OK git: /usr/bin/git
OK project_import: source-tree
OK sqlite_fts5: 3.51.2
OK temp_directory: /tmp
SKIP node: optional in a later phase
```

```text
.venv/bin/python scripts/dev.py check
compileall: passed
ruff: All checks passed
mypy: no issues in 115 source files
google docstrings: missing_google_sections=0
pytest: 931 passed, 4 warnings in 181.15s
```

```text
.venv/bin/python scripts/dev.py smoke
52 passed, 1 warning in 1.24s
```

### 移植回归

- 模型契约探针目标回归：1 passed。
- app-update/发布安全：30 passed。
- runtime construction/preflight：32 passed，1 warning。
- resilience/model/OCR：26 passed，1 warning。
- 来源编号/流式/回答/pipeline/资产：78 passed；指纹修正后另 21 passed。
- docstring-only AST：5 个文件全部 `executable_ast_equal=True`。

## 已知限制

- 12 个真实 Qdrant 测试文件仍需可用的本机 Qdrant Server；当前环境返回 502，未重建
  服务、未修改索引、未宣称这些集成测试通过。
- `project_import` 当前为 source-tree 模式，不是已安装分发包。
- Node 仅记录为后续可选检查；旧 shell/Docker 部署脚本仍存在但不在默认验证链。
- pytest 有 Starlette/httpx 弃用警告；部分构造测试会创建 Qdrant client 并被离线网络
  守卫阻止版本探测，但不影响测试结论。
- Qdrant Local + SQLite FTS5 是后续最小运行栈决策，本阶段未实现 Store/索引迁移。
- UI same-origin session 与 Trace 问题捕获涉及公共 HTTP/持久化兼容，已标记
  `REIMPLEMENT`，本阶段未移植。

## 下一阶段接口

1. 定义格式中立 Document IR 和 `ParserPort`，以现有安全 DOCX parser 作为首个
   adapter，保持来源定位和安全校验。
2. 定义最小 `EmbeddingPort`、`RerankerPort`、`GenerationPort`、
   `VectorStorePort`、`LexicalStorePort` 与 `TracePort`，不暴露厂商 schema。
3. 建立显式 Registry 和可信配置选择；未知键失败关闭，禁止用户输入动态 import。
4. 实现 Qdrant Local + SQLite FTS5 开发 adapter，并为现有 Qdrant/SQLite 行为保留
   兼容回归；任何索引迁移先经过破坏性决策门。
