# 当前 P11 发布阻塞

唯一总体状态见 [当前验收](release/p11-repair-acceptance.md) 与同名 JSON；
完整事实、用量边界及历史记录见 [最终验收](docs/progress/p11-final-acceptance.md)。
`CODE_FIXES_READY=true`、`P11_READY=false`、`FEATURE_MERGE_DONE=false`、
`MERGE_TO_MAIN_AUTHORIZED=false`；未执行本轮 release/feature/main 合并。

代码 `887276aa` 已通过 1825 项完整检查与对应 7/7 CI；
实际 App 已更新至 `1a0fe6…be25` 候选并核对 healthy，Qdrant 未重启。
真实连接、双槽、主路、备用切换和恢复均 PASS。
部署和完整累计预算已获准并执行，不再列作待决阻塞；
`CODE_FIXES_READY` 只表示已完成工程修复的规定门禁通过；
正例证据准入和负例误准入问题仍未解决。

| blocker_id | 当前原因 | 解除条件 |
| --- | --- | --- |
| RETRIEVAL_QUALITY | 原 30 问 × 两路质量 FAIL：两路同为正例08证据不足、负例03/05/06误判可回答；citation validity及来源覆盖0.95、answerable accuracy26/30、refusal F1为0.777778；来源范围精度虽1.0，样本19仍小于原门20 | 解决已实测的证据/拒答缺陷，并按原标签、阈值及完整范围重新验证；当前已停止追加Provider和调参，不把暴露holdout后的回归结果称为独立泛化验证 |
| OS_RISK | 新候选完整扫描54个High/Critical包版本元组、18个CVE仍待有效处置；overlay已绑定新扫描且review_errors为空，风险仍RISK_REVIEW_REQUIRED | 实际责任人按源包/CVE审阅全部元组，提供有效处置依据或环境/责任/期限/人工批准附件；身份重绑不等于风险获准 |

最终累计 **226 HTTP / 64311 estimated / 64510 known observed**；
原 3 次 usage 未知，本地拦截 **193 次 / 3169 estimated** 单列。
已批准累计 cap 仍为 439 / 145861，Jina 316 / 132142、百炼 123 / 13719；
保留同一 campaign 和全部旧账，本轮 Provider 调用已经停止。

- [完整真实质量收据](artifacts/p11-final/pilot-fix/quality-live-result.json)
- [配置诊断及最新累计账](release/p11-blocker-diagnosis.json)
- [实际方案预算](release/p11-budget-plan.json)
- [新候选逐项风险](release/p11-os-risk-review.json)
- [Legacy 历史原文](docs/progress/legacy-blocked.md)
