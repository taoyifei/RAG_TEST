# WB08R-03G V14：统一问答计划实施与严格候选报告

V14 最终状态为 **FAILED / PAUSED**，`merge_allowed=false`，
`production_publish_allowed=false`。统一问答计划和执行内核已经实现并完成相关离线门禁，
但严格对齐后的 C4 候选在五组真实回放中出现一个新的 P0：危险时序问法没有发布错误
关系，却同时丢失了来源可确定的输入项基础事实。按用户规定，本轮不再修改算法，不进入
同版本 Preflight-16 或 A～F，已保存证据、回滚 8289、精确清理候选并提交结果。

详细阻塞和恢复合同见 [V14 P0 清单](wb08r-03g-v14-p0-issues.md)。候选身份、证据摘要、
运行边界和清理清单由同目录 V14 manifest 固化。

## 实施结果

V14 没有继续为 F024、N031 或具体文档增加特判，而是把 03 的控制层收敛为一份冻结的
`CompiledAnswerPlan`：

- 显式来源在 QueryAnalyzer、Planner 和 Rewriter 前解析为文档版本许可；比较问法使用
  不相交的逐子句来源范围。
- 物理选择、必答成员、正向依赖、限定和执行任务在生成前冻结。覆盖计算只消费冻结计划
  与受检产物，不再从原问题或最终引用重新推导 required set。
- `executor.py` 统一调度 D/G 两类任务。可确定表格字段走服务端直接执行，回答阶段生成与
  语义复核均为零；开放正文才进入有限生成和互不重叠的真实复核批。
- 固定 24 个假想 Claim 的复核预检已从生产链路删除；没有提高 6144 输入上限、增加
  timeout、恢复自动 repair、删除安全绑定或放宽来源合同。
- 新路径失败时不回退 legacy Wire、摘录兜底或旧完整性算法。

实现按功能意图形成以下提交：

- `8fd7833ecb38035d7874df888a1fdb5aceeed0a8`：建立统一回答计划和执行内核。
- `b34628f1db11500f1511acf8c91921d98500690d`：保留计划中的业务目标语义。
- `32ebc7121eea0119441ccb88c7250a1113d27cc2`：收紧回答义务和物理选择边界。
- `ead4f2c29d90cd7a8242d6db738c169f32246d92`：完成 V14 执行合同对齐。
- `35b6e3c9e17cf61690f91afeaf76c9f374b0e757`：记录 C4 前的严格一致性审计；C4 镜像以
  该完整源码树作为 revision。

## 严格候选前的纠偏边界

C1～C3 留下了真实诊断证据，但不作为“严格 V14 已通过”的候选：

| 候选 | 代码 | 已执行 | 未严格对齐的表现 | 处理 |
|---|---|---:|---|---|
| C1 | `8fd7833` | 五题 + 16 条历史 Preflight | 危险时序仍是 G 路径，2 次生成、1 次复核；Preflight 摘要为 `NOT_REVIEWED` | 只作诊断，不计通过 |
| C2 | `b34628f` | 五题 + 16 条历史 Preflight | 危险时序仍未拆成“基础事实 + 待核实限定”；Preflight 仍未完成人工事实裁决 | 继续修正实现偏差 |
| C3 | `32ebc71` | 五题 | 显式列名和危险时序仍走 G 路径，均为两个义务、2 次生成、1 次复核 | 停止真实请求，做完整离线一致性审计 |

用户明确要求“先确认代码按 V14 方向和细节一致，再做真实测试”后，C4 前没有继续业务
请求。审计确认并修正了逐来源比较范围、限定跨 Atom 传播、G 批次调度归属、等价目标
selection identity 和回放证据字段等实现偏差。这些修正属于“此前没有严格执行计划书”的
例外，不属于新问题后的自行补丁。完整清单见
[V14 真实测试前一致性审计](wb08r-03g-v14-prelive-alignment.md)。

C1、C2 各 16 条 Preflight 记录只证明请求执行和协议观测存在；两份摘要均明确写有
`preflight_gate=NOT_REVIEWED`，因此不被本报告宣称为质量通过，也不能替代 C4 的同版本
门禁。C4 严格五题失败后没有运行新的 Preflight-16。

## 只运行了与 03 问答直接相关的离线验证

