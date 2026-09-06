# 当前 P11 发布阻塞

唯一总体状态见 [当前验收](release/p11-repair-acceptance.md) 与同名 JSON；
实测与历史记录见 [最终验收](docs/progress/p11-final-acceptance.md)。
`CODE_FIXES_READY=true`、`P11_READY=false`、
`FEATURE_MERGE_DONE=false`、`MERGE_TO_MAIN_AUTHORIZED=false`。

百炼 API 已证明返回有效 1024 维向量，响应格式兼容缺陷已修复并通过 1774 项完整检查和 7/7 CI。
端点、实际草稿、campaign 首绑通过；当前 App 仍为 d98a8d，修复候选为 2a726c，原 Qdrant 未重启。

| blocker_id | 当前原因 | 解除条件 |
| --- | --- | --- |
| FIXED_CANDIDATE_NOT_DEPLOYED | 修复提交 b8b2f37 已构建并通过隔离功能门，运行实例尚未使用该镜像 | 按任务书 5.1 明确允许更新至 2a726c；备份后 app-only 更新，核对无测试注入 |
| POST_FIX_CANARY | 原 canary 因代码缺陷失败；原步骤及两次诊断单次额度已用完，修复后未调用 | 明确批准文档单次复测，成功才 query 单次；同一 campaign 追加记账，保留旧失败 |
| FULL_LIVE_BUDGET | 当前累计 9 HTTP / 196 estimated；原上限 25 / 1000、每 Provider 600。实际绑定完整计划仍 PROPOSED | 批准具体连续阶段或完整累计预算；实际草稿身份变化即重新核对，所有重试计账 |
| OS_RISK | 新镜像完整未过滤扫描 54 个 High/Critical 元组、18 CVE 尚未有效处置 | 实际责任人审阅覆盖全部元组的原 overlay；不代填批准人、期限或结论 |
| LIVE_ACCEPTANCE | 真实双槽、主路、failover/recovery、原 30 问两路质量未执行 | 新目标、必要授权和预算具备后按依赖继续；离线/CI 不代替真实验收 |

- [配置诊断及最新累计账](release/p11-blocker-diagnosis.json)
- [实际方案预算](release/p11-budget-plan.json)
- [新候选逐项风险](release/p11-os-risk-review.json)
- [Legacy 历史原文](docs/progress/legacy-blocked.md)
