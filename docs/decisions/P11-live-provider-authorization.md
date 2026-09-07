# P11 真实 Provider 授权门

状态：`AUTHORIZED_CONNECTIONS_SAVED_WAITING_FOR_CORRECT_ALIYUN_WORKSPACE`。

截至 2026-09-04，P11 的离线、浏览器 Mock、真实 Qdrant、备份恢复、升级、安全和
容器工作已经完成。随后使用页面保存的加密 Jina 连接发起首次真实验证；第一项即以
`PROVIDER_NETWORK_ERROR` 失败并停止。不得用此前 Mock 结果替代 Live 验收。

## 2026-09-04 授权更新

- 用户已明确授权发送公开合成短文本、公开合成 DOCX 切片，以及指定文件
  `GM-03 质量管理制度.doc` 的内容；该授权不扩展到其他企业文档；
- 本次授权覆盖本地与 CI 合计，Provider HTTP 尝试累计上限为 30 次，重试
  计入；估算输入 Token 累计上限为 20,000；
- 为落实用户后续“不要消耗太多 Token”的要求，本次执行进一步自行收紧为最多
  25 次 Provider HTTP 尝试、全局最多 1,000 估算输入 Token、每个 Provider
  最多 600；收紧值不会自动回升到外层授权值；
- 页面已保存一个 Jina 与一个阿里云百炼连接，均为
  `database_encrypted`；百炼 Workspace 已配置且 Region 为
  `cn-beijing`；
- 页面五项真实连接验证已运行第一项 Jina document embedding：1 次 Provider
  HTTP、19 估算输入 Token，结果为 `network_error`，没有可用的 Provider usage；
  其余四项按失败即停规则为 `NOT_RUN`，百炼实际请求为 0；
- 指定 DOC 已在无网络、只读根文件系统、非 root 的本地容器中完成真实解析：
  27,136 bytes、73 nodes、28 chunks，并保留原文件 SHA；未输出正文；
- 按 Live harness 的预算估算口径，28 个 chunk 每个 embedding slot 需要约
  4,158 输入 Token，双槽仅建索引约需 8,316，尚未计入查询和重排。它超过当前
  全局 1,000/每 Provider 600 硬上限，因此必须在首个 Provider 请求前停止；
- `database_encrypted` 仅说明存储模式；数据库、日志与浏览器明文扫描仍为
  `NOT_RUN`；
- 浏览器控制 helper 故障只影响自动化浏览器证据，不视为产品失败；C2 浏览器
  完整路径仍为 `NOT_RUN`；
- 首次请求前发现的页面百炼校验合同漂移已完成修复、完整离线门禁、候选重建和
  原位部署；Jina 第一项真实请求在收到可验证模型响应前发生网络错误，因而未进入
  百炼验证。处置见 [P11-page-provider-contract-drift.md](P11-page-provider-contract-drift.md)
  与 [P11-live-jina-network-error.md](P11-live-jina-network-error.md)。

## 用户配置

不要在聊天、命令行参数或 GitHub 日志中发送 Key。管理员登录产品页面后打开“模型
服务”：

1. 新建 Jina 连接，选择页面加密托管，输入 API Key 并保存。
2. 依次测试 `jina-embeddings-v5-text-small` 的 document/query 与
   `jina-reranker-v3.5`。
3. 新建阿里云百炼连接，选择页面加密托管，输入 API Key、Workspace ID，区域固定
   `cn-beijing`，保存后测试 `qwen3.7-text-embedding` 的 document/query。
4. 除已明确授权的 `GM-03 质量管理制度.doc` 外，不要上传其他企业文档；低预算
   Live 验收会自行生成公开合成 DOCX。

如果通过 GitHub 手工工作流验收，则在 `p11-live-provider` Environment 配置
`P11_JINA_API_KEY`、`P11_ALIYUN_API_KEY`、`P11_ALIYUN_WORKSPACE_ID` 三个 Secret，
并启用 required reviewer。Repository `.env` 不用于这个工作流。

