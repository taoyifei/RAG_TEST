# P11 阿里云百炼真实验证请求错误

状态：`WAITING_FOR_CORRECT_WORKSPACE_ID`。

发现日期：2026-09-05。

## 触发条件

页面已保存一个 `database_encrypted` 的百炼连接，Region 为 `cn-beijing`，
Workspace ID 已配置。任务书要求验证 `qwen3.7-text-embedding`、
`text_type=document|query`、1024 维、`output_type=dense` 和 usage。

当前实现使用官方原生 Endpoint：

```text
POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/
api/v1/services/embeddings/text-embedding/text-embedding
```

请求体只含固定公开合成文本、model、`input.texts` 和
`parameters.text_type/dimension/output_type`，没有企业文档、Secret、向量或
真实知识库。当前参考为阿里云官方
[文本向量同步 API](https://help.aliyun.com/en/model-studio/text-embedding-synchronous-api)
和[错误码](https://help.aliyun.com/en/model-studio/error-code)。

## 第一次失败与分类修复

候选 `358f7560eb621f8b2a8736640fc36b558f51462b` 的首次 document 验证为
HTTP 4xx、19 估算输入 Token、无 observed usage、266 ms；旧安全错误为
`REGION_OR_WORKSPACE_INVALID`。审计发现旧逻辑把百炼所有 HTTP 400 都无条件映射
为 Workspace/Region 错误，可能把参数或鉴权问题错误归因。

提交 `c4e68dfa1ba61fef8ac590e81b663682bb0c1a2b` 改为只读取 4 MiB 内 JSON
的白名单 `code`，支持官方 `InvalidApiKey`、`NOT AUTHORIZED`、
`WorkSpaceNotFound` 和访问拒绝类别；未知 code 与原始 message 不持久化。

```text
targeted provider tests: 46 passed, 1 warning
Ruff / format / mypy: passed
full offline gate: 1471 passed, 79 deselected, 4 warnings
```

部署前备份 `pre-c4e68df.tar.gz` 已通过校验，SHA-256 为
`22e9e3323ced776490815f3ab58e698d3e466455f61e97aacbe9d85f0a96f62a`。
候选镜像 digest 为
`sha256:0a67aa625aa05bf9b01a0ddc899cd4d19106fa9c59a03b05c36a9022376271ad`，
OCI revision 与提交一致。只重建 app，Qdrant 容器 `1bc4088a449c` 未变。

## 新候选真实重试

通过正式控制台 Session/API 只执行一次 document 重试，没有自动重试：

```text
HTTP attempts: 1
Estimated input tokens: 19
Observed provider tokens: unavailable
Status: failed
HTTP category: http_4xx
Safe error: PROVIDER_REQUEST_INVALID
Latency: 270 ms
```

旧 Workspace 结论已被推翻，但正式请求仍被真实服务拒绝。百炼 query、双槽索引、
产品查询、故障切换与恢复均未运行；`GM-03 质量管理制度.doc` 未出网。

当前总账为 6/25 次 Provider HTTP、157/1,000 估算输入 Token；Jina
4 次、119/600，百炼 2 次、38/600。成功响应观察用量只有 Jina 的 242 Token。

## 决策

1. 按任务书“真实模型 ID/API Schema 与官方当前合同不一致”条款暂停后续
   Provider 请求。
2. `LIVE_QWEN_STANDBY_READY`、`AUTOMATIC_FAILOVER_READY`、
   `PRODUCT_E2E_READY`、`REMOTE_PRODUCTION_PROFILE_READY` 与 `P11_READY`
   保持 `false`。
3. 不删除任务书要求的 `text_type=document|query` 来制造表面成功，不用 Mock
   替代百炼 Live。
4. 只有确认同一北京 Workspace/API Key 已开通 `qwen3.7-text-embedding`，或批准
   一次有界的官方最小请求对照诊断后，才恢复 Live Gate；不得在聊天中粘贴 API Key。

## 零调用根因核验与生产护栏

对已保存连接只做形状核验，没有输出 Workspace ID、API Key、哈希或正文。结果为：
Region 是 `cn-beijing`，凭据仍是页面加密托管且 Key 形状正常，但 Workspace ID
不以阿里云官方要求的 `llm-` 开头。官方控制台应复制 Workspace ID，而不是 App ID、
Workspace 名称或 API Host 片段；参考
[获取 Workspace ID](https://help.aliyun.com/zh/model-studio/obtain-the-app-id-and-workspace-id)
与[地域和 Workspace](https://help.aliyun.com/zh/model-studio/regions/)。

提交 `224ac930be701cfd6d53ecede8501071cf9129da` 在创建 HTTP Client 前要求
`llm-` 前缀，避免无效配置继续出网消耗预算。验证证据：

```text
provider runtime tests: 47 passed, 1 warning
previously failing product tests: 5 passed, 1 warning
full offline gate: 1472 passed, 79 deselected, 4 warnings
```

部署前备份 `pre-224ac93.tar.gz` 校验通过，SHA-256 为
`2fcd5a2d4b9c9e9bc6506796122cce34d229a198298362347f2faae0a4ab0ed5`；
候选镜像 digest 为
`sha256:f82968df74884c80008fb698aa3f55bea6128ffca68b93d22266ce53ceed731f`，
120,069,668 bytes，OCI revision 与提交一致。只替换 app 后健康；Qdrant 容器
`1bc4088a449c` 与创建时间未变，2 个 Connection、2 个 Credential 和 6 条历史
验证记录均保留。

通过正式 Session/API 执行一次生产护栏验收，3 ms 即以
`PROVIDER_CONFIGURATION_INVALID` / `invalid_configuration` 本地失败；Provider HTTP
为 0，observed usage 为空。验证记录从 6 增至 7，其中 19 Token 只是未发送的本地
估算，不计入真实 Provider 消耗；真实总账仍为 6/25 次 HTTP、157/1,000 估算输入
Token。下一次真实百炼请求须等用户在页面保存以 `llm-` 开头的正确 Workspace ID
并确认后再运行。
