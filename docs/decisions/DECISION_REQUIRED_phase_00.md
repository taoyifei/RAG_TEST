# 阶段 0 决策请求：`origin/main` 的 mypy 基线失败

## 当前状态

- 用户已授权方案 A；mypy 最小补丁和目标回归已完成。
- 目标测试 `1 passed`，Ruff 通过，mypy 已恢复为
  `Success: no issues found in 114 source files`。
- 用户随后授权以阶段 0 任务书为目标完成全部必要范围内修改；第二个 docstring
  决策按方案 A 完成，5 个文件去除 docstring 后的 AST 均保持一致。
- 当前没有待用户回答的决策门；本文保留原问题、选项和证据供审计。

## 事实证据

- 预检工作区为空，阶段开始前没有未提交修改。
- `origin/main` 起始提交为
  `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`。
- `feature/universal-rag` 与 `codex/p00-bootstrap` 已从该提交创建并首次推送。
- 在任何源码或配置改动之前运行：

  ```text
  .venv/bin/mypy --no-incremental src evaluation scripts
  ```

- 命令返回码为 `1`，稳定错误为：

  ```text
  scripts/verify_model_contracts.py:632: error: Missing named argument
  "question_profile" for "answer_request" [call-arg]
  Found 1 error in 1 file (checked 114 source files)
  ```

- `src/rag_app/model_contracts.py` 中的 `answer_request` 要求必填
  `question_profile: QuestionProfile`，但
  `scripts/verify_model_contracts.py` 的模型契约探针未传入该参数。
- `compileall` 与 Ruff 已通过；pytest、Google docstring、阶段 smoke 和后续验收均未运行，
  因为本阶段执行“失败即停”。

## 可选方案

### A. 允许阶段分支先修复基线（推荐）

在 `codex/p00-bootstrap` 上为模型契约探针传入与生产合同一致的合成
`QuestionProfile`，补充回归测试，然后从 mypy 门禁重新开始。

影响：修复范围小且不改变公共 API，但它属于阶段 0 目标之外的既有缺陷，必须获得明确授权。

### B. 先在 `main` 外部修复，再同步集成分支

由维护者在本轮流程之外修复 `main`；随后重新核对
`feature/universal-rag` 与新 `origin/main` 的关系，并由用户决定如何同步。

影响：保持阶段 0 不承担基线修复，但会中断当前已创建分支的线性起点，恢复流程需要额外治理决定。

### C. 豁免该 mypy 错误并继续

影响：违反本阶段“基线测试非环境失败即暂停”和统一严格验证要求，不建议采用。

## 推荐项

选择方案 A。它保留 `main` 和 `Industry` 不变，在阶段分支上用最小补丁恢复现有
mypy 合同，并以回归测试证明探针与生产请求构造保持一致。

## 需要用户回答的一句话

是否授权按方案 A 在 `codex/p00-bootstrap` 上先修复这个既有 mypy 基线错误，再继续阶段 0？

---

# 第二个决策请求：Google docstring 基线失败

## 事实证据

- 在获批的 mypy 修复通过后运行：

  ```text
  .venv/bin/python scripts/check_google_docstrings.py
  ```

- 命令返回码为 `1`，结果为 `missing_google_sections=22`。
- 缺失项位于当前 `origin/main` 已有的 5 个源码文件：
  `src/rag_app/api/stream.py`、`src/rag_app/clients/llm.py`、
  `src/rag_app/generation/evidence.py`、`src/rag_app/query_service.py`、
  `src/rag_app/state/answer_cache.py`。
- 失败内容均为已有公共或非下划线 callable 缺少项目要求的 `Args:` 和
  `Returns:` 段，不是 Python 环境、依赖或外部服务故障。
- 完整 pytest 尚未运行，因为该门禁失败后立即停止。

## 可选方案

### A. 允许阶段分支补齐既有 docstring 基线（推荐）

只补充报告中的 22 个 `Args:`/`Returns:` 段，不修改可执行语句，并使用去除
docstring 后的 AST 对比证明行为不变，然后重新运行门禁。

影响：会额外修改 5 个既有源码文件，但属于纯文档补丁，不改变公共 API 或运行行为。

### B. 先在 `main` 外部修复，再同步集成分支

影响：阶段 0 继续暂停，且需要重新决定如何把新的 `origin/main` 同步到已经创建的
集成分支。

### C. 从阶段基线移除 docstring 门禁

影响：与 README 已公布的本地校验入口及项目 Python 规范不一致，不建议采用。

## 推荐项

选择方案 A，并把 docstring-only 补丁作为独立 Conventional Commit，避免与阶段治理
骨架混在同一提交中。

## 需要用户回答的一句话

是否授权按方案 A 补齐这 5 个既有源码文件的 docstring 基线，再继续阶段 0？