没有运行全仓测试、前端、迁移、索引重建或其它功能测试。

- V14 改动模块专项 pytest：`399 passed in 14.99s`。
- 变更范围 Ruff format：`31 files already formatted`。
- 变更范围 Ruff：`All checks passed`。
- 18 个相关生产源码和证据脚本 mypy：`Success: no issues found`。
- 新执行内核、计划编译器和前置来源解析独立导入：通过。
- `git diff --check`：通过。

这些结果只证明实现合同和相关回归通过，不替代真实问答质量验收。

## C4 冻结身份

C4 在第一条业务请求前完成冻结，`business_requests=0`、`provider_calls=0`：

- code SHA：`35b6e3c9e17cf61690f91afeaf76c9f374b0e757`
- image tag：`rag-test-wanshitong:wb08r03-v14-c4-35b6e3c`
- image ID：`sha256:cf392ff6adc033c1aa9878eb0f3d8e69d6b52681f17a87bda034ef891f86a607`
- runtime tree：`732cdcf33c6cca700a1dfd9f48dfec0d76736d5bb40bdc273903e17d18100d95`
- runtime guard：`7b448024dce3f1fecc7dcd68ed70ddc5b92e1f0ffb0c16668a02f7a28a82b1c4`
- version bundle：`6228fecd78962b91e2d7106b96928f38f202728fe0e97627f285a37f4644da1a`
- version bundle 文件：`d1184aa91d289b3d3249614ec8caa3b2cc723fa5250696e3c54ad76b3fed9d91`
- asset manifest：`8a17ea7a636889a9024923babef0f08cd3bdf3e49026b01556f0d534a2c25b50`
- configuration：`ee6db0601c1fea57936194f868ff508ca28498c893a5c76efcadf19c72f55f47`
- serving fingerprint：`sha256:189907352ec14c09849a0bc05f3beaf8d2352a642c07c2b7fe322f502fe728a8`
- 活动索引：`irev_0329a35ca9ea700133f7b309160118f0`
- Answer Plan：`wb08r-answer-plan-v2` / `wb08r-answer-plan-policy-v2`
- Prepared Packet：`wb08r-prepared-packet-v4`

镜像在本地和 60 服务器的 ID、revision/base 标签一致；构建产物的产品资产自检通过，
37 个资产文件已验证。部署只覆盖 60 服务器的 `127.0.0.1:8289`，`.env` 和
`compose.yaml` 的 SHA-256 在部署前后不变。

## C4 五组真实回放

C4 只执行一次五题回放，没有重试。五题均只有一个 `final`：

| 场景 | 终态 | 实际调用与覆盖 | 人工裁决 |
|---|---|---|---|
| F024 | `INSUFFICIENT_EVIDENCE` | 0 回答、0 引用、0 生成 | 通过；指定模板正文不能由他文替代 |
| N031 | `ANSWERABLE/FULL` | D 路径，0 生成、0 复核，3 引用 | 通过；给出材料清单，无“之前/必须”注入 |
| 显式来源 | `ANSWERABLE/FULL` | D 路径，0 生成、0 复核，3 引用 | 通过；唯一正确文档和输入字段 |
| 显式列名 | `ANSWERABLE/FULL` | D 路径，0 生成、0 复核，3 引用 | 通过；与显式来源使用同一 selection digest，无预算阻断 |
| 危险时序 | `INSUFFICIENT_EVIDENCE` | 0 引用、0 rerank、0 生成、0 复核、0 发布 | **失败；安全基础事实被整体丢弃** |

前四题证明 V13 的三项决定性阻塞已经消失：显式来源不再假 `PARTIAL`，显式列名不再被
假想复核预算阻断，F024/N031 的来源与关系边界继续保持。危险时序题也没有发布无依据的
“之前/必须”，但其冻结计划出现：

- `selection_digests=[]`
- `task_modes=[GROUNDED_GENERATION]`
- `missing_qualifier_ids=[Q1,Q2]`
- `reranking=0`、`generation_calls=0`、`relation_review_calls=0`
- `published_claim_count=0`、`answer_path=NO_ANSWER`

V14 要求在来源只支持“输入”时保留输入项基础事实，并将 BEFORE/MUST 限定标为未确认
或限答；不能因为限定不成立而抹掉基础字段。因此这是严格实现后首次观察的新 P0，而不是
可以继续按题修补的历史偏差。详细代码断点见 V14 P0 清单。

