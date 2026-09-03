# P06 持久化开工审计

## 基线

- 集成分支：`feature/universal-rag`
- 开工 SHA：`bb13930ba3b06bcfcecb3f0acdf7c023e16afe1b`
- 阶段分支：`codex/p06-local-index`
- P05.5 anchor 的 ancestor 检查通过。
- 开工前工作树为空，未执行 stash、reset、clean 或 rebase。
- `main` 与 `Industry` 未修改。

## 权威合同与现状

P06 以 P05.5 的 document-scoped dver、独立 ParseContext、无持久化副作用 Parser、
content-addressed ParsedArtifact、三文本 Chunk V3、resolved Profile/Registry 和分槽
Embedding 身份为 Schema 基线。开工时已检查 P05.5 阶段报告、中期审计、两份架构决策、
Provider 配置传播、Chunk 质量报告和兼容迁移说明。

- `ParserPort` 接收 `ParseSource + ParsingPolicy + ParseContext`，返回含待提交 Artifact 的
  `ParseResult`；Parser 不拥有任何持久化 Store。
- `BlobStorePort` 只表达 content-addressed put/read/exists/delete；P06 另加 catalog/reference
  端口，业务删除不直接调用 Blob `delete()`。
- `VectorStorePort` 原有 slot 写接口保留兼容；P06 增加完整 named-vector Point 的不可变
  Revision API。
- `LexicalStorePort` 开工时是内存语义；P06 新增独立的 `sqlite-fts5` 注册名。
- `document_version_id` 保持
  `deterministic_id("dver", document_id, content_sha256)`，显示名和解析策略不进入 dver。
- `ParseResult.artifacts` 是待持久化 content-addressed 对象，不代表已经提交物理 Blob 或引用。
- Single Profile 只要求 primary slot；Hot-Standby Profile 要求 primary 与 standby，均来自
  resolved `ChunkingPolicy`。
- Query Embedding Router 在开工时仍只由 Hot-Standby composition 构造；Single 查询路由留给
  P07，不在 P06 扩张范围。
- 旧 `sqlite`、`local`、`memory` 注册名保持兼容；新持久化组件使用显式新名称。

## SQLite、Qdrant 与数据检查

- P06 新 Schema 从 `0001` 到 `0005`，分别覆盖控制面、Artifact、Revision/Chunk、
  FTS5/Exact、Embedding Cache/GC。
- 环境固定的 `qdrant-client` 版本为 `1.18.0`；已核对并采用完整 Point named-vector upsert、
  fetch、search、count 和 collection delete API。
- 未发现声明过的正式 P06 SQLite、Blob 或 Qdrant 数据。仓库内发现的 `.db` 仅属于工具缓存，
  不进入迁移范围。
- 因此 P06 使用新的数据目录和从 0001 开始的单调 migration，不执行旧 content-only dver
  或旧单向量 Collection 的原地改写。

## 开工门禁

- `python scripts/dev.py doctor`：Python、Git、source-tree import、SQLite FTS5 和临时目录通过；
  Node 按阶段规则跳过。
- `python scripts/dev.py check`：1206 passed，75 deselected，4 warnings。
- `python scripts/dev.py smoke`：62 passed，1 warning。
- Provider：未调用真实 Provider，网络模式保持 offline。
