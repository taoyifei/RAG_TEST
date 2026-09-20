# WB08R-03G V13：三轮真实候选失败后的 P0 清单

V13 状态为 **FAILED / PAUSED**，`merge_allowed=false`，
`independent_verification_ready=false`。F024 的错误来源和 N031 的无依据时序已在第三轮
固定回放中修通，但通用显式来源正例仍被错误降为部分回答，两个关系变体仍在生成前预算
阻断。第三轮达到用户规定上限，本清单只记录已观察事实、代码判断和恢复门槛，不提出
第四轮补丁。

## 三轮停止证据

| 项目 | C1 | C2 | C3 |
|---|---|---|---|
| 代码 | `b064b77` | `aba7892` | `69057ba` |
| F024 ×3 | 安全拒答 | 安全拒答 | 安全拒答 |
| N031 ×3 | `PARTIAL` | `PARTIAL` | `SUPPORTED` |
| 显式来源正例 | `BUDGET_BLOCKED` | `PARTIAL` | `PARTIAL` |
| 显式列名正例 | 未达到门禁 | `BUDGET_BLOCKED` | `BUDGET_BLOCKED` |
| 危险时序反例 | 未达到门禁 | `BUDGET_BLOCKED` | `BUDGET_BLOCKED` |
| 是否允许继续修复 | 是 | 是 | 否，达到三轮上限 |

## P0-01：显式来源解析与 QueryAtom 语义仍是两套合同

**实际观察**

- C3 的显式来源正例已经唯一解析到正确文档，SourceScope 为 `RESOLVED`，最终 3 条引用
  全部来自该文档。
- QueryAnalyzer 却将整句识别为 `DEFINITION`，关系为“定义”；本地同函数直接诊断与
  8289 trace 一致。
- 同一问题的来源身份由后续 SourceScope 保存，但目标、表格列和回答类型没有同步变成
  “需求快验 / 输入 / ENUMERATION”。
- C3 新增的 `query_without_source_qualifier()` 只消费 Atom 上的文本来源字段；真实来源
  已解析到 SourceScope 并不保证该字段携带同一显示名，因此来源标题仍可能参与表格轴词面
  匹配。

**根因判断**

来源解析、问句语义和 QueryAtom 构造先后独立，缺少一个权威的“去掉已解析来源后的业务
问句”。尾部“是什么”先命中定义规则，遮蔽了“输入项”关系；随后表格选择器又可能把来源
标题中的列名词当成用户请求的列。

**恢复要求**

1. SourceScope 解析成功后生成只读 `question_body_without_source`，由 QueryAnalyzer、
   QueryAtom、表格关系绑定和目标完整性共同消费；不能各自重做标题字符串猜测。
2. “某文档中，某行的某列是什么/有哪些”必须稳定形成一个 Atom：目标、列关系、枚举类型
   和 required document identity 都是强类型字段。
3. 定义句法不得覆盖已经识别的表格列意图；加入带书名号、无书名号、标题含列名、标题不含
   列名的通用成对测试。
4. 不得用当前文档名、F024/N031 case ID 或“工作模式”词表特判。

## P0-02：目标成员完整性把未请求列加入必答集合

**实际观察**

- C3 显式来源正例的 Claim 已由 `physical_table_fact` 渲染，`relation_gap=false`，来源
  完整且引用包含行标签、输入列表头和输入值。
- `target_member_coverage` 仍产生 3 个 `required_member_keys`，只覆盖输入值对应的 1 个，
  另 2 个位于同一物理行的非请求列，最终 reason 为
  `TARGET_MEMBERS_NOT_ANSWERED`。
- 因此回答内容安全、来源正确、所问值已给出，终态仍为 `PARTIAL` 并增加不完整提示。
- N031 的短目标“快验”在 C3 只要求真正输入值，故为 `SUPPORTED`；显式来源正例使用完整
  目标“需求快验”时却扩张到额外列，两个等价问法得到不同完整性结果。

**根因判断**