## 本次授权与运行边界

用户已经在当前任务完成配置与授权。有效边界如下：

- 可发送：公开合成短文本、由公开合成文本生成的 DOCX 切片，以及指定 DOC 的切片；
- 不会发送：其他企业文档、其他真实知识库、Secret、向量、Provider 原始响应体；
- 服务：Jina Embeddings、Jina Reranker、阿里云百炼 Qwen3.7 Embedding；
- 本次执行硬上限：25 次 Provider HTTP 尝试、全局 1,000 估算输入 Token、
  每个 Provider 600，重试计入；
- 操作：五项连接验证、双槽建索引、正常查询、Acceptance Proxy 阻断 Jina、Qwen
  自动切换、Half-open 恢复和后续 Primary 查询。

发布候选已经完成离线门禁、重建和原位重部署。首次 Jina document validation 实际
发送 19 估算输入 Token 后发生 `PROVIDER_NETWORK_ERROR`，Live Gate 已暂停；当前累计
为 1/25 次 HTTP、19/1,000 估算输入 Token，Jina 19/600、百炼 0/600。指定 DOC 的
完整双槽发布另因预算不足保持 `BUDGET_BLOCKED`，不得截断文档、减少槽位或改用 Mock
冒充；只有用户再次明确提高本次硬上限后才可执行。若要覆盖完整索引、查询和重排，
应预留高于 8,316 的全局额度及高于 4,158 的单 Provider 额度。

## 停止条件

模型 ID、Endpoint 或响应 Schema 与当前合同不一致，出现网络/鉴权/限流错误、意外
收费/配额、预算将超限、需要发送授权范围外的非公开数据或需要修改真实 Key 制造故障
时立即停止。问题记录为新的 `docs/decisions/P11-<topic>.md`，不改用 Mock 宣称通过。


## 2026-09-05 当前状态（取代上文旧账本）

Jina document/query/reranking 已取得真实 `live_200`；Jina 累计 4 次 Provider
HTTP、119 估算输入 Token，成功响应观察用量合计 242 Token。百炼 document 累计
2 次 HTTP、38 估算输入 Token，均为 HTTP 4xx；候选
`c4e68dfa1ba61fef8ac590e81b663682bb0c1a2b` 的最新安全分类为
`PROVIDER_REQUEST_INVALID`，百炼 query 未运行。

当前总账为 6/25 次 Provider HTTP、157/1,000 估算输入 Token；Jina
119/600，百炼 38/600。指定 `GM-03 质量管理制度.doc` 未出网，完整双槽发布仍为
`BUDGET_BLOCKED`。后续 Provider 调用已按任务书暂停。详见
[P11-live-jina-network-error.md](P11-live-jina-network-error.md) 和
[P11-live-aliyun-validation-error.md](P11-live-aliyun-validation-error.md)。

## 2026-09-05 Workspace 根因与零调用护栏（取代上节百炼归因）

只核验连接与加密凭据形状后确认：Region 与 Key 形状正常，已保存的 Workspace ID
不符合官方 `llm-` 前缀。提交
`224ac930be701cfd6d53ecede8501071cf9129da` 已把此前宽松 slug 校验收紧为
HTTP 前置校验；完整离线门禁为 `1472 passed, 79 deselected`。

新镜像部署后，正式 Session/API 的生产护栏验收以
`PROVIDER_CONFIGURATION_INVALID` 本地结束，Provider HTTP 为 0；因此真实总账仍为
6/25 次 HTTP、157/1,000 估算输入 Token，指定 DOC 仍未出网。用户须在页面新建或
更新百炼连接，复制控制台中以 `llm-` 开头的 Workspace ID，且不要把 Workspace ID
或 API Key 粘贴到聊天；保存后才恢复一次最小 document Live 验证。
