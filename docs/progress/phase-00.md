# 阶段 0 进度：集成分支与治理基线

## 状态

- 开发分支：`codex/p00-bootstrap`。
- 集成分支：`feature/universal-rag`，从
  `origin/main@af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` 创建。
- `main` 与 `Industry` 始终只读；未向两者提交、合并或推送。
- 阶段 feature commit：`6771193531c6ccca8b487fb73d0a0df6dd5a617a`。
- `--no-ff` integration merge commit：
  `06b6325c2b915b83628017dd8e486c0815db793f`。
- 推送后远程已核对：`origin/codex/p00-bootstrap@6771193`、
  `origin/feature/universal-rag@06b6325`；`origin/main@af30f81` 和
  `origin/Industry@5cc5d7b` 未变化，远程阶段分支保留。

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
- `6771193`：Industry 矩阵、baseline、ADR、分支/决策门与本阶段报告。

## 实际修改文件

- 运行配置：`deployment/ASSETS.sha256`、`deployment/config/pipeline.json`。
- 治理文档：`docs/architecture/0001-universal-rag-goals.md`、
  `0002-ports-and-adapters.md`、`0003-local-first-validation.md`、
  `docs/baseline/main-baseline.md`、`docs/decisions/DECISION_REQUIRED_phase_00.md`、
  `docs/development/branch-workflow.md`、`decision-gates.md`、
  `docs/migration/industry-port-matrix.md` 与本文件。
- 开发入口：`scripts/dev.py`、`scripts/verify_model_contracts.py`。
- 兼容修复：`src/rag_app/api/stream.py`、`clients/llm.py`、
  `clients/model_services.py`、`clients/resilience.py`、`generation/answer.py`、
  `generation/evidence.py`、`model_contracts.py`、`ocr/client.py`、
  `query_service.py`、`state/answer_cache.py`。
- 自动回归：`tests/conftest.py`、`test_answer_streaming.py`、
  `test_app_update_builder.py`、`test_architecture_boundaries.py`、`test_dev_cli.py`、
  `test_release_safety.py`、`test_resilient_http.py`、`test_runtime_construction.py`、
  `test_runtime_preflight.py`、`test_support_id_answer.py`、
  `test_verify_model_contracts.py`。

## Schema、公共接口与迁移

- 公共 HTTP/SDK schema、配置键和持久化数据模型均未改变。
- 未迁移、删除或重建现有 Qdrant collection、SQLite 数据库或用户索引。
- Industry 未被 merge/cherry-pick；所有 `PORT_NOW` 项均从独立失败证据重做最小补丁，
  并由矩阵中的具体测试证明。工业语料、镜像、现场包、回滚和服务器验收资产未进入
  默认路径。

## 实际验证

### 起始完整基线

```text
.venv/bin/python -m pytest -q
906 passed, 111 failed, 61 warnings in 685.63s
```

其中 63 项为 12 个真实 Qdrant 文件在 `127.0.0.1:6333` 收到 502；其余 48 项
main 基线漂移已按最小补丁修复。未把真实 Qdrant 测试伪装成离线通过。

| 分类 | 准确结论 |
|---|---|
| `OFFLINE_REQUIRED` | 统一离线入口最终 931 passed；默认无公网、真实 Key、Docker、Qdrant Server、OCR 或模型 API |
| `LOCAL_INTEGRATION_REQUIRES_SERVICE` | 12 个真实 Qdrant 文件中 63 项在当前本地代理路径返回 502；保留真实集成语义 |
| `TRUE_CODE_REGRESSION` | 起始 48 项 main 漂移均以兼容补丁修复并进入回归 |
| `OPTIONAL_TOOL_MISSING` | 0 项门禁失败；Node 在 P00 明确为未检查的后续可选工具 |

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

## 外部服务与数据边界

- 默认 `doctor/check/smoke` 没有调用真实 Embedding、Reranker、LLM、OCR 或 Qdrant
  服务，没有读取真实 API Key，也没有使用用户文档；DOCX fixture 全部由测试合成。
- 补充验收曾显式启动 loopback 临时 Qdrant，输入仍是测试合成数据；除此之外，除 Git
  fetch/push 到仓库 origin 外，没有发送应用数据到外部服务。
- 离线网络守卫禁止非 loopback socket；loopback 只为显式本地集成测试保留。

## 决策文件

- `docs/decisions/DECISION_REQUIRED_phase_00.md` 记录了起始 mypy/docstring 基线门；用户
  授权必要兼容修复后已解除，当前没有待回答的决策门。

## P00 补充验收与 hardening

