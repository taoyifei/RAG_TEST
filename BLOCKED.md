# 当前 V3 Universal RAG 状态

当前查询恢复状态以
[V3-07 查询质量恢复报告](design/public/v3-07-query-quality-recovery.md)为权威入口；
旧 V3/P11 报告保留历史证据，但其中的候选 SHA、镜像和查询结论不代表当前版本。

| 状态项 | 当前状态 | 已验证边界或恢复条件 |
| --- | --- | --- |
| `QUERY_DATA_PLANE_READY` | `PASS` | 响应、History、Trace 与 UI 使用同一冻结运行时状态；本地 fallback 不再冒充真实 Dense |
| `DIRECT_EVIDENCE_QA_READY` | `PASS` | 公开 holdout 40/40 可答、4/4 正确拒答；私有已知可答 15/15，另有 5/5 负例 |
| `SEMANTIC_QUERY_READY` | `PASS` | 类型化语义、硬约束、受控 interpret/rewrite、结构通道和最小充分支持集进入正式 Product API |
| `PRIVATE_KNOWN_REGRESSION` | `PASS` | 仅本地隔离 Product API；公开材料只含匿名计数，不含原件、问题、答案、Trace 或 hash |
| `PUBLIC_HOLDOUT` | `PASS` | 冻结 88-case 数据集；holdout 只运行一次，accuracy/citation/paraphrase/status 均 1.0，硬约束违规 0 |
| `STATUS_AND_TRACE_CONSISTENCY` | `PASS` | 已发布 fallback 为 ANSWERED，正常无答案为 REFUSED，错误为 FAILED；公开和私有回归 parity 1.0 |
| `CONTAINER_QUERY_QA` | `PASS` | 候选镜像中真实上传合成 DOCX、自然问句回答、FULL Trace、桌面/移动浏览器、重启持久性均通过；外部调用 0 |
| `SCOPED_FUNCTIONAL_GATES` | `PASS` | 用户本轮要求的查询、Product API/E2E、公开/私有回归、前端与 Docker 功能门禁已执行 |
| `FULL_LOCAL_GATES` | `NOT_RUN_BY_SCOPE` | 用户明确暂缓 CI、mypy、无关全仓测试与完整发布/离线包验收；不能写成 PASS |
| `LIVE_RETRIEVAL_READY` | `BLOCKED` | 没有已授权的真实 Provider/Profile/预算运行证据；需管理员明确授权 Lane R 后执行 |
| `LIVE_GENERATION_READY` | `BLOCKED` | 私有语料未获数据出网与累计预算授权；AI 不代管理员批准 |
| `SOURCE_MERGE_READINESS` | `PENDING` | 需对最新远端目标执行隔离 `--no-ff` 合并，并在 merged tree 重跑约定功能门禁 |
| `PRODUCTION_RELEASE_READINESS` | `BLOCKED` | 未执行生产部署；OS 风险、CI/Branch Protection 与真实 Provider 质量仍须独立批准和验证 |

`PASS` 只覆盖对应行的实际证据。离线 Mock、公开合成、私有本地 renderer、源码合并和
候选容器分别是不同边界，任何一项都不能替代真实 Provider 或生产发布证据。

当前工作流仍遵守：

- `main` 与 `Industry` 只读，不自动合并或更新；
- 只向 `feature/universal-rag` 做普通 push，禁止 force-push；
- 合并前重新 fetch 并确认目标没有未知前进；
- 私有原件、问题、答案、文件名、Trace、数据库、凭据和 hash 不进入 Git、Review ZIP
  或 Docker build context；
- 真实 Provider 与生产发布必须由管理员另行授权。
