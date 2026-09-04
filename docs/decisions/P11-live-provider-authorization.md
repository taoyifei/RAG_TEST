# P11 真实 Provider 授权门

状态：`WAITING_FOR_USER_CONFIGURATION_AND_AUTHORIZATION`。

截至 2026-09-04，P11 的离线、浏览器 Mock、真实 Qdrant、备份恢复、升级、安全和
容器工作不需要 Jina 或阿里云密钥，且没有执行任何真实 Provider 请求。不得用这些
结果替代 Live 验收。

## 用户配置

不要在聊天、命令行参数或 GitHub 日志中发送 Key。管理员登录产品页面后打开“模型
服务”：

1. 新建 Jina 连接，选择页面加密托管，输入 API Key 并保存。
2. 依次测试 `jina-embeddings-v5-text-small` 的 document/query 与
   `jina-reranker-v3.5`。
3. 新建阿里云百炼连接，选择页面加密托管，输入 API Key、Workspace ID，区域固定
   `cn-beijing`，保存后测试 `qwen3.7-text-embedding` 的 document/query。
4. 不要上传企业文档；Live 验收会自行生成公开合成 DOCX。

如果通过 GitHub 手工工作流验收，则在 `p11-live-provider` Environment 配置
`P11_JINA_API_KEY`、`P11_ALIYUN_API_KEY`、`P11_ALIYUN_WORKSPACE_ID` 三个 Secret，
并启用 required reviewer。Repository `.env` 不用于这个工作流。

## 需要的明确授权

配置完成后，用户需在当前任务明确授权以下清单：

- 将发送：公开合成短文本和由公开合成文本生成的 DOCX 切片；
- 不会发送：企业文档、真实知识库、Secret、向量、Provider 原始响应体；
- 服务：Jina Embeddings、Jina Reranker、阿里云百炼 Qwen3.7 Embedding；
- 最大请求数：30 次 Provider HTTP 尝试，重试计入；
- 最大估算输入 Token：20,000；
- 操作：五项连接验证、双槽建索引、正常查询、Acceptance Proxy 阻断 Jina、Qwen
  自动切换、Half-open 恢复和后续 Primary 查询。

未获得这份授权前不运行 `tests/live/test_p11_live_providers.py`，也不触发
`P11 Live Provider` 工作流。

## 停止条件

模型 ID、Endpoint 或响应 Schema 与当前合同不一致，出现意外收费/配额、预算将超限、
需要发送非公开数据或需要修改真实 Key 制造故障时立即停止。问题记录为新的
`docs/decisions/P11-<topic>.md`，不改用 Mock 宣称通过。