## 停止门禁

按用户规则，C4 新 P0 出现后：

- 没有创建 C5，也没有修改 `plan_compiler.py`、提示词、预算、timeout 或任何测试断言。
- C4 同版本 Preflight-16 未运行。
- EvidencePack-12、CoreAnswer-16、Failed-24、Natural-36、Full-96 和
  Concurrency-4 均未运行。
- 没有用 C1/C2 的未评审 Preflight 结果替代 C4 同版本门禁。
- 没有生产发布，也没有开始 04。

## 回滚、生产边界与清理

- 8289 已回滚到 `rag-test-wanshitong:wb08r03f-bd00c56`；最终容器 ID
  `b2d8ffb8778cee211c518d4f9ab88c417747151f31ed28c493a064ff418c8589a`，image ID
  `sha256:621acc792c12169d347fde03a8cfb06dff4ca033bf4867171d35bc46f284b75c`，
  `/live` 与 `/ready` 均为 200。
- 生产容器 ID
  `7f46e6d98effc008a62d31313b576db9b92b656923f1ca4b2194ffb02860236e`、image ID
  `sha256:97826be2208718be61d37921705f595c3c0056d57a2384ffc54ffdaf66380306`
  和启动时间前后不变；60:8288 与 54:18288 `/live` 均为 200。
- 未修改 Qdrant、活动索引、业务数据、生产 Compose、生产容器或生产镜像标签。
- 60 上 4 个 V14 候选镜像、22 个明确 V14 临时路径已删除；54 没有 V14 镜像，12 个
  明确 V14 中转路径已删除；本地删除 C4 候选镜像和 11 个明确 V14 构建/传输路径。
- 本地、54、60 的 V14 临时项最终均为零；未执行全局 prune。
- 原有 10 个未跟踪评测文件没有纳入提交，最终哈希与任务开始时记录一致。

## 私有证据

回答正文、引用正文和逐请求 Trace 未进入 Git。C1～C4 证据已保存到：

`/home/jerry/work/RAG-private-evidence/wb08r03-v14-20260920/`

| 候选 | 文件 | SHA-256 | 大小 |
|---|---|---|---:|
| C1 | `wb08r03-v14-c1-evidence.private.tar.gz` | `aa9a292000625790ad1e0014b724549223e027945b1a22f527f15fe8e614d45b` | 192675 |
| C2 | `wb08r03-v14-c2-evidence.private.tar.gz` | `efdb0fbee382f60830da912f3349519c1283d3a221fcf4e18af6e94f16411eb8` | 200524 |
| C3 | `wb08r03-v14-c3-evidence.private.tar.gz` | `86b8ae87b5caf6d7c030d4504e38f36aab29b7fea9f08400ab06ca8643dd3f2f` | 130395 |
| C4 | `wb08r03-v14-c4-evidence.private.tar.gz` | `e53ca4938ce8addd6824437cac3b8a09de596d40f59b06d69459c750e20bccfb` | 77985 |

目录权限为 700，四份文件权限为 600；传输前后 SHA-256 一致，四份 gzip 完整性检查
通过。服务器上的唯一证据只在本地副本验证完成后删除。

为便于脱离服务器逐字段分析，C4 还在 Windows 桌面保存了一份未压缩分析包：

`C:\Users\jerry\Desktop\湾研院\湾事通\WB08R03_V14_C4_P0_分析证据_35b6e3c\`

该目录包含完整 `private-replay.ndjson`、按五个 case 拆分的格式化 JSON、关键字段对比摘要、
无第三方依赖的检查脚本、测试提交导出的 32 个相关源码/测试文件、V13 到 C4 的问答差异、
运行身份、V14 任务书和本报告。`SHA256SUMS.txt` 覆盖除自身外的全部文件。该目录含内部
问题、答案、引用和文档路径，未纳入 Git，也没有写入密码、Token 或 `.env`。

## 冻结结论

V14 保留在分支供审计，但不具备生产替换资格。恢复工作需要新的明确授权；恢复时应先
处理 P0 中“基础字段选择与强限定满足状态必须独立”的合同断点，再从专项离线回归和同一
五题回放开始。当前不得继续创建候选或用完全拒答冒充安全的部分回答。
