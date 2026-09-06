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
| PROVIDER_CONNECTIVITY_READY | NOT_RUN | aliyun_document_canary, aliyun_query_canary, jina_connection |
| DUAL_SLOT_FUNCTION_READY | NOT_RUN | dual_index, primary_query |
| FAILOVER_RECOVERY_READY | NOT_RUN | standby_failover, recovery |
| RETRIEVAL_QUALITY_READY | NOT_RUN | citation_quality |
| PRODUCT_BROWSER_READY | PASS | ALL_REQUIRED_PASSED |
| BACKUP_RESTORE_READY | PASS | ALL_REQUIRED_PASSED |
| SECURITY_READY | BLOCKED | os_risk |
| CI_READY | PASS | ALL_REQUIRED_PASSED |
| REMOTE_PRODUCTION_PROFILE_READY | BLOCKED | ALIYUN_ENDPOINT_CONTRACT_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY |
| RELEASE_CANDIDATE_READY | BLOCKED | ALIYUN_ENDPOINT_CONTRACT_READY, CONNECTION_CONFIGURATION_READY, CAMPAIGN_BINDING_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY, SECURITY_READY, REMOTE_PRODUCTION_PROFILE_READY |
| P11_READY | BLOCKED | RELEASE_CANDIDATE_READY |

预算与用量：

```json
{
  "status": "BLOCKED",
  "reason": "CAMPAIGN_BINDING_REQUIRED",
  "campaign_bound": false,
  "cumulative": {
    "total": 7,
    "reserved": 6,
    "forwarded": 6,
    "locally_blocked": 1,
    "estimated_input_tokens": 157,
    "observed_tokens": 242,
    "observed_usage_status": "unknown",
    "unknown_usage_attempts": 3,
    "unknown_forwarding_attempts": 0,
    "locally_blocked_estimated_tokens": 19,
    "source": "provider_operation_events_read_only_deduplicated",
    "validation_coverage": "RECONCILED_OR_RESERVED_UNKNOWN",
    "unmatched_validation_attempts": 0,
    "providers": {
      "aliyun": {
        "total": 3,
        "reserved": 2,
        "forwarded": 2,
        "locally_blocked": 1,
        "estimated_input_tokens": 38,
        "observed_tokens": null,
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
    "request_limit": 25,
    "estimated_token_limit": 1000,
    "provider_token_limits": {
      "jina": 600,
      "aliyun": 600
    }
  },
  "this_run": {
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
  "remaining": {
    "requests": 19,
    "estimated_input_tokens": 843
  }
}
```

限制：

- 本轮Provider HTTP=0、estimated=0，未发送私有DOC/DOCX；官方公告、镜像仓库和GitHub访问另计。
- 原App仍为20864e7e，候选仍为d98a8d16；未更新/停止/重启原实例，Qdrant未操作。原库17项migration checksum相符，无待执行产品SQL migration。
- 原库无任何已保存或草稿方案；实际计划actual_profile_bound=false、PROPOSED且未批准，默认434HTTP/145703estimated不可直接批准。
- 最新旧账6次转发/157estimated；known observed242、3次usage未知；本地拦截1次/estimated19、未知转发0。原累计授权25/1000与每Provider600未修改，campaign未首绑。
- 百炼缺显式endpoint_mode；workspace_host模式还需控制台真实API Host。凭据元数据有效不证明真实连接可用；Jina连接新Live证据未执行。
- 真实Provider、双槽、failover/recovery与冻结30问两路质量未执行；候选Mock、本地Qdrant与离线测试只证明各自工程合同。
- 预算/CLI/resolved-policy定向57通过；冻结后原check1751通过、88deselected，Ruff/mypy/docstrings通过。预算回归曾发现sidecar写入与WAL错误不可见，均修复且原严格断言保留，旧失败日志仍在artifacts/p11-final。
- 原有已测业务/测试/前端/镜像/迁移资产逐文件及release函数核对后复用，26份原PASS证据哈希已核验；复用记录保留reused_from。新增CLI仅影响预算函数，新check与CI单独绑定当前代码。
- 浏览器3个skip为原desktop/mobile互斥用例，未增加跳过；R5最初check、恢复回归、直接目录构建缓存权限和verify相对路径失败保留于原R5日志，不冒称本轮重新执行。
- 当前镜像完整未过滤扫描有效复用；54个High/Critical元组、18CVE仍待处置。基础标签和官方Trixie状态重新核查，Perl位数/模块缺失补证不等于风险豁免，未代填任何人工批准。
- CI33987948399对应旧合并7e46cd9已成功；本轮CI34014470636对应6c011c3七项成功。后续文档提交不改变被测业务资产，不重复本地重建、向量化或收费验证。
- 本轮未合回release或feature/universal-rag；main/Industry未改。CODE_FIXES_READY不替代P11_READY，也不构成预算、风险或运行实例更新授权。

详细证据来源、命令退出码、资产身份见同名 JSON。
MERGE_TO_MAIN_AUTHORIZED=false。
