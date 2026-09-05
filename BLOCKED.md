# 当前 P11 发布阻塞

本页是摘要，唯一总体状态见 [当前验收](release/p11-repair-acceptance.md) 与同名 JSON。
本轮工程变更、实际测试和交付记录见 [P11-R5](docs/progress/p11-r5.md)。
`P11_READY=false`，`MERGE_TO_MAIN_AUTHORIZED=false`；局部代码门通过不代表真实发布就绪。

| blocker_id | 当前证据与直接原因 | 根因已验证 | 解除责任与下一步 | 外部请求 |
| --- | --- | --- | --- | --- |
| ALIYUN_CONFIGURATION | 只读非秘密配置显示 endpoint_mode 缺失；API Host 未保存，Workspace 形状与北京地域有效 | 是，配置级；未验证供应商可用性 | 用户编辑原百炼连接，选择模式；业务空间模式从北京控制台 API Key 弹窗或业务空间管理 API Host 列复制主机。保留原 Credential，保存但不测试 | 否 |
| CAMPAIGN_BINDING | 产品数据卷尚无持久 ledger，旧账已核对但未导入 | 是 | 获准更新运行实例后，用既有维护入口仅停目标 App、断网首绑全量旧账、finally 恢复；不动 Qdrant | 否 |
| FULL_LIVE_BUDGET | 原预算剩余 19 HTTP / 843 estimated；仅 30 问两路 Query 下限就需 60 / 3590 | 是 | 用户审阅 PROPOSED 完整预算；实际管理员通过已有 Session + CSRF 明确追加累计授权，随后按获准范围续跑 | 批准本身否；执行 Live 是 |
| OS_RISK | 完整候选扫描与逐项审查见风险报告；未知可达性和无有效批准保持 BLOCKED | 部分，包/公告已核对，未声称不可利用 | 责任人补充可核验证据，或在明确范围和期限内作真实人工处置；AI 不签署接受 | 官方公告查询有；无 Provider HTTP |
| LIVE_ACCEPTANCE | 真实连接、双槽、故障恢复与 30 问质量尚未满足所需新证据 | 是，前置条件受阻 | 配置、绑定和明确授权满足后复用有效步骤，按原实验继续 | 是，必须另有有效授权 |

- [零调用诊断](release/p11-blocker-diagnosis.json)
- [完整待批准预算](release/p11-budget-plan.json)
- [候选镜像逐项风险](release/p11-os-risk-review.json)
- [Legacy 历史原文](docs/progress/legacy-blocked.md)：旧 Industry 地址、shadow 路由和早期服务器验收不适用于当前 P11。
