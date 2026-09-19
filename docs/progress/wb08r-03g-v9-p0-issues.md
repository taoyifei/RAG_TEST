# WB08R-03G V9 P0 问题清单

状态：**OPEN / FAILED / PAUSED**。用户要求两次修改均未通过即停止并推送代码、详细问题记录。本轮两份功能候选均失败，已停止功能修改并回滚 8289。本文件记录未完成目标，不把它们写成已修复；后续建议仅供判断，不构成本轮第三次实验授权。

C1=`bddf54e5e9ca2bfd27bd80163cb6dfca24bc0a27`，C2=`2661ecf8e8b5aa4b7b78a7c4c625a49a0f09f22d`。两者共 32 次原 Preflight 请求，全部回答长度 0、引用 0。C1 行为合格 1/16，C2 2/16；后者仅 F024/F034 正确拒答，不能表述为答题能力恢复。C2 image=`sha256:9c5bb66ddda56aa83a465bff489186199241136cc88cc528eae93d9dacd8eb9d`。

完整 SAFE 数据、逐 claim hash/quote hash/stable key、版本包、命令和收据 hash 在 `wb08r-03g-v9-manifest.json`。私有材料根目录为 `C:/Users/jerry/AppData/Local/Codex/diagnostics/wb08r03g-v9-20260919`。下列行号均对应 C2 实测源码，不是文档提交新部署的代码。

## P0-01：N031 完整来源已发送，全部事实在三态关系门之前被拒

**影响与复现**：原 N031 三次，C1/C2 均 INSUFFICIENT_EVIDENCE、零回答/引用。每次 5 条生成 claim 全拒，两候选共有 30 条拒绝；C2 的 15 条全部有精确 origin。

**已证实链路**：同活动索引的规范表头确实存在。G01 行名经原通道进入；G02/G03 和规范表头在 `expand_neighbors` 合法闭合；生成包与实际 TRANSPORT_SENT 中均有同 11 个 protected stable key。C2 三次与 C1 的这 11 个来源，文档版本、节点、part/story、路径、跨度、quote hash 逐项一致；实际 HTTP body hash 相同，15 对 claim/quote hash、所选单一支持与 reason 也相同。C1/C2 均 review=0、repair=0。不是当前表头丢失或传输裁剪。

**准确断点**：`src/rag_app/application/answering/grounded.py::_validate_natural_entailment` 第 5553 行。触发条件：

```python
claim_actions = set(re.findall(_ACTION_VERB, _predicate(text)))
supported_actions = set(re.findall(_ACTION_VERB, source))
if claim_actions - supported_actions:
    raise ValidationFailed(..., code="CLAIM_RELATION_UNSUPPORTED")
```

该检查用候选 claim 与它选中的 quote 比较动作词；此处拒绝之后，不会到达最终三态 request-relation 判定，也不会成为可复核的 UNDETERMINED。这暴露了本轮“统一关系判定”的实际覆盖缺口：前置有限词法核验仍能先行截断。它是否在这些真实句子上构成误拒，必须依据实际 claim/quote 语义判断，不能只因最终没有答案就断言模型正确。

**已排除/未观测**：不能再将“表格交点证书是 N031 首个拒绝位置”当结论；C2 origin 已证伪该定位。具体动作词差集、完整原始生成正文和现场 covered_scope 未保存，保持 NOT_OBSERVED。历史表头第一次究竟在哪个旧阶段丢失，也仍 NOT_OBSERVED；本轮恢复位置不能反推历史丢失位置。

**两次尝试及剩余问题**：C1 完成 canonical 表头/阅读单元；C2 修复已进入 review 的表格 context 和既有交点摘录，但 N031 在更早的动作检查已终止，真实失败签名没有改变。没有通过改 error code 或降低覆盖阈值继续第三轮。

**供后续判断的最小证据与验收**：若重新授权，先保存同一 request/plan/claim/quote 与动作差集的可审计私有回放，区分“事实新增动作”与“来源已表达但词法不理解”。任何软化都必须继续拒绝甲的动作借给乙、跨阶段、无关列表和无根据条件，并在同固定反例和原 N031 三次上验证。不得把所有 CLAIM_RELATION_UNSUPPORTED 改为 UNKNOWN。

