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
| PROVIDER_CONNECTIVITY_READY | NOT_RUN | jina_connection |
| DUAL_SLOT_FUNCTION_READY | NOT_RUN | dual_index, primary_query |
| FAILOVER_RECOVERY_READY | NOT_RUN | standby_failover, recovery |
| RETRIEVAL_QUALITY_READY | NOT_RUN | citation_quality |
| PRODUCT_BROWSER_READY | PASS | ALL_REQUIRED_PASSED |
| BACKUP_RESTORE_READY | PASS | ALL_REQUIRED_PASSED |
| SECURITY_READY | BLOCKED | os_risk |
| CI_READY | PASS | ALL_REQUIRED_PASSED |
| REMOTE_PRODUCTION_PROFILE_READY | NOT_RUN | PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY |
| RELEASE_CANDIDATE_READY | BLOCKED | PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY, SECURITY_READY, REMOTE_PRODUCTION_PROFILE_READY |
| P11_READY | BLOCKED | RELEASE_CANDIDATE_READY |

预算与用量：

```json
{
  "status": "PASS",
  "cumulative": {
    "total": 12,
    "reserved": 11,
    "forwarded": 11,
    "locally_blocked": 1,
    "estimated_input_tokens": 315,
    "observed_tokens": 390,
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
      "aliyun_document_retest_20260906": 1,
      "aliyun_query_canary": 1
    },
    "providers": {
      "aliyun": {
        "total": 8,
        "reserved": 7,
        "forwarded": 7,
        "locally_blocked": 1,
        "estimated_input_tokens": 196,
        "observed_tokens": 148,
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
    "estimated_input_tokens": 106,
    "observed_tokens": 56,
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
    "attempt-2cdbb956fc9c4b62b4ec264cd325dc00"
  ],
  "providers_this_run": {
    "aliyun": {
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
    }
  },
  "this_turn": {
    "forwarded": 2,
    "estimated_input_tokens": 119,
    "observed_tokens": 79
  },
  "final_acceptance_total_new": {
    "forwarded": 5,
    "estimated_input_tokens": 158,
    "observed_tokens": 148,
    "baseline_forwarded": 6,
    "baseline_estimated_input_tokens": 157
  }
}
```

限制：

- 运行实例已更新至2a726c，百炼document/query两项真实canary均PASS；原失败和诊断记录仍保留。
- 本次追加授权仅涵盖文档1次及成功后query1次，恰好2HTTP、零重试；不构成完整质量预算批准。
- 实际绑定完整预算保持PROPOSED，累计25/1000和每Provider600不变。
- 同一实际镜像的完整扫描仍有54个High/Critical元组、18CVE待有效处置。
- Jina本轮未重测；真实双槽、主路、failover/recovery及原30问两路质量未执行。release/feature均未合并。

详细证据来源、命令退出码、资产身份见同名 JSON。
MERGE_TO_MAIN_AUTHORIZED=false。
