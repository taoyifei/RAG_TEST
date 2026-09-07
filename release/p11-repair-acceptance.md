# P11 当前验收

P11_READY=False
CODE_FIXES_READY=True

| Gate | 状态 | 原因 / 待补证据 |
| --- | --- | --- |
| AUTHORIZATION_BOUNDARY_READY | PASS | ALL_REQUIRED_PASSED |
| ALIYUN_ENDPOINT_CONTRACT_READY | BLOCKED | endpoint_contract |
| CONNECTION_CONFIGURATION_READY | BLOCKED | connection_configuration |
| CAMPAIGN_BINDING_READY | BLOCKED | campaign_binding |
| CONNECTION_EDIT_READY | PASS | ALL_REQUIRED_PASSED |
| RESOLVED_POLICY_CONFORMANCE_READY | PASS | ALL_REQUIRED_PASSED |
| PROFILE_INDEX_SWITCH_READY | PASS | ALL_REQUIRED_PASSED |
| PROVIDER_CONNECTIVITY_READY | BLOCKED | aliyun_document_canary, aliyun_query_canary, jina_connection |
| DUAL_SLOT_FUNCTION_READY | BLOCKED | dual_index, primary_query |
| FAILOVER_RECOVERY_READY | BLOCKED | standby_failover, recovery |
| RETRIEVAL_QUALITY_READY | BLOCKED | citation_quality |
| PRODUCT_BROWSER_READY | BLOCKED | candidate_browser |
| BACKUP_RESTORE_READY | PASS | ALL_REQUIRED_PASSED |
| SECURITY_READY | BLOCKED | os_risk |
| CI_READY | PASS | ALL_REQUIRED_PASSED |
| REMOTE_PRODUCTION_PROFILE_READY | BLOCKED | ALIYUN_ENDPOINT_CONTRACT_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY |
| RELEASE_CANDIDATE_READY | BLOCKED | ALIYUN_ENDPOINT_CONTRACT_READY, CONNECTION_CONFIGURATION_READY, CAMPAIGN_BINDING_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY, PRODUCT_BROWSER_READY, SECURITY_READY, REMOTE_PRODUCTION_PROFILE_READY |
| P11_READY | BLOCKED | RELEASE_CANDIDATE_READY |

预算与用量：

