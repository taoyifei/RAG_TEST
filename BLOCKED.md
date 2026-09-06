# 当前 P11 发布阻塞

本页是摘要，唯一总体状态见 [当前验收](release/p11-repair-acceptance.md) 与同名 JSON。
本轮工程变更、实际测试和交付记录见 [最终验收](docs/progress/p11-final-acceptance.md)。
`P11_READY=false`，`MERGE_TO_MAIN_AUTHORIZED=false`；局部代码门通过不代表真实发布就绪。

| blocker_id | 当前证据与直接原因 | 根因已验证 | 解除责任与下一步 | 外部请求 |
| --- | --- | --- | --- | --- |
| RUNTIME_TARGET | 原 App 仍为 20864e7e 镜像；已验收候选为 d98a8d16，未获目标更新授权。原镜像已不在本地镜像库，需先保留原实例恢复材料 | 是；原库 1—17 migration checksum 已核对，无待执行产品 SQL migration | 用户明确允许 app-only 更新；先备份与保存恢复路径，只短暂停目标 App，不动 Qdrant/卷。更新后核查测试注入 | 否 |
| ALIYUN_CONFIGURATION | 只读非秘密配置显示 endpoint_mode 缺失；API Host 未保存，Workspace 形状与北京地域有效 | 是，配置级；未验证供应商可用性 | 用户编辑原百炼连接，选择模式；业务空间模式从北京控制台 API Key 弹窗或业务空间管理 API Host 列复制主机。保留原 Credential，保存但不测试 | 否 |
| ACTUAL_PROFILE | 原库已保存/草稿方案数量为 0；当前配置没有 source_profile_revision_id。预算 CLI 接线已补齐，但实际计划 actual_profile_bound=false | 是，原数据卷零调用核对 | 原连接配置完成后保存合法 Jina 主向量/Qwen 备用/Jina v3.5 重排草稿，保留实际 instruct 与检索策略；不激活、不提前建索引。执行者重算实际绑定预算 | 否 |
| CAMPAIGN_BINDING | 产品数据卷尚无持久 ledger，旧账已核对但未导入 | 是 | 获准更新运行实例后，用既有维护入口仅停目标 App、断网首绑全量旧账、finally 恢复；不动 Qdrant | 否 |
| FULL_LIVE_BUDGET | 最新旧账仍 6 HTTP / 157 estimated，原授权剩余 19 / 843；默认计划 434 / 145703 为未批准假设，不能直接批准 | 是 | 实际草稿绑定预算生成后，用户批准明确累计上限或较小连续阶段；已有 Session + CSRF 修订入口保持原 campaign 与全部旧账 | 批准本身否；执行 Live 是 |
| OS_RISK | 当前同摘要候选完整扫描中 54 个 High/Critical 包版本元组、18 CVE 仍未形成有效处置；本次补证 Perl 位数/模块及 homed 缺失，官方 Trixie 条目仍 open | 部分，包/公告/组件条件已核对，未声称全部不可利用 | 指定实际责任人按 CVE/源包分组审阅，最终原 overlay 覆盖每个元组；不要求直接接受全部风险，不代填批准人/期限 | 官方公告查询有；无 Provider HTTP |
| LIVE_ACCEPTANCE | 真实连接、双槽、故障恢复与 30 问质量尚未满足所需新证据 | 是，前置条件受阻 | 配置、绑定和明确授权满足后复用有效步骤，按原实验继续 | 是，必须另有有效授权 |

- [零调用诊断](release/p11-blocker-diagnosis.json)
- [完整待批准预算](release/p11-budget-plan.json)
- [候选镜像逐项风险](release/p11-os-risk-review.json)
- [Legacy 历史原文](docs/progress/legacy-blocked.md)：旧 Industry 地址、shadow 路由和早期服务器验收不适用于当前 P11。
