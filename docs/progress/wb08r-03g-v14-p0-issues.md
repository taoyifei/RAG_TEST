# WB08R-03G V14：严格五题回放后的 P0 清单

V14 状态为 **FAILED / PAUSED**，`merge_allowed=false`，
`independent_verification_ready=false`。C4 是完成 V14 一致性审计后的严格候选；它修通了
显式来源假部分、显式列名预算阻断，并保持 F024/N031 安全边界，但危险时序问法丢失了
可确定的基础字段答案。根据用户的停止规则，本清单只记录新问题、证据、代码断点和恢复
门槛，不实施修复。

## P0-01：强限定未获支持时，基础物理字段也被计划编译器丢弃

### 复现输入与预期合同

严格回放场景是：

> 《开发中心三种工作模式》中，需求快验之前必须准备哪些材料？

来源中的闭合表格事实只证明“需求快验”行的“输入”字段及其材料清单，没有直接证明
BEFORE 或 MUST。V14 任务书要求把两层事实分开：

1. 发布来源明确支持的输入项基础事实；
2. 将“之前”“必须”标为未获直接依据，整体不得报严格限定完整满足；
3. 不得把“输入”升格成前置义务，也不得因为限定未获支持而删除基础事实。

该合同对应附件验收 P05，以及实施方案中“如果来源只列输入，输出输入项并说明严格前置
义务尚无直接依据”的规定。

### C4 实际观察

C4 的来源解析本身正确：

- `resolution_stage=PRE_ANALYSIS`
- `source_intent=DOCUMENT_AUTHORITY`
- `resolution=RESOLVED`
- `allowed_document_count=1`

但后续计划和发布结果为：

- `selection_digests=[]`
- 唯一任务模式为 `GROUNDED_GENERATION`
- obligation `O1` 的 `missing_qualifier_ids=[Q1,Q2]`
- coverage 为 `MISSING`，`source_closed=false`
- reranking、generation、relation review 均为 0
- generated、accepted、published Claim 均为 0
- `answer_path=NO_ANSWER`、0 引用、空答案
- 外部终态为 `INSUFFICIENT_EVIDENCE`，且只有一个 `final`

因此系统没有产生不安全时序，但也没有保留已经存在、且在 N031、显式来源、显式列名三题
中稳定发布的同一输入项事实。这是安全的过度拒答，也是 V14 必答范围合同失败。

### 代码断点

问题已收敛到
`src/rag_app/application/answering/plan_compiler.py` 的物理事实选择链，而不是 Provider、
预算或来源解析：

1. `_candidate_facts()` 能从已绑定事实计算 `same_target` 和
   `qualified_same_target`。
2. `_binding_matches_fact()` 同时要求目标和关系标签匹配。危险时序问法的关系表达是
   “准备/材料”，来源表头是“输入”，因此本次 `semantic` 集合没有形成基础字段锚点。
3. `_select_fact_bindings()` 的 `QUALIFIED_BASE_FACT` 分支要求
   `qualified_same_target and semantic` 同时成立。只有同目标且带限定、但关系没有直接匹配
   “输入”时，它返回空选择。
4. 没被选择的 Atom 在 `compile_answer_plan()` 最后一段被降为无成员依赖的
   `OPEN_TEXT` obligation；限定 Q1/Q2 被保留，但输入字段和 value dependencies 已丢失。
5. executor 面对没有可绑定 Claim 的 G 任务安全停止，于是出现 0 rerank、0 generation、
   0 publication。运行时行为与上述静态路径一致。

换言之，当前合同把“能否定位基础字段”和“强限定是否被来源证明”耦合在同一个选择条件
里。V14 已经把限定保存下来，却没有把基础字段选择独立保留下来。

### 为什么属于新 P0

- C4 前的严格离线审计已经确认来源预解析、冻结计划、执行器归属、D/G 批次、覆盖纯函数、
  Wire 和预算路径均按 V14 落地；相关门禁为 399 passed、Ruff、mypy、导入和 diff 检查
  全部通过。
- C4 同一版本的显式来源和显式列名都以相同 selection digest
  `sha256:363cb5e0b4a404143b5b5b3b324df268eadef5135649e092eaa15882963268bf`
  走 D 路径并得到 `FULL`，说明文档、行、字段、事实和依赖本身可用。