```json
{
  "total": 721,
  "reserved": 378,
  "forwarded": 378,
  "locally_blocked": 343,
  "estimated_input_tokens": 113028,
  "observed_tokens": 102846,
  "observed_usage_status": "unknown",
  "unknown_usage_attempts": 21,
  "unknown_forwarding_attempts": 18,
  "locally_blocked_estimated_tokens": 24582,
  "campaign_id": "p11-20260905-public-synthetic",
  "authorization_id": "p11-20260904-existing-25-1000",
  "request_limit": 439,
  "estimated_token_limit": 145861,
  "provider_request_limits": {
    "aliyun": 123,
    "jina": 316
  },
  "provider_token_limits": {
    "aliyun": 13719,
    "jina": 132142
  },
  "step_request_limits": {
    "aliyun_document_canary": 1,
    "aliyun_document_diagnostic_20260906": 1,
    "aliyun_document_diagnostic_20260906_2": 1,
    "aliyun_document_retest_20260906": 1,
    "aliyun_query_canary": 1,
    "citation_quality": 396,
    "dual_index": 6,
    "historical": 6,
    "jina_connection": 3,
    "primary_query": 6,
    "recovery": 9,
    "standby_failover": 6
  },
  "providers": {
    "aliyun": {
      "total": 98,
      "reserved": 88,
      "forwarded": 88,
      "locally_blocked": 10,
      "estimated_input_tokens": 9242,
      "observed_tokens": 5202,
      "observed_usage_status": "unknown",
      "unknown_usage_attempts": 2,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 1009
    },
    "jina": {
      "total": 623,
      "reserved": 290,
      "forwarded": 290,
      "locally_blocked": 333,
      "estimated_input_tokens": 103786,
      "observed_tokens": 97644,
      "observed_usage_status": "unknown",
      "unknown_usage_attempts": 19,
      "unknown_forwarding_attempts": 18,
      "locally_blocked_estimated_tokens": 23573
    }
  },
  "steps": {
    "aliyun_document_canary": {
      "total": 1,
      "reserved": 1,
      "forwarded": 1,
      "locally_blocked": 0,
      "estimated_input_tokens": 13,
      "observed_tokens": 23,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "aliyun_document_diagnostic_20260906": {
      "total": 1,
      "reserved": 1,
      "forwarded": 1,
      "locally_blocked": 0,
      "estimated_input_tokens": 13,
      "observed_tokens": 23,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "aliyun_document_diagnostic_20260906_2": {
      "total": 1,
      "reserved": 1,
      "forwarded": 1,
      "locally_blocked": 0,
      "estimated_input_tokens": 13,
      "observed_tokens": 23,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "aliyun_document_retest_20260906": {
      "total": 1,
      "reserved": 1,
      "forwarded": 1,
      "locally_blocked": 0,
      "estimated_input_tokens": 13,
      "observed_tokens": 23,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "aliyun_query_canary": {
      "total": 1,
      "reserved": 1,
      "forwarded": 1,
      "locally_blocked": 0,
      "estimated_input_tokens": 106,
      "observed_tokens": 56,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "citation_quality": {
      "total": 668,
      "reserved": 344,
      "forwarded": 344,
      "locally_blocked": 324,
      "estimated_input_tokens": 111774,
      "observed_tokens": 100262,
      "observed_usage_status": "unknown",
      "unknown_usage_attempts": 18,
      "unknown_forwarding_attempts": 18,
      "locally_blocked_estimated_tokens": 23438
    },
    "dual_index": {
      "total": 2,
      "reserved": 2,
      "forwarded": 2,
      "locally_blocked": 0,
      "estimated_input_tokens": 104,
      "observed_tokens": 85,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "historical": {
      "total": 7,
      "reserved": 6,
      "forwarded": 6,
      "locally_blocked": 1,
      "estimated_input_tokens": 157,
      "observed_tokens": 242,
      "observed_usage_status": "unknown",
      "unknown_usage_attempts": 3,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 19
    },
    "jina_connection": {
      "total": 3,
      "reserved": 3,
      "forwarded": 3,
      "locally_blocked": 0,
      "estimated_input_tokens": 82,
      "observed_tokens": 234,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "primary_query": {
      "total": 6,
      "reserved": 6,
      "forwarded": 6,
      "locally_blocked": 0,
      "estimated_input_tokens": 144,
      "observed_tokens": 567,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "recovery": {
      "total": 15,
      "reserved": 6,
      "forwarded": 6,
      "locally_blocked": 9,
      "estimated_input_tokens": 162,
      "observed_tokens": 585,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 990
    },
    "standby_failover": {
      "total": 15,
      "reserved": 6,
      "forwarded": 6,
      "locally_blocked": 9,
      "estimated_input_tokens": 447,
      "observed_tokens": 723,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 135
    }
  }
}
```

限制：

- 本轮代码、离线工程检查、合并 CI、可信备份隔离恢复与 App-only 替换已完成。
- RelatedContent 只提供独立原文查阅，不改变回答资格或正式质量指标；新增 Provider HTTP 和模型 Token 为 0。
- 原18次真实传输故障具体异常类仍 UNKNOWN；本次定向观察与旧现场分开记录。
- 本次少量定向观察不能替代原60条全量质量验收，旧质量失败和实现身份失效均保留。
- SQLite仍为3.46.1 / 3.46.1-7+deb13u1；不可信备份入口缓解不等于漏洞修复。
- 18 CVE / 50 High-Critical 元组保持，未伪造人工风险批准；风险决定未通过，14天有效期尚未开始。
- 仅完成本机loopback、可信管理员、原允许出网公开资料范围的技术准备；不扩大到团队、客户、公网或外来备份。
- 当前仍为RuleBasedNormalizer；无生成式Query Rewrite、新模型调用、重建索引或重新向量化。
- 运行代码、合并提交与报告提交分别记录；main、Industry、release和feature线未推进。

详细证据来源、命令退出码、资产身份见同名 JSON。
MERGE_TO_MAIN_AUTHORIZED=false。
