# 当前 P11 发布阻塞

权威状态：[当前验收](release/p11-repair-acceptance.md) / [JSON](release/p11-repair-acceptance.json)；本轮完整记录：[质量与安全收尾](docs/progress/p11-quality-security-closure.md)。

`CODE_FIXES_READY=true`，`RETRIEVAL_QUALITY_READY=BLOCKED`，`SECURITY_READY=BLOCKED`，`P11_READY=false`，`FEATURE_MERGE_DONE=false`，`MERGE_TO_MAIN_AUTHORIZED=false`。

工程代码d1f8d8d完成2198项完整check及精确SHA的7/7 CI；实际App为05d6f9…866f。原四题两路结果已纠正，但唯一完整60观测Live遇到18次Jina传输错误，备用正例16–20拒答、引用有效率0.75，不能放行。已停止追加Provider，不更改原指标或重跑择优。

| blocker_id | 当前事实 | 解除条件 |
| --- | --- | --- |
| RETRIEVAL_QUALITY | 原程序MISSING_CASE_BOUND_LIVE_ATTEMPTS_OR_ROUTE，primary计划路2条自然切备用，16次rerank bypass；备用5条新退化，准确率25/30、拒答F1=0.8 | 保留本轮失败；未来若另行决定复验，需真实完整原范围通过和有效路由证据，本轮不自动追加 |
| OS_RISK | 完整扫描HC54→50、18CVE，移除mount的4条元组；客观豁免0、人工批准0，所有54条已有完整对账 | 实际责任人通过既有风险入口决定限定环境及期限的剩余风险；已备好18CVE摘要、入口/加载/发行版证据，AI未代签 |

同campaign累计354 HTTP / 104986 estimated，理论剩余85 / 40875；known observed94902另列，21次usage未知，其中本轮18次TRANSPORT_ERROR保守计费占额。累计本地拦截325次/24342 estimated不混入HTTP。配置、首绑、旧预算和同目标维护已获准且完成，不重新列为授权阻塞。

- [唯一原60观测Live](artifacts/p11-final/quality-security-closure/live-result.json)
- [逐题前后差异](artifacts/p11-final/quality-security-closure/per-case-before-after.json)
- [18CVE及54→50元组处置](artifacts/p11-final/quality-security-closure/security/cve-summary.md)
- [既有风险入口](release/p11-os-risk-review.json)
- [原轮历史记录](docs/progress/p11-final-acceptance.md)

修复分支已推送，工作线正常合并 `029d674c19b3d61f31ad0da84228d54af0535ed1` 已推送；[实际合并CI](https://github.com/taoyifei/RAG_TEST/actions/runs/34031731094) 7/7 success。业务资产与已测候选一致，release/feature/main/Industry未推进。
