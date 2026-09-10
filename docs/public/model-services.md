# 配置模型服务

管理员先在“模型服务”创建 Provider Credential 和 Connection。页面托管密钥要求服务
启动时配置 `RAG_MASTER_KEY_FILE`；输入值只用于当前提交且不会回显。部署环境托管只保存
环境变量名，密文到实际调用边界才在服务端解析。连接验证证明当前端点和响应合同可用，
不等于远程质量、容量、预算或生产可用性已通过。

## Retrieval Profile 与实际查询数据面

知识库的远程检索配置使用不可变 Retrieval Profile Revision。管理员创建草稿、验证引用
的 Connection、查看影响并激活；embedding 模型、维度或指令变化要求构建新 Index
Revision。只有向量覆盖与 Revision 校验完成后，Profile 和新 Revision 才原子切换；失败
时旧活动版本继续服务。轮换页面托管密钥会使相关连接验证过期，但不会自动重建索引。

“知识库模型”显示的 `retrieval_data_plane` 来自持久化运行状态，而不是下拉框：

- `active_remote_profile`：查询已解析到活动 Profile，并显示实际 Embedding Provider、
  模型、向量空间、Reranker、Index Revision 与服务指纹；
- `default_local_fallback`：没有活动 Profile，使用本地 deterministic + lexical/结构检索；
  页面不会把它标成真实 Dense；
- `DRAFT_NOT_ACTIVE`、`REBUILD_PENDING`、`REBUILD_FAILED`：草稿尚未服务或重建状态明确；
- `PROFILE_INDEX_MISMATCH`、`VECTOR_COVERAGE_INCOMPLETE`、
  `PROFILE_VALIDATION_STALE`：活动 Profile 与当前数据面不满足安全使用条件；
- `DENSE_CALIBRATION_MISSING`：远程 Dense 尚未完成质量校准，不能据此声明 Live Ready。

没有活动 Profile 不会替管理员发起付费调用。本地 Exact、FTS、结构 Evidence 与 renderer
仍能回答来源明确的问题；`fallback_reason_codes` 和 `/retrieval-profiles` 修复入口会保留。

## 回答、解释与改写模型

知识库可选择 generation 模型，并按需开启一次查询解释/改写。高置信通用问法直接使用
本地类型化语义；只有规则不确定且配置、操作授权与预算均有效时才调用模型。原问题永远
保留，模型产生的语义或改写必须保持标识符、数字、单位、否定、来源限定和日期/版本，
且只用于检索。回答模型只接收最小充分支持集，输出还要经过 claim 与引用校验。

模型未配置、未授权、超预算、超时或返回无效结构时不会覆盖本地证据。支持集完整就发布
结构化 `extractive_fallback` 并显示具体原因；支持集不完整才拒答、澄清或返回明确的
配置/预算/Provider 状态。

## 批准当前活动语料

远程 generation 等携带业务来源的操作还需要管理员在“知识库模型”中执行一次“批准当前
活动知识库资料”。服务端从当前活动 Revision 计算不可变
`CorpusAuthorizationManifest`，绑定：

- Project、知识库、活动 Index Revision 与全部活动文档版本的整体摘要；
- 活动文档数量、Provider Connection/模型和获准 operations；
- authorization ID、budget campaign、策略版本、创建和到期时间；
- 每项操作的请求次数与估算 Token 上限。

浏览器不接收逐文档 hash、正文或 Secret。generation 的实际 source hash 仍必须是批准
清单的子集；不携带正文的 `query.rewrite` 仍要求模型、operation、有效期和预算一致。
新文档、新版本、删除、恢复、活动 Revision 或模型变化后，旧批准会显示为
`STALE_CORPUS`、`STALE_REVISION` 或 `STALE_MODEL`，不会静默跟随。到期和累计预算耗尽也会
分别显示；重新批准必须由真实管理员会话和 CSRF 操作完成，AI 与后台任务不能代签或扩大
预算。

## 真实 Provider 边界

默认开发、测试和公开 V3-07 回归为离线/keyless，外部调用数为 0。真实 Provider Lane
只有在已有有效数据出网、模型、operation 和累计预算授权时才运行，并独立报告实际
provider/model/vector space、调用次数、usage、故障和延迟。公开合成语料的批准不能借给
私有知识库；缺少私有语料出网授权时，私有 Live 质量保持 `BLOCKED`，本地直接证据能力
仍可单独验收。

完整问答行为见 [检索与问答](search-and-answer.md)，数据出网边界见
[数据出网与费用](data-egress-and-cost.md)。
