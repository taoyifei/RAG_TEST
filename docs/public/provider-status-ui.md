# Provider 状态 UI

系统页面的普通状态读取只调用 `/api/v1/system/components`，不会主动访问远端 Provider。
组件列表、Profile、index/serving fingerprint、FTS analyzer、P08 离线质量状态及
Primary/Standby LIVE 状态全部来自 API。

`not_verified`、`configured`、`quality_not_evaluated` 等状态使用中性文字和图标，不显示为
绿色 Healthy。当前阶段没有 Jina 或阿里云真实校准证据，因此
`REMOTE_PRODUCTION_PROFILE_READY` 保持 false。

Provider Probe 只有 Admin 可见。第一次点击只显示网络和费用警告；第二次确认才发送
`X-Allow-Network: true` 与有界 `X-Request-Budget`。控制台不周期性 Probe，也不展示
API Key、Workspace ID 或 Provider 原始响应。

