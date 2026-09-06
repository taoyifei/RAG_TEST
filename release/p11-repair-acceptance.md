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
| PROVIDER_CONNECTIVITY_READY | PASS | ALL_REQUIRED_PASSED |
| DUAL_SLOT_FUNCTION_READY | FAIL | dual_index, primary_query |
| FAILOVER_RECOVERY_READY | BLOCKED | standby_failover, recovery |
| RETRIEVAL_QUALITY_READY | NOT_RUN | citation_quality |
| PRODUCT_BROWSER_READY | PASS | ALL_REQUIRED_PASSED |
| BACKUP_RESTORE_READY | PASS | ALL_REQUIRED_PASSED |
| SECURITY_READY | BLOCKED | os_risk |
| CI_READY | PASS | ALL_REQUIRED_PASSED |
| REMOTE_PRODUCTION_PROFILE_READY | FAIL | DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY |
| RELEASE_CANDIDATE_READY | FAIL | DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY, SECURITY_READY, REMOTE_PRODUCTION_PROFILE_READY |
| P11_READY | FAIL | RELEASE_CANDIDATE_READY |

预算与用量：

```json
{
  "status": "PASS",
  "cumulative": {
    "total": 17,
    "reserved": 16,
    "forwarded": 16,
    "locally_blocked": 1,
    "estimated_input_tokens": 501,
    "observed_tokens": 709,
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
        "total": 9,
        "reserved": 8,
        "forwarded": 8,
        "locally_blocked": 1,
        "estimated_input_tokens": 248,
        "observed_tokens": 196,
        "observed_usage_status": "unknown",
        "unknown_usage_attempts": 2,
        "unknown_forwarding_attempts": 0,
        "locally_blocked_estimated_tokens": 19
      },
      "jina": {
        "total": 8,
        "reserved": 8,
        "forwarded": 8,
        "locally_blocked": 0,
        "estimated_input_tokens": 253,
        "observed_tokens": 513,
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
      }
    }
  },
  "this_run": {
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
    "attempt-8f67222768654383af9584561dc6c844",
    "attempt-0a9267140d3245c0b9c14b089878a5ee"
  ],
  "providers_this_run": {
    "aliyun": {
      "total": 1,
      "reserved": 1,
      "forwarded": 1,
      "locally_blocked": 0,
      "estimated_input_tokens": 52,
      "observed_tokens": 48,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    },
    "jina": {
      "total": 1,
      "reserved": 1,
      "forwarded": 1,
      "locally_blocked": 0,
      "estimated_input_tokens": 52,
      "observed_tokens": 37,
      "observed_usage_status": "known",
      "unknown_usage_attempts": 0,
      "unknown_forwarding_attempts": 0,
      "locally_blocked_estimated_tokens": 0
    }
  },
  "this_turn": {
    "forwarded": 5,
    "estimated_input_tokens": 186,
    "observed_tokens": 319
  },
  "final_acceptance_total_new": {
    "forwarded": 10,
    "estimated_input_tokens": 344,
    "observed_tokens": 467,
    "baseline_forwarded": 6,
    "baseline_estimated_input_tokens": 157
  }
}
```

限制：

- 百炼文档/query与Jina文档/query/重排的真实连接验证均PASS；连接证据按当前操作身份复用。
- 双槽两Provider文档向量和索引任务完成，随后质量记录摘要ValidationError导致步骤FAIL；原失败保留，不能记为双槽PASS。
- 该两字段代码修复已通过1776项检查/7个CI作业及新653候选镜像门；当前运行App仍2a，待具体目标更新授权。
- 完整质量预算数值确认仍待回复；本轮仅在原25/1000、每Provider600上限内执行，累计16HTTP/501estimated。
- 新候选54High/Critical元组18CVE未有效处置，实际责任/期限/附件未提供。
- 主路、failover/recovery及原30问两路质量未执行；没有release/feature合并。

详细证据来源、命令退出码、资产身份见同名 JSON。
MERGE_TO_MAIN_AUTHORIZED=false。