目标成员集合仍可被问句词面和来源标题扩张，未完全复用已经证明并选中的
`AtomFactBinding/PhysicalTableFact`。发布层已知道唯一事实，完整性层却重新扫描表格并形成
不同的必答集合。

**恢复要求**

1. 对闭合物理表格事实，完整性 required set 直接来自已绑定 fact 的值成员；行标签和表头
   是关系上下文，不应作为额外事实要求，也不得扩张到未绑定列。
2. `target_member_coverage` 必须验证 required set 与 `selected_assertion_ids`、
   `AtomFactBinding.fact_id` 一致；不一致时记录内部合同错误，不向用户伪装成资料不完整。
3. 完整目标、唯一短目标、显式来源和省略来源四种等价问法必须得到相同 required set 和
   `SUPPORTED` 结果。

## P0-03：显式列名和危险时序被错误拆成两个 Atom并在复核前超预算

**实际观察**

- 显式列名正例被分析为 `FACT/ORIGINAL_FALLBACK`，只保留 `QUOTED_TEXT` 约束；
  自适应 Planner 实际调用后生成 2 个 Atom。
- 危险时序反例同样为 `FACT/ORIGINAL_FALLBACK`，只保留两个 `QUALIFIER` 约束，也被拆成
  2 个 Atom。
- 两题都在任何回答生成前命中 `SEMANTIC_REVIEW_PREFLIGHT_BUDGET_EXCEEDED`，
  `generation_called=false`，最终两个 Atom 均为 `MISSING`。
- C2、C3 的结果一致，证明这不是第三轮模型随机性。

**根因判断**

规则语义没有识别“行目标 + 引用列名 + 包含什么”和“行目标 + 时序/义务修饰 + 材料”这两类
结构，导致 Planner 把一个表格关系问题重复拆分。每个 Atom 又携带结构闭合与语义复核
预算，预检成本超过上限。

**恢复要求**

1. 显式列名正例必须形成一个表格 Atom；危险时序问法可以形成“基础输入事实 + 不受支持的
   时序限定”结构，但不得把同一事实复制成两个完整证据 Atom。
2. 预检预算按去重后的物理事实和 Atom 关系计算；同一表格事实不能因 Planner 拆分重复承担
   完整外壳。
3. 预算修复后，危险时序只能安全降级为来源支持的“输入”或拒绝该限定，绝不能发布
   “之前/必须”。
4. 不得仅提高 `max_input_tokens`、删除语义复核或重新启用 repair 来制造通过。

## P0-04：三轮上限已到，后续门禁没有执行资格

**实际观察**

- 三轮共执行 18 条 F024/N031 固定回放和 10 条直接相关变体；C3 在通用正例和预算变体
  上失败。
- New Wire P0～P3、Preflight-16 与 A～F 均未运行；没有用局部 P0 修通包装成完整问答
  通过。
- 8289 已回滚基线，生产身份未变，三轮候选镜像和临时文件已清理。

**恢复要求**

新的工作必须作为新的架构修复批次启动，而不是 V13 第四轮微调。开始前至少提供：

1. 统一的 `resolved source -> question body -> typed atom` 合同；
2. 物理事实绑定与 target required set 的单一权威映射；
3. 一个 Atom 的显式列名/危险时序解析与预算证明；
4. F024、N031、显式来源、显式列名、危险时序五组固定正反例；
5. 新的候选次数和失败停止条件。

满足这些条件后，仍必须从最小正反例开始，依次通过 New Wire P0～P3、Preflight-16、
EvidencePack-12、CoreAnswer-16、Failed-24、Natural-36、Full-96 和
Concurrency-4。任一前置 Gate 失败即停止后续。

## 冻结结论

V13 已证明确定性文档范围、服务端物理表格渲染和口语输入关系规范化能消除 F024、N031
两个严重错误；剩余阻塞不是继续堆提示词，而是来源解析、Atom 语义、事实绑定和完整性
验收仍未共享同一个权威结构。在该结构闭合前，不再创建新的 8289 候选。