## P0-02：F015 从 V8 三次事实合格退化为零发布；C2 的结构修复未作用到真实复核输入

**原基线不能改写**：V8 的 F015 三次事实/来源合格，但系统覆盖 PARTIAL。C1、C2 都变成三次零事实，属于明确退化；不是仅 status 误报。

**A1 准确断点**：C2 `_validate_table_claim_certificate:5348`。它实际只引用 S18 规范列头；对应输入值在首个 SENT 的 S16。只选了表头，没有引用行名/值交点，不能凭来源包整体完整替该 claim 补证。本项不能泛称动作词门拒绝，也不能为了恢复通过直接把该 claim 标 SUPPORTED。

**A2 准确链路**：首次选择 S13（与原 Gold G03 值同源），进入 `_validate_natural_request_support:5299` 的 UNDETERMINED；实际 review 的来源仍仅 S13，与 C1 一样，没有加入首个 SENT 中 S1 行名/S19 规范列头。模型返回 supported，服务器最终为 `SCOPE_NOT_IN_BOUND_SOURCE`。三次一致。

**C2 做了什么**：本地 strict Fake HTTP 证明原 review 确实丢失 canonical 结构语境。新逻辑仅在当前目标、同表同列规范头、同真实行名、完整受信组、同已发送 reading unit 与 per-Atom 许可都能证明时加入 context；context 与原 claim supports 分开，模型不能把它当新增事实引用。既有 TABLE_INTERSECTION 的三个成员也可作为一条摘录一起过原最终事实门，并严格绑定当前 Atom。

**为什么不能说已修好**：真实复核输入未扩 context，是已观测事实；哪个具体准入谓词未满足还没有记录。root 解析 UNKNOWN 已观测，但当前实际 Atom.target 未观测，不能将它当已确认原因。本地显式目标测试通过不等于真实 Planner/Atom 接线完成。

**目标完整性**：三次 required/covered/missing 集合都为空，reason 为 TARGET_MEMBER_SET_UNPROVED，`source_complete=false`、`complete=false`。空 missing 不等于没有缺项；此时连必要集合都没被证明。不可直接移除 PARTIAL 检查或把物理完整升为任务完整。

**后续可证伪路径**：冻结实际 Atom 语义与 context 准入固定 reason，确认未加入 S1/S19 的第一条失败条件；用同 plan/evidence/claim/packet 复现，再决定是目标语义派生、合法结构识别还是原 claim 选引用的问题。恢复要求两个所问事实、来源与目标集合同时合格，保留非目标模式/角色和错误列头负例。

## P0-03：N033 新草稿混入另一张表，与旧连续 cell 问题是两条失败链

C1/C2 三次均零发布，C2 `_validate_natural_support_structure:4787`，原始 code=CLAIM_SOURCE_MISMATCH、公开 code=CLAIM_SUPPORT_NOT_OWNED；review=0。

实际选用 S1/S2 是同 node、同 `tbl13/tr1/tc1`，原文范围 0..41 和 41..52，连续；但 S4 来自另一表的 `tbl8/tr0/tc1`。将它们一起作为原事实 supports 不满足同一结构关系。此处有硬边界依据，不应因为最终题目必须回答就放过跨表引用。

旧保存草稿的完整服务回放已通过原同 cell 连续结构门，随后在 `_validate_bound_scope` 的阶段核验失败。旧回放与本轮真实新草稿跨表失败不能合并成“连续节点仍没修好”。两者均未证明所问角色在指定阶段的全部职责集合，不能用一个物理完整 cell 代替全部职责。

后续需首先固定当前草稿为何选入另一表头的身份链，并让合法职责支持保持来源/阶段/角色边界；再证明全部必要 stable member keys 被接受事实覆盖。不得跨角色凑齐列表、不得用改 Gold 或减少成员过门。

## P0-04：F013 和 N035 的模型 supported 与服务器范围核验仍不闭合