- Integration base：`06b6325c2b915b83628017dd8e486c0815db793f`。
- Feature branch：`codex/p00-hardening`。
- Validation code commit：`c0f6bf4`。
- Hardening feature commit：`552ba45925ebc1846394615242a6a9a9c277f281`。
- Hardening implementation merge：
  `578585ff7cc9cf8ba8b17376104e488526e90774`。
- Remote push：首次 hardening 合并后 `origin/feature/universal-rag@578585f`、
  `origin/codex/p00-hardening@552ba45`；`main@af30f81` 和 `Industry@5cc5d7b`
  未变化。
- Schema/API compatibility：没有公共 HTTP/SDK schema、配置键、持久化 schema 或索引
  迁移。
- Migration：12 个真实 Qdrant 模块添加 `local_integration` marker；默认 check 改用
  marker 表达式，没有删除、mock 或按失败文件忽略测试。

实际 hardening 文件：`pyproject.toml`、`scripts/dev.py`、`tests/conftest.py`、
`tests/test_network_guard.py`、`tests/test_architecture_boundaries.py`、
`tests/test_dev_cli.py`、`tests/test_rerank_stage.py`、12 个真实 Qdrant 测试模块，以及
三个 ADR、决策门、Industry 矩阵和本阶段报告。

实际完成命令与结果：

```text
.venv/bin/python scripts/dev.py doctor
Python/Git/source-tree import/SQLite FTS5/temp: OK; Node: SKIP optional

.venv/bin/python scripts/dev.py smoke
54 passed, 1 warning in 1.50s

.venv/bin/python scripts/dev.py check
compileall: passed
ruff: All checks passed
mypy: no issues in 115 source files
google docstrings: missing_google_sections=0
pytest: 936 passed, 75 deselected, 4 warnings in 182.76s

.venv/bin/python -m pytest -q -m local_integration
75 passed, 936 deselected, 58 warnings in 410.31s

.venv/bin/python -m pytest -q tests/test_dev_cli.py tests/test_network_guard.py tests/test_architecture_boundaries.py tests/test_rerank_stage.py
11 passed in 1.08s
```

本地集成使用已存在的 `qdrant/qdrant:v1.18.3` 镜像，以 `--pull=never --rm` 创建
`rag-p00-hardening-qdrant`，只绑定 `127.0.0.1:6333`、无挂载，健康检查为
`all shards are ready`。没有下载镜像；完成后容器已停止并自动删除，6333 不再监听，
原有停止容器和镜像未修改。第一次无服务运行已在取得可用本地镜像线索后中断，没有
最终统计，不作为验收证据。

首次 hardening 合并后的集成分支再次执行：`doctor` 全部 OK；`check` 为
`936 passed, 75 deselected, 4 warnings in 177.83s`；`smoke` 为
`54 passed, 1 warning in 1.50s`。该复验期间没有启动 Qdrant 容器。

补充验收没有触发新决策门。剩余风险仅包括现有 Starlette/httpx 弃用警告和本地 HTTP
Qdrant 测试的 insecure-connection 警告；测试只使用 loopback 与合成凭据。

## 已知限制

- 12 个真实 Qdrant 测试文件仍需显式可用的本机 Qdrant Server；无服务起始运行返回
  502。补充验收使用无挂载临时 v1.18.3 容器后，75 项全部通过；它们仍不进入默认离线
  门禁，也不代表 Qdrant Remote 或生产环境已经验证。
- `project_import` 当前为 source-tree 模式，不是已安装分发包。
- Node 仅记录为后续可选检查；旧 shell/Docker 部署脚本仍存在但不在默认验证链。
- pytest 有 Starlette/httpx 弃用警告；部分构造测试会创建 Qdrant client 并被离线网络
  守卫阻止版本探测，但不影响测试结论。
- Qdrant Local + SQLite FTS5 是后续最小运行栈决策，本阶段未实现 Store/索引迁移。
- UI same-origin session 与 Trace 问题捕获涉及公共 HTTP/持久化兼容，已标记
  `REIMPLEMENT_LATER`，本阶段未移植。

## 下一阶段接口

1. 在 `rag_app.core` 建立格式中立 `models/`、窄 `ports/`、`errors.py`、
   `fingerprints.py` 和 `policies.py`；以现有安全 DOCX parser 作为首个 legacy adapter。
2. 在 `rag_app.composition` 建立 `registry.py`、`profiles.py` 和 `factory.py`；只允许可信
   显式注册和实例注入，未知键失败关闭。
3. 定义最小 `EmbeddingPort`、`RerankerPort`、`GenerationPort`、
   `VectorStorePort`、`LexicalStorePort` 与 `TracePort`，不暴露厂商 schema。
4. 用 legacy adapters 绞杀式包装现有 DOCX/Qdrant/HTTP/SQLite 实现；下一阶段不改旧
   公共 API 或旧索引，任何索引迁移先经过破坏性决策门。
