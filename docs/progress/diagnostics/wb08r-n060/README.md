# WB08R-N-060 单题诊断

结论：`NOT_OBSERVED`。本次只完成历史证据链定位，没有足够证据证明语义复核误杀、Claim 自身错误，或当前版本已经修复，因此不实施算法改动。

## 1. 身份与边界

| 项目 | 值 |
|---|---|
| 当前诊断基线 | `a045922f5ea1e2bdab2a0c78e04494f01b054224` |
| 历史代码版本 | `6187be6f0bc9d822101ab5268b0b7eebd1905970` |
| 历史 run | `natural_complex18/WB08R-N-060` |
| 代表 Trace | `trace_615ef69a7ec40a30cc62f928c2480505` |
| 对照 Trace | `trace_d2e891a2857d7a658c1590c900581a8f`、`trace_3499cae8760e035264aa8d326393f785` |
| 历史包 SHA-256 | `A325BA5E0DA303EFF825619939A2134782AD72AB40314154BCF3471ECB92FDDC` |
| 模型/IdP 调用 | `0` |

本诊断只读历史包、当前代码和仓库内既有 truth 数据。未访问 SSO、Auth、Cookie、回调、用户主体或反馈持久化链路；未启动服务、未部署、未重建索引。

## 2. 已观测事实

1. 仓库内已冻结 truth 将本题标为复合 `DURATION` 问，要求同一表格行中的 `F1` 和 `F2`：责任主体与确认动作位于 `tc:0`、`tc:1`，反馈时限位于 `tc:3`。对应公开登记见 `evaluation/wanshitong/wb08r03f-truth-v1/cases.ndjson` 与 `evidence_truth.ndjson`。
2. 三次历史 Trace 的结果一致：Planner 产出两个 Atom，但安全审计只留下 `A1:COUNT`、`A2:COUNT`；两条 Claim 分别被判为 `SEMANTIC_CONTRADICTED` 和 `SEMANTIC_UNKNOWN`，最终生成 2、接受 0、发布 0。
3. Gold 片段确实进入候选链：时限 chunk 在 Root lexical/dense 中均为第 1，责任主体/动作 chunk 在 Root lexical/dense 中分别为第 6/10；rerank 后二者仍保留为第 1/8。三个 Gold support 也都出现在 `retrieval.generation_evidence.admitted_sources`，并同时关联 `A1`、`A2`。
4. “进入候选包”不等于“生成器可读”。首个 Prepared Packet 的 `support_sources` 注册表包含 Gold 行，但 `per_atom_support_ids` 实际只允许 13 个其它 support；`S1`、`S15`、`S16`、`S17` 均不在任一 Atom 的可读集合。第二个 Packet 只允许 `A1 -> S28`、`A2 -> S2`，同样不含 Gold 行。三次历史 Trace 均如此。
5. Trace 只保留两条 Claim 的哈希、投影摘要和复核终态，没有保存 Claim 原文、各 Claim 的 refs、复核请求正文或模型解释。`artifacts` 为空，历史包内的 `private-replay.ndjson` 也没有 N060 记录。

## 3. 当前代码核对

- 从历史版本 `6187be6` 到基线 `a045922`，表格 literal fragment 的可读过滤与 Gold support 投影逻辑没有变更。
- 后续 `521f3c5` 只把 structured output Schema 收窄到本次真实 Atom，并不能证明 N060 的 Atom 语义或来源许可已经修复。
- 当前私有回放合同能够保存原始请求、草稿、refs 和 Prepared Packet，但现有 N060 历史运行没有启用这份捕获；仓库内的固定私有回放脚本也不包含 N060。
- 因缺少历史 Planner 原始 Atom 的 `target`、`relation`、`source_qualifier`，不能离线构造与旧请求等价的当前版本输入。重新调用模型才可能产生当前结果，但不在本批授权范围。

## 4. 判定与停止理由

现有证据能证明：Gold 来源到达了检索和 admitted registry，却没有进入生成器的 Atom 可读集合；同时 Planner 把两个 Atom 都记录成 `COUNT`。但无法仅凭这些 SAFE 摘要区分以下根因：

- Planner 的 Atom 目标、关系或 answer shape 错误，导致表格事实不能认证；
- 来源绑定/物理表格事实投影存在窄缺陷；
- 生成 Claim 自身答偏或引用不足，语义复核正确拒绝；
- 复核输入缺失了同源主体、动作、条件或时限，造成误拒绝。

因此按等待期计划归类为 `NOT_OBSERVED`，D1 状态为 `DIAGNOSED_ONLY`。本批不删除语义复核、不把 `unknown` 当 `supported`、不恢复 repair、不调整全局阈值，也不增加硬编码回归答案。

若进入后续 Q1，最小新增证据应是一次隔离的 N060 当前版本私有捕获，至少包含：完整 QueryPlan Atom、实际发送的 read units、两条 Claim 与 refs、Relation Review 请求/响应。取得这些证据前，不建立修复任务。
