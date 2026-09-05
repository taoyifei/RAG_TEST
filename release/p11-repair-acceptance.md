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
| CI_READY | BLOCKED | ci |
| REMOTE_PRODUCTION_PROFILE_READY | BLOCKED | ALIYUN_ENDPOINT_CONTRACT_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY |
| RELEASE_CANDIDATE_READY | BLOCKED | ALIYUN_ENDPOINT_CONTRACT_READY, CONNECTION_CONFIGURATION_READY, CAMPAIGN_BINDING_READY, PROVIDER_CONNECTIVITY_READY, DUAL_SLOT_FUNCTION_READY, FAILOVER_RECOVERY_READY, RETRIEVAL_QUALITY_READY, SECURITY_READY, CI_READY, REMOTE_PRODUCTION_PROFILE_READY |
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

- 本轮新增 Provider HTTP=0；另有官方公告、依赖/镜像仓库与GitHub访问，不把所有联网写成0。
- 当前非秘密配置缺显式百炼端点模式；业务空间模式还需真实控制台API Host。凭据元数据有效不等于供应商可用。
- 原实例未更新、未停止或重启；只读挂载/data，不挂Secret卷。首绑尚未执行，原Qdrant未改动。
- 累计原账6次转发、estimated157；known observed242、3次usage未知；本地拦截1次/estimated19，未知转发0。
- 预算434 HTTP/145703 estimated累计cap为PROPOSED、未批准、未激活；实际Profile确定后应重新生成。
- 真实Provider、双槽故障恢复与原30问Live质量未运行；离线Mock、候选容器和本地Qdrant不能证明Live质量或远程生产就绪。
- 完整OS发现和逐项未决结论保留，未形成任何人工风险接受；Python/npm audit不能代替OS安全门。
- 最初check的未暂存/静态环与恢复集成回归已修复并重跑原门，旧失败日志保留；未放宽断言或新增skip/noqa/xfail。
- 浏览器3个skip为已有desktop/mobile互斥用例；未跳过对应场景。
- R4原始扫描保留作为历史；当前候选独立完整扫描。最终报告提交不改变业务资产，因此不重复构建本地候选。
- 直接目录构建在载入上下文时被历史Trivy缓存权限阻断，未生成候选；改用同一已提交SHA的git archive输入完成唯一成功构建，未改动旧缓存或Dockerfile。
- 扫描入口的相对证据路径已修复为绝对路径；28项CLI回归、Ruff/mypy和原verify重跑。仅release脚本变化，运行时/测试/前端/镜像依赖身份未变，无需重建或重跑未受影响门。候选代码CI7项通过；本报告生成时最终合并CI待执行，旧CI不伪装为新脚本CI。

详细证据来源、命令退出码、资产身份见同名 JSON。
MERGE_TO_MAIN_AUTHORIZED=false。
