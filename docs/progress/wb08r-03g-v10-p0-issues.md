# WB08R-03G V10：两轮真实候选失败后的 P0 清单

V10 状态为 **FAILED / PAUSED**，merge_allowed=false，independent_verification_ready=false。两份候选均已在专用 8289 服务完成真实 Preflight-16；第二轮仍未恢复目标问答，因此触发用户约定的“两轮即停”。本文件只记录已经观察到的事实、代码路径诊断和恢复条件，不把未保存的字段或模型意图补写成事实。

本轮落实的目标确实是“删除、降级或合并冗余运行时门禁，同时重新划分职责”，不是单纯放宽小门：

- 动作词差集、短问词面锚点、自由文本 covered_scope 包含关系已撤销硬拒绝权，降为诊断或复核信号。
- 表格行名和表头应由服务端根据认证坐标闭合，不再要求模型单独选中上下文。
- 多条自然语义路径合并到统一关系复核结果；复核失败原则上不应抹掉已确认事实。
- ACL、活动版本、来源身份、逐字引文、数字、否定、主体、阶段、表格坐标和真实跨度等硬边界继续保留。

## 两份候选和停止证据

| 项目 | 候选 C1 | 候选 C2 |
|---|---|---|
| 代码 | a7892fa6cb03ae46fa3c0c4e6f9b8721301e2ad3 | da854865f4dbb0c8b673b2d23087988d0a8e49cf |
| 镜像 | rag-test-wanshitong:wb08r03g-v10-c1-a7892fa | rag-test-wanshitong:wb08r03g-v10-c2-da85486 |
| image ID | sha256:5f8251fb803b969061fe0929bc9e47d024968f679a2fc0ae3f351b664c1f7d12 | sha256:7ff2c1ee55a8658842b18c3ff899360c9a4e8dfb38a40d415aa94591ae28f51c |
| review 协议 | wb08r-relation-review-v3 | wb08r-relation-review-v4 |
| Preflight-16 | FAILED；非空事实答案 0/16 | FAILED；非空答案 1/16，但该例仍需真值复核 |
| F015 三次 | 3/3 误拒 | 3/3 误拒 |
| repair | 0 | 0 |

两次 runner 都正常完成并产生 16/16 唯一 final；退出码 0 只表示执行完成，不代表门禁通过。A～F 均未运行。

## P0-01：物理表格行身份仍与语义目标混为一谈

**实际观察**

- C2 的 F015 主生成三次都只给 A1 选择表头 S18、给 A2 选择事实值 S13。
- A1 被 _validate_table_claim_certificate 以 CLAIM_TABLE_DEPENDENCY_INCOMPLETE 拒绝；A2 被 request_relation 以 CLAIM_QUERY_RELATION_UNDETERMINED 拒绝。
- 本轮新增的服务端表格闭合没有把 S1 行名、S19 列头补进最终 Claim，因此真实结果仍是 accepted=0、published=0。

**代码路径诊断**

_table_proof_units 和 _close_verified_table_supports 仍通过 citation_text == certificate.query_target 寻找物理行名，而 query_target 在来源资格判定中承担的是当前 Atom 的语义目标。两者不是同一合同：用户所问对象可以是“领取年份”“报销规则”等语义概念，物理行名则是表格某个坐标中的原文标签。C2 虽新增 table_fact_units 和多表头支持，却仍把两个身份复用成一个字段，所以真实值单元格无法确定性投影到行名和列头。

这是代码与真实“不闭合”现象共同支持的首要诊断；SAFE trace 没有同时保存原始 query_target 与物理行名明文，因此不能进一步声称某一对具体字符串已经被逐字证实不相等。

**恢复要求**

新增独立的物理事实身份，例如 physical_table_fact_id，至少绑定 document_version、table_locator、row、value_column、label_coordinate、header_coordinates 和 supporting_node_ids。语义 target 只用于判断该物理事实是否回答当前 Atom，不能再作为行定位键。服务器应在模型生成前形成可发布事实单元，模型只选择事实单元，不选择孤立单元格。

## P0-02：补充模型复核仍处于成功路径关键链，且协议缩短没有换来可用结果

**实际观察**

- C1 的 F015 v3 复核三次都得到响应，但三次均因返回结构无效而失败。
- C2 改为紧凑 anchor ID 的 v4 后，F015 三次准备包均包含 S1/S13/S19，估算输入 3320、预留输出 1024；三次都在 HTTP read timeout 结束，约 23.17 秒/次，未得到模型结论。
- C2 总 generation 调用 22 次；Preflight 中成功事实答案仅 1 条且仍需真值复核。复核没有形成稳定的可发布闭环。

**判断**

补充复核可以处理无法由结构证明的自然语义，但不能承担已认证表格事实的首要发布路径，也不能因超时或协议错误把确定性事实变成业务拒答。

**恢复要求**

认证表格事实走无模型的结构发布路径。关系复核只处理剩余自然文本歧义，采用独立、短超时、可降级的补充槽；超时只标记未决部分，不撤销同请求中已验证的独立事实。必须新增真实超时回放，证明降级不会抹除已接受事实。

