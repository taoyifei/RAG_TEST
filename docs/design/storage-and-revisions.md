# P06 Storage 与不可变 IndexRevision

## 权威边界

SQLite 是身份、生命周期、Blob 引用、Chunk、FTS、Embedding 进度和 Active 指针的权威
控制面。Filesystem Blob 只保存 content-addressed bytes。Memory 或 Qdrant 只保存按
Revision 隔离的完整 Named Vector Point，不保存正文。

DocumentVersion 的身份固定为 `dver(document_id, content_sha256)`。ParsingPolicy、Parser、
Chunker、Embedding topology、Lexical schema 和 Vector schema 都保存在 Revision 合同中，
不进入 DocumentVersion 身份。两个逻辑文档可以共享同一物理 Artifact，但 dver、node 和
chunk 身份仍按 document scope 分离。

## Schema 与 migration

当前 schema version 为 5：

1. `0001_control.sql`：project、knowledge base、document、document version、job、metadata。
2. `0002_artifacts.sql`：Blob catalog 和引用表。
3. `0003_revisions_chunks.sql`：Revision、文档绑定、Chunk、slot、进度、coverage、Active history。
4. `0004_fts5.sql`：FTS5 和 Exact Identifier。
5. `0005_embedding_cache_gc.sql`：持久化向量 cache、Provider 用量和 GC plan。

Migration 文件名和版本必须连续，已应用 checksum 不得漂移，未知更高版本 fail closed。
每个 migration 在显式事务中执行。SQLite 每个事务使用独立连接，启用 FK、row factory、
有界 busy timeout；WAL、DELETE 和 MEMORY 由严格配置选择。

## Revision 状态与激活

Build 固定经过 created、parsing、chunking、embedding、lexical indexing、vector indexing、
validating、ready、active。失败只写 retryable 或 terminal 状态，不能改变旧 Active。

Validator 从 SQLite、FTS 和 Vector Store 重新读取并检查 scope、canonical IR/Chunk、报告、
实际行数、完整 Point、维度、coverage、指纹和确定性 fetch/query。证据写入 READY 后，
Active 指针、旧 Active 到 RETIRED、历史记录在同一 SQLite 写事务中完成。Qdrant 不参与
分布式事务，因此只有已经完整构建和回读验证的独占 collection 才能激活。

## 路径与指纹

data root、SQLite 文件名和 Qdrant local path 是部署位置，不进入 index fingerprint。
Store 类型、Qdrant local-memory/local-path 模式、schema、resolved policy 和 required slots
进入组件或 Revision 合同。配置禁止未知字段，Registry 仍只由可信代码显式注册。
