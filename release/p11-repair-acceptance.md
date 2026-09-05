# P11-R4 验收

P11_READY=False

| Gate | 状态 | 原因 / 待补证据 |
| --- | --- | --- |
| AUTHORIZATION_BOUNDARY_READY | BLOCKED | campaign_config |
| ALIYUN_ENDPOINT_CONTRACT_READY | BLOCKED | campaign_config |
| CONNECTION_EDIT_READY | PASS | ALL_REQUIRED_PASSED |
| RESOLVED_POLICY_CONFORMANCE_READY | PASS | ALL_REQUIRED_PASSED |
| PROFILE_INDEX_SWITCH_READY | PASS | ALL_REQUIRED_PASSED |
| PROVIDER_CONNECTIVITY_READY | NOT_RUN | aliyun_document_canary, aliyun_query_canary, jina_connection |
| DUAL_SLOT_FUNCTION_READY | NOT_RUN | dual_index, primary_query |
| FAILOVER_RECOVERY_READY | NOT_RUN | standby_failover, recovery |
| RETRIEVAL_QUALITY_READY | BLOCKED | citation_quality |
| PRODUCT_BROWSER_READY | PASS | ALL_REQUIRED_PASSED |
| BACKUP_RESTORE_READY | PASS | ALL_REQUIRED_PASSED |
| SECURITY_READY | BLOCKED | os_risk |
| CI_READY | PASS | ALL_REQUIRED_PASSED |
| REMOTE_PRODUCTION_PROFILE_READY | BLOCKED | ALIYUN_ENDPOINT_CONTRACT_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY |
| RELEASE_CANDIDATE_READY | BLOCKED | AUTHORIZATION_BOUNDARY_READY, ALIYUN_ENDPOINT_CONTRACT_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY, SECURITY_READY, REMOTE_PRODUCTION_PROFILE_READY |
| P11_READY | BLOCKED | RELEASE_CANDIDATE_READY |

预算与用量：

```json
{
  "source": "read_only_running_product_database",
  "campaign_bound": false,
  "campaign_status": "BLOCKED_CONFIGURATION_CONFIRMATION_REQUIRED",
  "request_limit": 25,
  "estimated_token_limit": 1000,
  "provider_token_limits": {
    "jina": 600,
    "aliyun": 600
  },
  "prior": {
    "forwarded_http": 6,
    "estimated_input_tokens": 157,
    "observed_tokens_known_sum": 242,
    "observed_usage_unknown_attempts": 3,
    "locally_blocked": 1,
    "locally_blocked_estimated_tokens": 19
  },
  "this_run": {
    "forwarded_http": 0,
    "estimated_input_tokens": 0,
    "observed_tokens": null,
    "observed_usage_status": "NO_PROVIDER_REQUEST",
    "private_documents_sent": false
  },
  "cumulative": {
    "forwarded_http": 6,
    "estimated_input_tokens": 157,
    "observed_tokens_known_sum": 242,
    "observed_usage_unknown_attempts": 3,
    "locally_blocked": 1,
    "locally_blocked_estimated_tokens": 19
  },
  "remaining": {
    "requests": 19,
    "estimated_input_tokens": 843
  },
  "quality_query_only_lower_bound": {
    "requests": 60,
    "estimated_input_tokens": 3590
  },
  "quality_minimum_additional_lower_bound": {
    "requests": 41,
    "estimated_input_tokens": 2747
  },
  "note": "estimated 与 observed 分列；历史缺 usage 保留 unknown；新增预算未经批准。"
}
```

限制：

- 代码和报告保存不需要用户手工点击；此前收尾延误由执行安排和验收入口问题造成，非等待 CI 或用户审批。
- 真实 Live 未执行：页面尚需确认百炼 endpoint_mode、对应可信 API Host 和北京地域；当前 config_check 为 CONNECTION_OR_PROFILE_INVALID。
- 持久账本代码已实现并验证，但当前实例 campaign 尚未首绑；页面保存连接后由续跑命令在停止 app 的维护窗口导入全部旧账并绑定，不能把旧账清零。
- 主/备真实模型、故障恢复和质量验收未执行；历史 Jina 200 仅保留为历史记录，相关请求策略已变，不能代替本候选证据。
- 质量 pilot 为 30 个问题、主/备各一轮；仅查询下限为 60 请求 / 3590 估算 Token，现余 19 / 843；至少差 41 / 2747，且尚未计文档、重排和重试。没有扩大授权预算。
- SECURITY_READY=BLOCKED：最终镜像全部 High/Critical 54、可修复 0、无修复版本 54；逐项可达性和缓解尚未评估，无风险接受人或期限，不能用于生产放行。
- 最终镜像浏览器为 5 passed / 3 skipped；3 项为既有平台范围，详细命令及范围保留。Mock/离线证据不证明真实模型质量。
- CI 7 个 job 在代码合并 SHA 成功；证据/Markdown 提交复用相同业务资产，不重建镜像、不重做备份、不重复模型调用。
- P11_READY=false，禁止合入 feature/universal-rag；MERGE_TO_MAIN_AUTHORIZED=false，main/Industry 保持原引用。

详细证据来源、命令退出码、资产身份见同名 JSON。
MERGE_TO_MAIN_AUTHORIZED=false。

交付延误与用户操作说明：

代码和报告提交无需用户再点击。Codex 未及时冻结改动和集中验收，重复执行回归；
release 模块搜索路径、浏览器端口/Origin、Docker 缓存权限问题导致额外返工。
问题已修复。代码合并 SHA `8e58720c59dc3187361fce32fc26fbf6572dc641` 的 CI
在 2026-09-05 23:36（香港时间）已全部成功，之后没有及时交付属于 Codex 收尾问题，
不能归因于等待用户或 CI。

真实 Live 才需要用户在页面确认百炼 Endpoint 模式、对应可信 Host 和北京地域。
随后运行已有 release acceptance 的续跑模式，首绑导入旧账，原预算不变。
详细两个步骤及可直接执行的命令见
[phase-11.md 的本次追加记录](../docs/progress/phase-11.md#用户后续只有两个操作步骤)。
质量预算缺口和 OS 风险评估是另外的发布阻断，页面点击不会自动解除。

实际验证：完整离线 1629 passed / 88 deselected；smoke 72；product-check 72；
product-smoke 6；升级 7；Qdrant 3；前端单元 27；最终镜像浏览器 5 passed / 3 skipped。
[代码合并 SHA 的 P11 CI](https://github.com/taoyifei/RAG_TEST/actions/runs/33975025319) 7/7 成功。
代码已推送 P11，未合入 feature/universal-rag、main 或 Industry。

资产：候选镜像 `sha256:20864e7e232c03af74e4ef9f7ea48569d40fc2a2821429b423167e99e6c691e1`，
本地实例健康，2 个连接、2 个 Credential、7 条历史验证保留；本次新增真实 HTTP 0、
累计 6/25；估算 Token 157/1000；已知 observed usage 242、另外 3 次 unknown；私文未出网。

[单份证据包](p11-r4-evidence.json)保存原始命令、退出码、日志、CI、扫描清单和资产身份。
