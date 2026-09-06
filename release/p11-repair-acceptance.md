# P11 当前验收

P11_READY=False
CODE_FIXES_READY=True

| Gate | 状态 | 原因 / 待补证据 |
| --- | --- | --- |
| AUTHORIZATION_BOUNDARY_READY | PASS | ALL_REQUIRED_PASSED |
| ALIYUN_ENDPOINT_CONTRACT_READY | PASS | ALL_REQUIRED_PASSED |
| CONNECTION_CONFIGURATION_READY | PASS | ALL_REQUIRED_PASSED |
| CAMPAIGN_BINDING_READY | PASS | ALL_REQUIRED_PASSED |
| CONNECTION_EDIT_READY | PASS | ALL_REQUIRED_PASSED |
| RESOLVED_POLICY_CONFORMANCE_READY | PASS | ALL_REQUIRED_PASSED |
| PROFILE_INDEX_SWITCH_READY | PASS | ALL_REQUIRED_PASSED |
| PROVIDER_CONNECTIVITY_READY | FAIL | aliyun_document_canary, aliyun_query_canary, jina_connection |
| DUAL_SLOT_FUNCTION_READY | NOT_RUN | dual_index, primary_query |
| FAILOVER_RECOVERY_READY | NOT_RUN | standby_failover, recovery |
| RETRIEVAL_QUALITY_READY | NOT_RUN | citation_quality |
| PRODUCT_BROWSER_READY | PASS | ALL_REQUIRED_PASSED |
| BACKUP_RESTORE_READY | PASS | ALL_REQUIRED_PASSED |
| SECURITY_READY | BLOCKED | os_risk |
| CI_READY | PASS | ALL_REQUIRED_PASSED |
| REMOTE_PRODUCTION_PROFILE_READY | FAIL | PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY |
| RELEASE_CANDIDATE_READY | FAIL | PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY, SECURITY_READY, REMOTE_PRODUCTION_PROFILE_READY |
| P11_READY | FAIL | RELEASE_CANDIDATE_READY |

预算与用量：

```json
{
  "status": "PASS",
  "cumulative": {
    "total": 10,
    "reserved": 9,
    "forwarded": 9,
    "locally_blocked": 1,
    "estimated_input_tokens": 196,
    "observed_tokens": 311,
    "observed_usage_status": "unknown",
    "unknown_usage_attempts": 3,
    "unknown_forwarding_attempts": 0,
    "locally_blocked_estimated_tokens": 19,
    "campaign_id": "p11-20260905-public-synthetic",
    "authorization_id": "p11-20260904-existing-25-1000",
    "request_limit": 25,
    "estimated_token_limit": 1000,
    "provider_request_limits": {},
    "provider_token_limits": {
      "aliyun": 600,
      "jina": 600
    },
    "step_request_limits": {
      "aliyun_document_canary": 1,
      "aliyun_document_diagnostic_20260906": 1,
      "aliyun_document_diagnostic_20260906_2": 1,
      "aliyun_query_canary": 1
    },
    "providers": {
      "aliyun": {
        "total": 6,
        "reserved": 5,
        "forwarded": 5,
        "locally_blocked": 1,
        "estimated_input_tokens": 77,
        "observed_tokens": 69,
        "observed_usage_status": "unknown",
        "unknown_usage_attempts": 2,
        "unknown_forwarding_attempts": 0,
        "locally_blocked_estimated_tokens": 19
      },
      "jina": {
        "total": 4,
        "reserved": 4,
        "forwarded": 4,
        "locally_blocked": 0,
        "estimated_input_tokens": 119,
        "observed_tokens": 242,
        "observed_usage_status": "unknown",
        "unknown_usage_attempts": 1,
        "unknown_forwarding_attempts": 0,
        "locally_blocked_estimated_tokens": 0
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
      }
    }
  },
  "this_run": {
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
  "imported_history": {
    "total": 0,
    "reserved": 0,
    "forwarded": 0,
    "locally_blocked": 0,
    "estimated_input_tokens": 0,
    "observed_tokens": null,
    "observed_usage_status": "unknown",
    "unknown_usage_attempts": 0,
    "unknown_forwarding_attempts": 0,
    "locally_blocked_estimated_tokens": 0
  },
  "attempt_ids": [
    "attempt-cb15305ef7114f1db7923e715f71a907"
  ],
  "providers_this_run": {
    "aliyun": {
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
    }
  },
  "this_turn": {
    "forwarded": 3,
    "estimated_input_tokens": 39,
    "observed_tokens": 69
  },
  "latest_diagnostic": {
    "path": "artifacts/p11-final/aliyun-response-diagnostic-2.json",
    "sha256": "3eb2abc66a0fb251bb131058010b2c35e1f89880d69e2ea61eec44cdca549bec"
  },
  "latest_operation": "aliyun_document_diagnostic_20260906_2"
}
```

限制：

- 真实百炼返回已证明为有效1024维向量；原canary失败因SDK外包字段误判，修复后的目标实例尚未更新/实测。
- 当前App与新候选镜像不同；不能用隔离Mock或CI宣称Live通过。
- 完整实际方案预算仍PROPOSED；累计25/1000及每Provider600不变。原canary单次额度和两次诊断额度已耗尽。
- 新镜像179条全等级发现，54个High/Critical元组、18CVE全部UNDER_INVESTIGATION。
- 双槽、failover/recovery、原30问两路真实质量未执行；没有release/feature合并。

详细证据来源、命令退出码、资产身份见同名 JSON。
MERGE_TO_MAIN_AUTHORIZED=false。