## P0-03：repair 调度仍被 accepted 状态和跨 Atom 失败耦合

**实际观察**

- 两份候选各 16 次真实请求，repair_calls 均为 0。
- C2 F015 已明确出现 CLAIM_TABLE_DEPENDENCY_INCOMPLETE，但 accepted=0，最终 repair_skip_reason 为 NON_RECOVERABLE_OR_NO_CITABLE_SOURCE。
- F015 的 A1、A2 各有不同失败；任何一个都没有得到局部修复机会。

**代码路径诊断**

CLAIM_TABLE_DEPENDENCY_INCOMPLETE 仅在 has_accepted=true 时加入可修集合；accepted=0 时，raw_failures 又会以当前 atom_id 重新绑定整组失败，容易让一个 Atom 的非可修错误阻断另一个 Atom。relation review 一旦占用补充槽，也会禁止 repair。该控制流与“按 Atom 保留已确认事实、只修当前未闭合事实”的职责不一致。

**恢复要求**

把验证结果建模为逐 Atom 状态，repair 只读取本 Atom 的失败和许可来源；表格依赖不完整可先由确定性投影修复，不依赖其他 Atom 已经 accepted。一个 Atom 的硬失败不得污染另一 Atom；补充复核与结构修复不能共享一个互斥的全局槽。

## P0-04：N031 已不再是召回缺失，而是事实成组和预算路径失败

**实际观察**

- 三次 N031 均已把目标行相关来源送入主生成。
- 模型产生 8 条单来源 Claim，分别引用 S15、S16、S14、S13、S17、S20、S21、S22；全部在 CLAIM_QUERY_RELATION_UNDETERMINED 被拒。
- 关系复核在发送前以 RELATION_REVIEW_INPUT_BUDGET_EXCEEDED 跳过，三次最终均为 BUDGET_BLOCKED，无 repair。

**判断**

继续调召回或扩大候选数不能解释本轮失败。系统仍把一个结构事实拆成若干孤立单元格，再要求模型和通用关系门重建完整事实，导致输入膨胀和逐项误拒。

**恢复要求**

在 EvidencePack 阶段生成按 Atom 认证的 table fact units，并以事实单元而不是单元格进入发送预算、生成和发布校验。预算应以去重后的事实依赖图计算；同一行名/表头只发送一次。必须以 N031 三次固定回放证明目标事实单元实际进入生成和最终发布校验。

## P0-05：剩余硬边界失败不能在证据不足时删除

**实际观察**

- N033：CLAIM_STAGE_UNSUPPORTED，引用 S1/S2。
- F013：CLAIM_MODALITY_MISMATCH，引用 S3/S6。
- F024、F034：_validate_natural_support_structure 的 CLAIM_SOURCE_MISMATCH。
- N035 是唯一发布非空答案的案例，复核恢复路径为 CLAIM_QUERY_RELATION_UNDETERMINED → RELATION_REVIEW_VALIDATED，但门禁仍标 NEEDS_TRUTH_REVIEW。

**判断**

这些结果既不能证明现有硬门都正确，也不能证明它们都是冗余误拒。删除主体、阶段、模态或来源结构校验会越过当前证据。N035 只能记为行为改善，不能记为正确答案。

**恢复要求**

先补齐每例的原 Claim、Atom 语义、允许来源、来源坐标和真值裁决，再逐门决定保留、合并或修正。只有词面启发式应失去硬拒绝权；可由程序验证的安全边界继续保持。

## P0-06：离线测试与真实问答之间缺少关键回放

**实际观察**

- C2 问答相关范围 338 项通过，候选/评测相关范围 152 项通过；Ruff、mypy、py_compile、JSON 和 diff 检查通过。
- 同一代码在真实 8289 Preflight-16 仍失败，F015 三次误拒，N031 三次预算阻断。

**缺口**

现有合成测试证明了接口和局部规则，却没有覆盖“语义 target 不等于物理行名”“主生成只选值单元格”“补充复核超时”“accepted=0 时仍需按 Atom 结构闭合”这四个真实组合。

**恢复要求**

从本轮 SAFE 证据制作脱敏固定回放，至少覆盖：

1. 物理行身份与语义 target 分离；
2. 值单元格自动补齐行名和一个或多个表头；
3. 复核超时后保留已认证事实；
4. 多 Atom 中某一硬失败不阻断另一 Atom 的结构修复；
5. N031 事实单元经发送预算后仍完整。

只有这些回放先失败、修复后通过，才值得开启新的候选轮次。

## 冻结结论

V10 不进入第三候选，不进入独立 Verification，不运行 A～F，不发布到生产。C1/C2 代码保留并推送用于审计，但不代表可部署版本。8289 已回滚到 bd00c56 基线；生产 18288/60:8288 未修改。

后续若恢复，正确顺序是：先建立独立物理表格事实身份与逐 Atom 状态机，再让服务器投影完整事实，最后才对剩余自然语义启用可降级复核。继续增加词表、放宽硬边界、扩大 timeout 或增加第三层模型校验都不是本轮证据支持的方案。
