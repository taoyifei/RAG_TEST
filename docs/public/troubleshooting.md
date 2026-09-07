# 故障排除

## Compose 启动失败

- `qdrant.yaml` 缺失：先执行 Quickstart 的 `init-secrets --directory`，不要手写
  Key 到 Compose。
- 端口占用：同时修改 `.env` 的 `RAG_PORT` 与 `RAG_TRUSTED_ORIGINS`。
- 宿主访问返回 `TLS_REQUIRED`：确认使用根 `compose.yaml`、端口只绑定
  `127.0.0.1`，且容器存在 `RAG_TRUST_LOOPBACK_HOST_PROXY=true`。远程访问必须
  配置 HTTPS 反向代理，不能关闭安全检查。
- `/live` 成功但 `/ready` 失败：读取安全错误码、Provider 验证和索引 Inventory；
  不把存活状态当生产就绪。

## Provider 验证失败

页面只显示脱敏分类。401 表示凭据无效，403 表示权限不足，429 表示限流，5xx 或
timeout 表示暂时不可用，维度/JSON/候选错误表示当前 API 合同不兼容。确认模型 ID、
Workspace 与 `cn-beijing`，不要通过修改真实 Key 制造故障。轮换 Key 后必须重新
验证所有引用连接。

## 索引或查询失败

- Primary/Standby 任一覆盖率不足时激活应 Fail Closed。
- Qwen Query Vector 只能查询 `dense_standby`。
- FTS V1 数据会明确要求 Reindex，不会自动冒充 FTS V2。
- Circuit 打开时先核对请求/Token 预算；预算不足会阻止 Standby。

## 发布门禁失败

任何 `check`、`verify`、`acceptance`、CI、漏洞扫描或真实 Provider 门失败时停止
发布，修复后重跑原门禁。审计服务网络不可达记为 `BLOCKED`，不能改写成通过。
真实调用发现意外收费、配额或 API Schema 漂移时记录
`docs/decisions/P11-<topic>.md` 并停止。
