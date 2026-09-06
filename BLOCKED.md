# 当前 P11 发布阻塞

唯一总体状态见 [当前验收](release/p11-repair-acceptance.md) 与同名 JSON；执行记录见
[最终验收](docs/progress/p11-final-acceptance.md)。
`P11_READY=false`，`FEATURE_MERGE_DONE=false`，`MERGE_TO_MAIN_AUTHORIZED=false`。

端点配置、实际草稿和 campaign 首绑已通过。App 使用 d98a8d 候选且健康，Qdrant 未重启。

| blocker_id | 当前证据与直接原因 | 解除条件 |
| --- | --- | --- |
| ALIYUN_RESPONSE_CONTRACT | 文档 canary HTTP 200，但 INVALID_RESPONSE_CONTRACT；查询依赖停止。额外一次已授权诊断采集点错误，未取得结构；已修正并通过零调用测试 | 获准补一次结构诊断后，依据真实字段与严格向量校验结果定位配置/代码问题；不猜测放宽断言，不清除失败 |
| FULL_LIVE_BUDGET | 原累计 25 HTTP / 1000 estimated、每 Provider 600 不变；当前累计 8 / 183。本地实际草稿已绑定，完整预算仍 PROPOSED | 真实响应问题定位后，由用户批准具体连续阶段或完整累计上限；所有诊断和重试继续记账 |
| OS_RISK | 当前候选完整扫描仍有 54 个 High/Critical 包版本元组、18 CVE 尚未形成有效处置；原客观补证保留 | 实际责任人按 CVE/源包分组审阅，原 overlay 覆盖全部元组；不代填风险批准人、期限或处置结论 |
| LIVE_ACCEPTANCE | 百炼文档操作失败；真实双槽、failover/recovery、原 30 问两路质量未执行 | 修复已证实的相关缺陷，预算与必要授权具备后按依赖继续；不能用离线/CI 替代真实验收 |

- [配置诊断](release/p11-blocker-diagnosis.json)
- [实际方案预算](release/p11-budget-plan.json)
- [候选镜像逐项风险](release/p11-os-risk-review.json)
- [Legacy 历史原文](docs/progress/legacy-blocked.md)