F013 原草稿三次 `_validate_natural_support_structure:4787` 拒绝。S3 为同 cell p1 的 2..22，S6 为同 node 的 0..22，存在重叠；S4 为同 cell p2。无原稿正文，不能仅凭坐标断言生成事实正确或把结构拒绝自动软化。

C1 的恢复候选触发一次批量 review，但输入估计 7505 超过 6144，发送前拒绝。C2 删除重复整份 QueryAnalysis、未引用大正文及冗余 span 信息，同时保留当前限制，预算降至 5018；三次都实际发送同 S3/S4/S6。每次两条结果为 supported，但六条全部 `SCOPE_NOT_IN_BOUND_SOURCE`；无发布。每次约 24.1–24.3 秒，其中 review 约 14.8–15.0 秒。

N035 同样从最终关系 UNKNOWN 进入 review，模型 supported 后被 scope 核验拒绝。不能将模型 supported 直接计为事实合格，也不能将“不发布”自动证明模型幻觉。

服务器在 `review_pending` 对返回 subject/relation/stage/conditions 与该候选原 quote、合法 context、所引来源 heading 做归一化包含核验。当前 SAFE 只记录总 reason，未记录哪个字段不满足，也未保留原 covered_scope，无法区分语义同义但词形不同、模型从问题补了对象、错误阶段或真实上下文缺失。这里的定位已经到明确决策分支；最终语义根因仍未知。

若后续授权，优先补受控的字段类别/摘要与固定输入回放，而不是继续添加业务同义词或删除 source-scope 约束。验收须同时证明原 quote 不变、合法 SENT 身份、来源条件未扩大、错误模型 supported 仍被拒、正确有限回答可发布。预算降低本身只解决发送条件，不证明语义成功。

## P0-05：A001 仍因补充复核预算失败，预期拒答验收未通过

C1/C2 均 BUDGET_BLOCKED、零引用。C2 的原草稿先结构拒绝，恢复进入四条候选的单批 review；该准备阶段估计输入为 6520 > 6144，HTTP 没有发送；首次生成估计 5683 已实际发送，因此不能把它写成首次生成预算失败。模型 status=NOT_OBSERVED、server reason=RELATION_REVIEW_INPUT_BUDGET_EXCEEDED。没有第三次调用，也没有把失败重试为新的预算。

原题要求安全拒答，预算异常不是合格的业务拒答。C2 F034 的预算问题已解除，模型返回 contradicted 后安全拒答合格，但 A001 不可借它的成绩。后续必须判断为何明确无关恢复仍需要高成本复核，以及无剩余预算时是否按既有有限/拒答语义收口；不得发布完整无关原句、不得扩大上下文或输出额度。

## P0-06：性能失败，成功事实样本仍为零

C1/C2 成功 Direct、成功生成均 n=0；任何总体“变快”都不能视作成功质量改善。C2 正确拒答 n=2，p95=21637.82ms，高于冻结的 8 秒观察线。F013 失败生成 p95≈24.25 秒，高于原生成 15 秒观察线。C2 八次实际 review 的应用端 p50/p95=10657.255/15014.094ms；独立单列，未隐藏入首次生成。

C1 generation HTTP=20（16+4），C2=24（16+8）；两份 review 准备均9次，分别5/1次无HTTP预算拒绝。repair均0。模型TTFT/queue未观测，首stage≤1秒不能证明模型首字延迟。完整桶定义、n、p50/p95及每次真实操作计数见报告/manifest。

本轮结论为 PERF_FAILED，不能标 DEMO_READY 或进入独立 Verification。后续不能以换模型、扩大timeout、删安全核验或只选最好一次成绩“修复”原观察线。

## 两份候选逐次对照

每行固定 run_id；trace_id 唯一。两候选全部 answer_chars=0、citation_count=0。C1/C2 elapsed 均为实际端到端毫秒，未剔除失败。完整逐 Claim 诊断在 manifest。

