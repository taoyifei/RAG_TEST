# 阶段 1 进度：Core、Ports 与显式插件架构

## 状态

- Integration base：`09702a0fbb99e365ff0cad036e970bb1af2fbaee`。
- Feature branch：`codex/p01-core-ports`。
- Feature commit：`75040b429b850b599d92493a955fa241a68dac72`。
- Integration implementation merge commit：`0588ed30f0cc8ba4b9e0d7956f7cd7c488939a1f`
  （`--no-ff`）。
- 远程状态：`origin/codex/p01-core-ports` 已包含 feature commit，
  `origin/feature/universal-rag` 已包含 implementation merge commit；两者均以
  `git ls-remote` 核对。
- `main` 与 `Industry` 只读；没有向两者提交、合并或推送。

## 实际修改

- 新增 `rag_app.core` 的格式中立模型、错误、ID、两类指纹、Egress/circuit 策略、Trace
  事件和十类同步窄 Ports。
- 新增空 Registry、显式内置注册、严格 JSON Profile、唯一 Composition Root、资源
  生命周期与最小同步 RagEngine。
- 新增旧契约单向转换、安全 DOCX wrapper、确定性/声明型 Provider、Memory/SQLite
  离线 Store 和嵌入示例；没有移动或删除旧模块。
- 固定 Jina v5 small primary、Qwen3.7 standby、Jina reranker 身份和双 named-vector
  topology；P01 不提供真实 HTTP。
- 新增 Core/Composition/Legacy/架构边界测试和本阶段设计、公共、迁移文档。
- Feature commit 共记录 55 个文件、7,099 行新增；没有提交 `.env`、数据库/WAL、索引、
  模型、ZIP、缓存或 secret。

## Schema、公共接口与迁移

本阶段只新增 Python 公共 schema；现有 HTTP/SDK schema、RuntimeSettings、SQLite schema、
Qdrant collection 和旧索引均未改变。没有重建或激活索引，没有覆盖数据库或用户文件。
Legacy 转换的字段损失通过 warning 显式报告。

## 验证记录

起始集成分支：

```text
.venv/bin/python scripts/dev.py check
936 passed, 75 deselected, 4 warnings

.venv/bin/python scripts/dev.py smoke
54 passed, 1 warning
```

提交前阶段验收：

```text
.venv/bin/python -c "from rag_app.application.engine import RagEngine; print('ok')"
ok

.venv/bin/python -m pytest tests/core tests/composition tests/adapters/legacy tests/test_architecture_boundaries.py -q
65 passed in 1.48s

.venv/bin/python scripts/dev.py check
ruff: All checks passed
mypy: Success: no issues found in 153 source files
Google docstrings: missing_google_sections=0
998 passed, 75 deselected, 4 warnings in 185.67s

.venv/bin/python scripts/dev.py smoke
58 passed, 1 warning in 2.14s
```

合并后的 `feature/universal-rag@0588ed30f0cc8ba4b9e0d7956f7cd7c488939a1f`：

```text
.venv/bin/python -c "from rag_app.application.engine import RagEngine; print('ok')"
ok

.venv/bin/python -m pytest -q tests/core tests/composition tests/adapters/legacy tests/test_architecture_boundaries.py
65 passed in 1.21s

.venv/bin/python scripts/dev.py check
ruff: All checks passed
mypy: Success: no issues found in 153 source files
Google docstrings: missing_google_sections=0
998 passed, 75 deselected, 4 warnings in 177.23s

.venv/bin/python scripts/dev.py smoke
58 passed, 1 warning in 1.59s
```

上述命令失败数均为 0。`check` 明确跳过 75 个带 `local_integration` 或
`live_provider` 标记的测试；没有把它们写成通过。

## 外部服务与安全边界

截至当前只访问 GitHub origin 执行 `git fetch/pull/push`。应用验证未调用 Jina、阿里、LLM、
OCR 或 Qdrant，没有读取真实 API Key，没有使用企业文档；DOCX 行为只使用后续测试合成
fixture。远程 Provider 在 P01 调用时失败关闭。

## 决策与风险

没有修改已接受 ADR，没有触发公共 schema、真实文档出网、付费服务、不可逆迁移、legacy
删除或合入 main 的决策门。P01 的声明型远程 Provider 按合同不发 HTTP；真实 Jina、
Qwen3.7 和 Qdrant 适配、索引构建及质量验证仍属于后续阶段。当前没有未完成的 P01
实现项。
