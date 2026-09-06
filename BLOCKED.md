# 当前 P11 发布阻塞

唯一总体状态见 [当前验收](release/p11-repair-acceptance.md) 与同名 JSON；
实测和历史记录见 [最终验收](docs/progress/p11-final-acceptance.md)。
`CODE_FIXES_READY=true`、`P11_READY=false`、
`FEATURE_MERGE_DONE=false`、`MERGE_TO_MAIN_AUTHORIZED=false`。

运行 App 已更新至修复候选 2a726c，healthy；Qdrant 未重启，备份已验证。
百炼 document/query 两项真实 canary 均 PASS（HTTP 200、1024 维），原失败保留。
用户密钥和端点无需再修改。本次授权的两个请求已经完成。

| blocker_id | 当前原因 | 解除条件 |
| --- | --- | --- |
| FULL_LIVE_BUDGET | 当前累计 11 HTTP / 315 estimated；原上限 25 / 1000、每 Provider 600。实际方案完整计划仍 PROPOSED | 批准具体下一连续阶段或完整累计预算；保留历史，复用同身份有效验证，不自动重跑 canary |
| OS_RISK | 同一实际镜像完整扫描 54 个 High/Critical 元组、18 CVE 尚未有效处置 | 实际责任人审阅覆盖全部元组的原 overlay；不代填批准人、期限或结论 |
| LIVE_ACCEPTANCE | Jina 本轮全部操作未完成核验，真实双槽、主路、failover/recovery、原 30 问两路质量未执行 | 必要授权及预算具备后按依赖继续，先核对可复用 Jina 证据；离线/CI 不代替真实验收 |

- [配置诊断及最新累计账](release/p11-blocker-diagnosis.json)
- [实际方案预算](release/p11-budget-plan.json)
- [当前镜像逐项风险](release/p11-os-risk-review.json)
- [Legacy 历史原文](docs/progress/legacy-blocked.md)