| run_id | C1 状态 / ms | C2 状态 / ms | C2 trace_id |
|---|---|---|---|
| `control-WB08R-A-001` | BUDGET_BLOCKED / 11883.00 | BUDGET_BLOCKED / 11695.16 | `trace_5910264f4c8095ec342833087a28a023` |
| `control-WB08R-F-024` | INSUFFICIENT_EVIDENCE / 7232.20 | INSUFFICIENT_EVIDENCE / 7071.06 | `trace_63e438c40d23690c5ab083ac7820f399` |
| `control-WB08R-F-034` | BUDGET_BLOCKED / 7729.32 | INSUFFICIENT_EVIDENCE / 21637.82 | `trace_d66f7320fd2133191b12026655b631aa` |
| `control-WB08R-N-035` | INSUFFICIENT_EVIDENCE / 13471.27 | INSUFFICIENT_EVIDENCE / 13365.21 | `trace_b77e7b63ea4cd1051b6c0bc4ea132278` |
| `repeat-1-WB08R-F-013` | BUDGET_BLOCKED / 9385.08 | INSUFFICIENT_EVIDENCE / 24252.99 | `trace_d8432326f46450b743e825b7235a44a0` |
| `repeat-1-WB08R-F-015` | INSUFFICIENT_EVIDENCE / 21225.36 | INSUFFICIENT_EVIDENCE / 18876.33 | `trace_d0e681875906c9b3a20be2f1a65f8fa3` |
| `repeat-1-WB08R-N-031` | INSUFFICIENT_EVIDENCE / 16502.84 | INSUFFICIENT_EVIDENCE / 16303.53 | `trace_df5afa557444c8bbc047fca7a2072d0b` |
| `repeat-1-WB08R-N-033` | INSUFFICIENT_EVIDENCE / 7111.84 | INSUFFICIENT_EVIDENCE / 6928.01 | `trace_ff5fd66a631530483cb654bcc24bf1d6` |
| `repeat-2-WB08R-F-013` | BUDGET_BLOCKED / 9218.15 | INSUFFICIENT_EVIDENCE / 24172.70 | `trace_0d2e3e865e934b1567f6f21da3ca5e4d` |
| `repeat-2-WB08R-F-015` | INSUFFICIENT_EVIDENCE / 19145.00 | INSUFFICIENT_EVIDENCE / 18985.03 | `trace_7c78df2c800268af9ab1ad197029e6eb` |
| `repeat-2-WB08R-N-031` | INSUFFICIENT_EVIDENCE / 16380.03 | INSUFFICIENT_EVIDENCE / 16288.49 | `trace_efdf7e6a120c990c23f13a2fe4164b4d` |
| `repeat-2-WB08R-N-033` | INSUFFICIENT_EVIDENCE / 6918.55 | INSUFFICIENT_EVIDENCE / 7179.29 | `trace_0c1d70355929c127aff4b128641e4793` |
| `repeat-3-WB08R-F-013` | BUDGET_BLOCKED / 9204.69 | INSUFFICIENT_EVIDENCE / 24105.66 | `trace_cd5554d26c0ee2420c90d6756d48ef20` |
| `repeat-3-WB08R-F-015` | INSUFFICIENT_EVIDENCE / 19257.73 | INSUFFICIENT_EVIDENCE / 18866.03 | `trace_64c044a65e5063c6952e5e8b967e8ce7` |
| `repeat-3-WB08R-N-031` | INSUFFICIENT_EVIDENCE / 16259.57 | INSUFFICIENT_EVIDENCE / 16236.25 | `trace_254e54f924f17043d98ef49713a56554` |
| `repeat-3-WB08R-N-033` | INSUFFICIENT_EVIDENCE / 7024.00 | INSUFFICIENT_EVIDENCE / 7087.24 | `trace_8ec108a50a09988870fb2386e833872e` |

## C2 的实际拒绝位置与复核结果

位置指真实异常 traceback 的最后一个 rag_app 帧；不是默认 validator 标签猜测。只给行号不能证明语义误拒，需结合上述边界。

