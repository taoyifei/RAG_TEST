# Product Runtime

`rag-app serve` 是唯一默认组合入口。调用链为：

```text
CLI
  -> ProductRuntimeSettings.from_environment
  -> create_product_lifespan_app
  -> lifespan startup
  -> build_product_runtime
  -> build_p09_runtime with product hooks
  -> create_product_app
  -> P09 API + product auth/routes + React host
```

组合根拥有 SQLite lifecycle/control、Filesystem Blob、Memory 或 Qdrant vector
adapter、FTS V2、Durable Job Runner、Provider Registry、Credential resolver、
Retrieval Profile resolver、SDK、Trace 与 Compatibility Manifest。关闭时先关闭缓存的
Provider client，再关闭 Job Runner 与底层 runtime。

## 最小配置

必须提供 `RAG_ADMIN_BOOTSTRAP_TOKEN_FILE`。普通源码运行默认使用
`.data/product`、`127.0.0.1:8088`、内存 vector store，并自动发现前端、migration
和 compatibility manifest。页面加密托管 Credential 时再提供
`RAG_MASTER_KEY_FILE`。持久 Qdrant 由 `RAG_QDRANT_MODE` 与 `RAG_QDRANT_URL`
选择。

Product Runtime 不要求 OCR、GPU、TEI endpoint 数组、Pipeline JSON、Retrieval
JSON、Tokenizer 资产或 `RAG_RELEASE_REVISION`。导入模块不会读取 Secret 或访问网络。

## 动态解析边界

每次 Query 通过 `retrieval_resolver` 读取知识库 Active Profile；每次新文档或版本
通过 `revision_builder_resolver` 做同样解析。P10.5 未获授权把真实 Provider 接入数据
面，因此 resolver 保持 P09 已验证的本地 FTS/Exact 与确定性离线构建服务。
Provider 未配置时主流程仍可运行。真实双槽 embedding/reranking 装配属于 P11。

Active Profile 与当前 Active Index Revision 的 fingerprint 不一致时，动态系统状态
返回 `reindex_required=true`。方案激活不修改既有索引，只产生新 Profile Revision。
