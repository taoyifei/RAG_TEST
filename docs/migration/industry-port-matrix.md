# Industry 通用修复移植矩阵

## 审计边界

- 对比基线：`origin/main@af30f81`。
- 只读参考：`origin/Industry@5cc5d7b`。
- merge-base：`af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`。
- Industry 独有提交：14 个；三点差异为 107 个文件、29,024 行新增、518 行删除。
- 本阶段没有 merge 或 cherry-pick Industry。混合提交只按下面标明的最小模块补丁重实现。

处置含义：`PORT` 已在阶段 0 移植；`REIMPLEMENT` 方向通用但必须在后续阶段按
通用端口重新实现；`KEEP_IN_INDUSTRY` 只服务工业部署；`DECISION` 现有证据不足。

## 逐提交矩阵

| Industry 提交 | 受影响模块与证据 | 处置 | 阶段 0 结果 |
|---|---|---|---|
| `0b1d93d` 新增隔离式工业部署链 | `deployment/industry`、`evaluation/industry`、工业 corpus/bundle 脚本与测试 | `KEEP_IN_INDUSTRY` | 不复制语料、镜像、恢复或服务器验收资产 |
| `0b1d93d` 同上 | `scripts/verify_model_contracts.py` 为回答请求补 `question_profile`；`tests/test_release_safety.py` 移除脆弱固定文件数 | `PORT` | 分别以 `a3a85b0`、`45f08d2` 最小移植并回归 |
| `5ce5870` 工业 simple 部署增量 | `generation/answer.py`、`evidence.py`、`model_contracts.py` 建立显式主体支持门禁；回答测试为合成证据 | `REIMPLEMENT` | 仅将 `2c4cf22` 所需的通用最小门禁并入 `eb99adf` |
| `5ce5870` 同上 | Industry deployment、smoke 数据、bundle 与 pipeline 工业修订 | `KEEP_IN_INDUSTRY` | 不移植 |
| `2c4cf22` 精确校验显式来源编号 | 回答校验、fallback、模型合同、pipeline 指纹、合成回答测试 | `PORT` | `eb99adf` 重实现；覆盖重叠标题、多编号和流式提前发布 |
| `2c4cf22` 同上 | 根级 `BLOCKED.md`、`PROGRESS.md` 工业验收叙述 | `KEEP_IN_INDUSTRY` | 不移植 |
| `cd5e377` 部署诊断文档 | 工业服务器诊断和剩余验收卡点 | `KEEP_IN_INDUSTRY` | 不作为通用运行时证据 |
| `ff8c9d2` UI 与应用更新候选 | `api/ui_session.py`、FastAPI 路由、bearer 前端、Cookie/会话配置 | `REIMPLEMENT` | 涉及公共 HTTP 鉴权和前端，本阶段禁止改 API，留待独立威胁模型与兼容设计 |
| `ff8c9d2` 同上 | Trace 问题捕获、store/recorder schema、管理 API | `REIMPLEMENT` | 保留现有 Trace；后续先定义保留期、脱敏和旧库迁移 |
| `ff8c9d2` 同上 | Industry app-update 构建器和部署脚本 | `KEEP_IN_INDUSTRY` | 不移植 |
| `809fb71` 加固 UI Trace 与应用更新 | `tests/test_app_update_builder.py` 对齐 main 已采用的 simple 三文件实现；runtime fixture 补现有 intent-router 路径 | `PORT` | `45f08d2`；62 个相关测试通过 |
| `809fb71` 同上 | `api/stream.py`、LLM/evidence/query/cache 的缺失 Google docstring | `PORT` | `9bcf8e0` 仅补文档，去除 docstring 后 AST 相同 |
| `809fb71` 同上 | UI session、Trace、simple/Industry 部署及设置 | `REIMPLEMENT` / `KEEP_IN_INDUSTRY` | 通用安全方向后续重实现；部署资产不移植 |
| `d5c03cf` serving 更新兼容 | `clients/resilience.py`、Embedding/Reranker/OCR 调用点及韧性测试 | `PORT` | `cbf1d48`；坏 Content-Type/JSON/schema 仅对非生成式请求有限切换 |
| `d5c03cf` 同上 | UI/Trace/settings 通用方向 | `REIMPLEMENT` | 需要公共配置和持久化兼容设计，本阶段不改 |
| `d5c03cf` 同上 | Industry serving updater、回滚、自检、last-good | `KEEP_IN_INDUSTRY` | 不移植 |
| `8755bf3` 真实服务器更新加固 | Industry compose canonical、权限、镜像和 UI contract checker | `KEEP_IN_INDUSTRY` | 属于服务器与镜像验收 |
| `195f9ac` 关闭更新事务缺口 | Industry finalize/update/verify 和事务测试 | `KEEP_IN_INDUSTRY` | 不移植 |
| `195f9ac` 同上 | `tests/test_target_verifier.py` 两行兼容调整 | `DECISION` | 没有独立通用失败证据，不单独移植 |
| `e5844e5` legacy last-good 恢复 | Industry last-good/runtime/update 及恢复测试 | `KEEP_IN_INDUSTRY` | 不移植 |
| `a50d5d5` activation/canary 可恢复 | Industry rollback core、update、last-good 和脚本测试 | `KEEP_IN_INDUSTRY` | 不移植 |
| `e5c98ce` 不可用目标回滚 | Industry rollback/update/builder 与脚本测试 | `KEEP_IN_INDUSTRY` | 不移植 |
| `82e9537` updater 绑定部署基线 | Industry updater/runtime/builder；simple bundle 的镜像复用方向 | `KEEP_IN_INDUSTRY` / `REIMPLEMENT` | 工业实现不移植；通用增量发布合同另立阶段设计 |
| `82e9537` 同上 | Trace 问题捕获兼容测试 | `REIMPLEMENT` | 与 `ff8c9d2` 的存储兼容方案一并处理 |
| `5cc5d7b` 固化 Industry 源镜像身份 | Industry 源镜像、rollback、last-good、serving selfcheck | `KEEP_IN_INDUSTRY` | 完全留在 Industry |

## 重点主题结论

- 来源编号与回答引用：`2c4cf22` 的通用 correctness 修复已最小重实现，并用合成证据
  覆盖错误重叠标题、多个编号与流式发布边界。
- 模型响应韧性：`d5c03cf` 的非生成式 failover 已移植；LLM 仍默认不对无效生成
  重放，避免重复副作用。
- UI 会话安全：Industry 方案同时改变 FastAPI 路由、Cookie、前端和部署配置，不能在
  阶段 0 作为“修复”偷渡公共 API，标记为 `REIMPLEMENT`。
- Trace：问题捕获、保留期和旧库兼容方向有价值，但涉及持久化和管理 API，标记为
  `REIMPLEMENT`；当前安全 Trace 不被删除或放宽。
- 部署路径：`deployment/industry`、工业 bundle/corpus、服务器更新和恢复资产全部
  `KEEP_IN_INDUSTRY`；通用分支不包含这些目录。
