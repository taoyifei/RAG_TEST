# 数据出网与费用边界

## 默认拒绝

默认离线 Profile 不允许任何远程调用。远程 Profile 必须分别授权：

```text
remote_document_embedding_jina
remote_query_embedding_jina
remote_reranking_jina
remote_document_embedding_aliyun
remote_query_embedding_aliyun
allow_aliyun_embedding_failover
```

厂商细分授权不能替代 `remote_document_embedding`、`remote_query_embedding` 或
`remote_reranking` 总授权。Router 和 adapter 都在构造网络请求前检查授权；一个厂商
失败不会绕过策略把正文发给另一个厂商。

## Secret

真实值只能来自环境变量或未来 SecretRef。仓库 Profile 和 catalog 只保存环境变量名。
日志、异常、Trace、截图和阶段报告不得包含 Authorization、Workspace secret、完整正文、
完整向量或完整响应。

阿里 V1 只允许 `cn-beijing`。Host 必须由格式受限的 Workspace ID 和固定模板
`{WorkspaceId}.cn-beijing.maas.aliyuncs.com` 构造，不接受任意 `base_url` 覆盖。

## 本地预算

自动备用要求正数 UTC 日请求预算和估算 Token 预算。`LocalUsageBudget` 的作用是限制当前
应用进程，不能查询云账户余额、跨进程统一计费，也不能保证请求仍处于免费额度。

Jina 和阿里都可能调整价格、免费额度、有效期和限流。2026-09-01 的官方页面显示阿里
北京 qwen3.7-text-embedding 免费额度具有有效期；该事实只用于人工评估，没有写入运行
分支。任何生产费用批准仍由项目所有者和云账户控制面负责。

## 内容与质量声明

真实 smoke 默认只允许公开合成短文本。企业 DOC 或 DOCX 是否允许发往 Jina 或阿里属于
项目级数据治理决定，必须逐项明确授权，不能由“Provider 声明不训练数据”替代。本次
任务中点名文件的例外授权和收紧预算记录在
`docs/decisions/P11-live-provider-authorization.md`。P02 的 MockTransport 证明合同、
状态机和隔离路径，不证明检索效果或成本。