| run_id | 原始拒绝与实际函数:行 | 复核服务器结果 |
|---|---|---|
| `control-WB08R-A-001` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | NOT_OBSERVED → RELATION_REVIEW_INPUT_BUDGET_EXCEEDED |
| `control-WB08R-F-024` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | 无复核 |
| `control-WB08R-F-034` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | contradicted → MODEL_NOT_SUPPORTED |
| `control-WB08R-N-035` | CLAIM_QUERY_RELATION_UNDETERMINED · _validate_natural_request_support:5299 | supported → SCOPE_NOT_IN_BOUND_SOURCE |
| `repeat-1-WB08R-F-013` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | supported → SCOPE_NOT_IN_BOUND_SOURCE |
| `repeat-1-WB08R-F-015` | CLAIM_RELATION_UNSUPPORTED · _validate_table_claim_certificate:5348; CLAIM_QUERY_RELATION_UNDETERMINED · _validate_natural_request_support:5299 | supported → SCOPE_NOT_IN_BOUND_SOURCE |
| `repeat-1-WB08R-N-031` | CLAIM_RELATION_UNSUPPORTED · _validate_natural_entailment:5553 | 无复核 |
| `repeat-1-WB08R-N-033` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | 无复核 |
| `repeat-2-WB08R-F-013` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | supported → SCOPE_NOT_IN_BOUND_SOURCE |
| `repeat-2-WB08R-F-015` | CLAIM_RELATION_UNSUPPORTED · _validate_table_claim_certificate:5348; CLAIM_QUERY_RELATION_UNDETERMINED · _validate_natural_request_support:5299 | supported → SCOPE_NOT_IN_BOUND_SOURCE |
| `repeat-2-WB08R-N-031` | CLAIM_RELATION_UNSUPPORTED · _validate_natural_entailment:5553 | 无复核 |
| `repeat-2-WB08R-N-033` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | 无复核 |
| `repeat-3-WB08R-F-013` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | supported → SCOPE_NOT_IN_BOUND_SOURCE |
| `repeat-3-WB08R-F-015` | CLAIM_RELATION_UNSUPPORTED · _validate_table_claim_certificate:5348; CLAIM_QUERY_RELATION_UNDETERMINED · _validate_natural_request_support:5299 | supported → SCOPE_NOT_IN_BOUND_SOURCE |
| `repeat-3-WB08R-N-031` | CLAIM_RELATION_UNSUPPORTED · _validate_natural_entailment:5553 | 无复核 |
| `repeat-3-WB08R-N-033` | CLAIM_SOURCE_MISMATCH · _validate_natural_support_structure:4787 | 无复核 |

## 证据索引与停止条件

- `n031-c2-review.safe.json`：11 来源身份、15 对 claim/quote hash、实际 HTTP hash、代码第5553行三次对照；`p0-n031-final-draft.md`保留更细的私有结构审查。
- `duty-c2-review.safe.json` / `duty-c1-c2-comparison.private.json`：9次职责/命名行的来源坐标、复核包、预算、origin和目标集合。
- `case-diagnostics-c1.safe.json` / `case-diagnostics-c2.safe.json`：32次完整SAFE拒绝、复核状态、原始reason、hash与许可集合。
- `preflight-c1-full.private.json` / `preflight-c2-full.private.json`：原始观测、私有答案、SAFE trace；不提交原正文。
- `performance-c1-v2.safe.json` / `performance-c2.safe.json`：成功/失败分桶、真实调用与History回读边界。
- `review-context-c1-verified-failure.log`、`canonical-extract-before.log`、`canonical-extract-atom-scope-before.log`及对应after：C2使用第二份候选的可失败本地证据；无效PYTHONPATH旧基线运行已作废，不拿来证明。
- `deploy-c1-verified.safe.json`、`deploy-c2.safe.json`、`rollback-c1.safe.json`、`rollback-c2.safe.json`、`runtime-final.safe.json`：候选/生产身份和回滚。

原始 Gold、原题和测试分母均保留；原10个未跟踪结果和P0/V8材料未提交未删除。旧V8 FAILED保持；新V9单列FAILED/PAUSED。A～F、04～06、合并、生产发布未执行。候选已回滚bd00，生产604ef63未变。这里列出的下一步仅用于用户判断，当前没有第三份候选或继续运行计划。
