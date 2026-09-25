# CQ1 集成报告与 P0 停止点

记录日期：2026-09-25。执行仓库为 `taoyifei/RAG_TEST`，本地隔离工作树 `/home/jerry/work/RAG-cq1`，基线提交 `dca24a80823b4e5d24dbbdd725e10c97167ad7a5`。原分支与用户现有工作树未被重置、覆盖或合并。

## 实际状态

| 层 | 本轮结果 |
| --- | --- |
| 工程接入 | CQ1-0 至 CQ1-3 的候选代码与离线合同已实现；v2 严格流默认关闭。模板解析策略升 v3，但服务器活动版本未重建。 |
| 自动支持检查能力 | **P0 未通过**：与 8289 配置对齐的 8B 资格集 10 例中 4 个高风险误接受；这些响应通过当前确定性 quote/位置检查仍会发布。 |
| 企业答案质量 | 未取得 72+20 同口径回放或业务审阅，不能声称提升、达到可信准出或生产可用。 |
| 对话体验 | 成对历史、续接、stop、状态和来源卡有离线合同测试；未做 8289 SSO 普通页面、四会话及并发实测。 |
| 部署 | **未部署、未切换、未发布**。54:18288 和 54:8289 原服务、容器及镜像保持运行身份；只读入口检查 HTTP 200。 |

资格集失败属于方案已预设的模型能力停止门槛，不是“代码和方案一致后出现的新问题”；用户所定最多三轮新问题修正并未被用作提示词反复调参。严格模式不得启用，18288 不进入发布步骤。详见 `QUALITY_OBSERVATIONS.md`。

## 执行过的验证

| 命令或检查 | 结果与边界 |
| --- | --- |
| 基线定向 `pytest -q`（自然来源/候选链/公共 API） | 修改前 34 passed。 |
| 受影响 Python 11 个测试文件 `pytest -q` | 修改后 **82 passed**；含来源、审核、会话、模板、公共 API。离线 harness 不等同真实 8289。 |
| `mypy` 全包 | **447 source files 通过**。 |
| 本次变更 26 个 Python 文件 `ruff check`、`ruff format --check` | 均通过；`git diff --check` 通过。 |
| 前端 `npm test -- --run` | **41 文件、232 tests 通过**；测试中预置的 synthetic session stderr 不计为失败。改动最后仅是测试代码的 lint 修正，相关 10 tests 再次通过。 |
| 前端 `npm run lint`、`npm run build` | 均通过；构建含 OpenAPI 类型核对与 `tsc --noEmit`，有非失败的大 bundle 提示。 |
| 冻结合成审核资格真实模型调用 | 10 次顺序请求，4 个高风险误接受，资格脚本返回失败；详见 `QUALITY_OBSERVATIONS.md`。 |
| Go 二进制摘要 | `vendor/weknora-chunker/bin/linux-amd64/wb-chunker` SHA-256 `491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f`；无 Go 改动，未运行 Go 重建或测试。 |

仓库级现存门禁未被掩盖：`ruff check .` 在本轮新增的两项修复后仍报 22 项，原基线相同文件也报同样 22 项；`ruff format --check .` 当前有 259 个文件待格式化，基线有 261 个。未批量修改这些无关文件。完整离线 `pytest -q -m "not local_integration and not live_provider"` 在早期出现多项失败后中止；随后 `pytest -x` 在 **114 passed** 后停于 `tests/adapters/parsers/docx/test_snapshots.py::test_all_fixed_fixtures_match_binary_and_semantic_snapshots`，同一测试在未修改的基线工作树也失败。未删除、放宽或标记忽略该测试。故不能报告全仓 Python 门禁通过。

GitHub CI、72+20 质量回放、8289 真实候选部署、SSO 页面登录与验证码交互、普通用户来源打开/反馈/Trace、四会话与同会话并发、18288 切换均**未执行**。这些步骤依赖 P0 能力门槛或可用的合格审核器；没有用离线测试代替它们。

## 资源与恢复

只读盘点确认 54:18288 通过 60:8288 接到 `wanshitong-wb08r-root-production-app`，54:8289 通过 60:18289 接到 `wanshitong-sso-candidate-app`，二者当时均 healthy。另一个 `wanshitong-wb08r06-production-app` 位于 60 回环 18288，不是用户访问链；仍在运行，不能作为过期镜像误删。详情在 `RUNTIME_MANIFEST.json` 与 `TEMPLATE_DATA_AUDIT.md`。

本轮没有构建、加载或启动 8289 新镜像，服务器和本机盘点均无可确认的“本轮过期 8289 镜像”。因此没有执行 Docker 全局 prune 或删除任何有容器引用的镜像。用于前端构建的本地 `node_modules` 符号链接和 `dist` 已精确清理；资格调用没有启动本地服务。原 8289 无需回退操作。

## 后续准出条件

在独立合格审核器或经过冻结负例重新证明可靠的模型可用后，先复核权限与受信模板别名，在隔离候选库用原件建立新的模板版本及候选索引，再完成 72+20 受控回放与逐原文审核、SSO 普通页面和并发验证。任何生产 18288 切换须以这些具体结果和用户后续授权为前提。当前仓库代码和报告作为**默认关闭的候选**交付。