- 危险时序题没有触发旧的 `SEMANTIC_REVIEW_PREFLIGHT_BUDGET_EXCEEDED`，也没有 Provider
  传输或输出协议错误；失败发生在生成前的计划选择。
- 该问题不是 C1～C3 已知的“不严格实现偏差”再次出现，而是严格 C4 对 P05 的真实反例。

按用户指令，这一证据出现后没有修改 `_select_fact_bindings()`、限定解析、提示词、预算、
timeout、repair、测试断言或任何运行配置。

### 影响

- 安全性：本例没有发布无依据时序，未观察到错误来源或关系注入。
- 可答性：来源明确支持的材料清单被完全隐藏，构成严重假拒答。
- 完整性：无法表达“基础事实已覆盖、两个强限定未覆盖”的预期部分状态。
- 门禁：C4 五题回放失败；C4 Preflight-16 和 A～F 没有执行资格。
- 发布：生产替换和阶段 04 均不允许。

### 恢复合同

后续只有在用户另行授权新批次后才可实现。恢复至少必须证明：

1. 同目标的强限定问法能绑定已唯一证明的基础字段，而不会把 BEFORE/MUST 自动标为满足。
2. `EvidenceSelection` 和 required member 保留输入字段；限定仍以独立 Q 项保持未满足。
3. D 路径可以发布基础事实，coverage 对基础成员与限定分别计账；外部结果是安全限答或
   部分回答，而不是空拒答或虚假 `FULL`。
4. 来源真实明示前置义务的成对正例仍可满足限定，不能把所有“之前/必须”一律拒绝。
5. 不得依赖具体文档名、行名、case ID 或业务词白名单，不得提高 token 上限、恢复 repair、
   删除限定或放宽来源绑定。
6. 先通过对应离线 P05/P06、D01/D04 和等义变体，再用同一代码/镜像重跑五题；只有五题
   完整人工评审通过后才能进入同版本 Preflight-16 与 A～F。

## 停止与证据

- C4 真实业务请求：5，重试：0。
- C4 私有回放 SHA-256：
  `fc897e5bd93e4ef7f906ced8e210fbcb0ba7df4e0c0aea1652221107bcd4f9443`。
- C4 私有证据归档 SHA-256：
  `e53ca4938ce8addd6824437cac3b8a09de596d40f59b06d69459c750e20bccfb`。
- C4 version bundle：
  `6228fecd78962b91e2d7106b96928f38f202728fe0e97627f285a37f4644da1a`。
- C4 Preflight-16：未运行。
- A～F：未运行。
- 8289：已回滚 `wb08r03f-bd00c56`，live/ready 均 200。
- 生产：未修改，60:8288 与 54:18288 均 200。
- V14 候选镜像和服务器/本地临时项：已精确清理；唯一证据已先复制到本地安全目录。

## 本机分析材料

为避免只留下结论而无法复核，完整 C4 Trace 和测试时代码已保存到：

`C:\Users\jerry\Desktop\湾研院\湾事通\WB08R03_V14_C4_P0_分析证据_35b6e3c\`

建议依次查看：

1. `01_trace/derived/analysis-summary.json`：五题的来源解析、AnswerPlan、覆盖、发布和
   Provider 调用对比。
2. `01_trace/derived/cases/UNSAFE_TEMPORAL.full.json`：失败题完整 Trace。
3. 同目录的 `N031`、`EXPLICIT_SOURCE` 和 `EXPLICIT_COLUMN`：确认相同基础事实可以在
   非强限定问法下稳定发布。
4. `03_source_tested/.../plan_compiler.py`：测试提交的精确源码，而不是后续工作区版本。
5. `04_diff/v13-332dd46-to-c4-35b6e3c-question-answering.patch`：控制层替换的完整相关差异。
6. `README.md` 与 `tools/inspect_c4_p0.py`：字段解释、复核顺序和可重复生成摘要的方法。

该材料含内部正文和引用，未提交到远端；`SHA256SUMS.txt` 用于检查本机副本完整性。

## 冻结结论

V14 的控制层替换方向已经消除了 V13 的假部分和假想复核预算阻断，但 P05 暴露出基础
字段选择与限定证明仍未完全解耦。当前分支用于审计和后续恢复，不具备生产发布资格；在
新的明确授权前不得继续创建候选或自行修改该路径。
