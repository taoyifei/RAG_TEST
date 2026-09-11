# 当前 V3 Universal RAG 状态

当前问答恢复的权威说明见
[V3-07 真正问答恢复状态](design/public/v3-07-query-quality-recovery.md)。旧 V3/P11 报告、
extractive 回归和早期 Review ZIP 只保留历史诊断价值，不代表当前 model-only 版本。

| 状态项 | 当前状态 | 已验证边界或恢复条件 |
| --- | --- | --- |
| `QUERY_DATA_PLANE_READY` | `PASS` | 响应、History、Trace 与 UI 使用同一冻结 Profile/Revision/provider/auth/budget 状态 |
| `MODEL_GROUNDED_QA_READY` | `PASS_SCOPED` | 此前 3 个代表性问题及本轮候选链 4 个口语化问题均真实调用 Jina+Qwen，并得到 `llm`、`CLAIMS_VALIDATED`、可回读引用和零 fallback；另有 1 次原问缓存复验为零 Provider 调用 |
| `SEMANTIC_RETRIEVAL_READY` | `PASS_SCOPED` | typed semantics、Exact/FTS/结构/Dense/RRF/Jina rerank、模型候选和最终引用进入正式 Product API |
| `CACHE_CITATION_RECHECK` | `PASS_SCOPED` | 长 canonical 段落的连续子引用可复核；预算耗尽后同身份命中缓存不再误变为 `BUDGET_BLOCKED`；越界、错身份、改字和过期授权继续失败关闭 |
| `ACTIVE_KB_LOCAL_RUNTIME` | `PASS` | App 与 Qdrant healthy，`/live`、`/ready` 200，产品资产自检通过，原持久卷保留 |
| `FIRST_REMOTE_INGESTION_RECOVERY` | `PASS_LOCAL_CANDIDATE` | migration 0028 已应用；两条既有首入库 Job 从永久 `pending/queued` 恢复为 `failed_retryable`，均为 `attempt=0`、`required_action=approve_retrieval`、`revision_available=false`；Jobs UI 不再读取不存在的 Revision，Provider 账本与授权 manifest 仍为空 |
| `CURRENT_PRIVATE_15_REGRESSION` | `NOT_RUN` | 旧 15/15 是已删除的 extractive 行为；需在当前 model-only 合同、明确额度和资料授权下重跑才可恢复 |
| `CURRENT_PUBLIC_HOLDOUT` | `NOT_RUN` | 旧 44/44 为 Provider call 0 的 extractive 结果；当前 80+ model-only holdout 未执行 |
| `CURRENT_BC_QUALITY_COMPARISON` | `NOT_RUN` | 当前候选尚无同配置 correctness/citation/false-refusal 对照；纯性能部分按用户范围排除 |
| `LIVE_FAILURE_MATRIX` | `CONTRACT_ONLY` | 授权、预算、429、timeout、无效 JSON 和 claim fail 有定向合同；未刻意制造真实 Provider 故障 |
| `REMOTE_QUALITY_CALIBRATION` | `NOT_VERIFIED` | `/ready` 的 primary/standby/reranker live evaluation 仍为 `not_verified`；这些代表性样本不能外推为广泛质量 |
| `FINAL_SOURCE_AND_IMAGE_IDENTITY` | `REQUIRED_AT_DELIVERY` | 最终提交合入后从 clean HEAD 构建，只切 App，再核 OCI revision、资产、健康和远端 SHA |
| `PRODUCTION_RELEASE_READINESS` | `BLOCKED` | 未执行生产部署；OS 风险、生产容量、正式质量校准与 Branch Protection 仍是外部边界 |

当前工作流仍遵守：

- 正式答案必须由模型读取有界原文候选后生成，再通过逐 claim 引用校验；没有模型时不得用
  本地表格规则或 extractive renderer 代答。
- 真实 Provider 只在当前知识库授权和累计预算内调用；AI 不代管理员批准资料出网或扩大预算。
- 首次远程入库必须先冻结 Job、资料集合、Profile、目标 Revision 和预算硬上限，再由管理员在
  “处理任务”中显式批准；未批准时保持可恢复且不得调用 Provider，也不得展示不存在的 Revision。
- 不用 Mock、旧 extractive 成绩、静态检查或容器健康冒充真实问答质量。
- 不运行与当前问答、引用、Provider、镜像和本机可用性无关的性能、P11、迁移或全仓测试。
- `main` 与 `Industry` 只读；只向 `feature/universal-rag` 普通 push，禁止 reset、stash、rebase、
  force-push 或自动合并主分支。
