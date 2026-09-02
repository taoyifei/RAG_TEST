# P00-P05 中期硬化事实审计

## 审计边界

- 实际基线 SHA：`c8297e138bffe848bf53dc59effacd5b51c750b0`
- 集成分支：`origin/feature/universal-rag`
- 阶段分支：`codex/p05-5-midterm-hardening`
- 只读引用：`origin/main@af30f81fbcbd0577c16fbf59bb9bce8f29a3de91`、
  `origin/Industry@5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`
- 审计目录文件数：Core 20、Application 4、Adapters 54、Composition 7、
  Tests 233、Architecture 4、Design 7、Progress 6。
- Python/工具链：Python 3.11.15、Ruff 80 列、strict mypy。

## P00-P05 合并图

集成线依次包含 P00 `06b6325/578585f/09702a0`、P01
`0588ed3/464f0a7`、P02 `b4c0b06/c667d05`、P03
`901d7a5/cef85e1/1be6790`、P04 `b0f0dab/f0e4c04` 和 P05
`0ba8331/c8297e1`。所有阶段均以显式 merge commit 进入唯一集成分支；
本阶段不读取或修改 `main`、`Industry` 的工作树内容。

## 修改前统一门禁

| 命令 | 修改前结果 |
| --- | --- |
| `.venv/bin/python scripts/dev.py doctor` | PASS；Python、Git、source-tree import、SQLite FTS5、临时目录 OK；Node SKIP |
| `.venv/bin/python scripts/dev.py check` | PASS；1172 passed、75 deselected、4 warnings；compileall、Ruff、strict mypy 207 files、docstring 均通过 |
| `.venv/bin/python scripts/dev.py smoke` | PASS；60 passed、1 warning |

默认入口清除 RAG/Provider 环境变量并设置 `RAG_TEST_NETWORK=offline`。
External services actually called: none。

## 当前端口签名

```python
ParserPort.parse(source: ParseSource, policy: ParsingPolicy) -> ParseResult
BlobStorePort.put(request: BlobWriteRequest) -> None
BlobStorePort.get(blob_id: str) -> BlobReadResult | None
BlobStorePort.delete(blob_id: str) -> None
ChunkerPort.chunk(document_ir: DocumentIR,
                  context: ChunkingContext) -> ChunkingResult
EmbeddingRouterPort.route(request: EmbeddingRouteRequest)
    -> EmbeddingRouteDecision
```

`EmbeddingRouterPort` 当前只产生静态 `RouteDecision`，而
`EmbeddingFailoverRouter` 实际调用 Provider、管理 circuit/budget 并返回向量。
两者名称没有表达不兼容职责。

## 身份与指纹输入

- 当前 `document_version_id(content_sha256)` 等价于
  `deterministic_id("dver", content_sha256)`。
- v4 与 legacy IR Parser 都直接执行旧模式；节点 ID 绑定 dver、Part URI、
  structural path、node kind 和内容 SHA；Chunk ID 再绑定 dver、Chunker
  fingerprint、SourceSpan 与 citation SHA。因此相同字节的不同逻辑文档会
  发生 dver/node/chunk 冲突。
- `DocumentRef` 同时包含 project、knowledge base 和 document ID；当前创建层
  没有证明 document ID 全局唯一。
- `ParsingPolicy.metadata` 当前携带 project/kb/document ID，Parser 从中取身份。
- Factory 的 Index Fingerprint 使用新建的 `ParsingPolicy()`，而非 Profile
  resolved policy。Chunking 指纹读取实例 `chunker.policy`，Profile 本身没有
  `parsing`、`chunking` 字段。
- display name 不进入现有 dver/node/chunk 身份，但 runtime identity 会进入
  ParsingPolicy canonical JSON，导致业务身份污染策略指纹。

本阶段采用的唯一性语义是：`document_id` 在所有 project/knowledge base 中全局
唯一。`ParseContext.document` 同时校验 project、knowledge base 与 document ID，
P06 的 DocumentRef 创建层必须拒绝同一 document ID 绑定到不同 scope。

## Profile 到 Adapter Config 传播矩阵

| Profile 字段组 | 修改前实际传播 |
| --- | --- |
| Jina slot/provider/model/dimension | 已传播 |
| Jina api_key_env/task/embedding_type/normalization | 未传播；Adapter 使用默认或硬编码值 |
| Jina document/query egress | 已传播 |
| Jina role-specific request policy/revision | 只传播合并后的单一 hash |
| Qwen slot/provider/model/dimension | 已传播 |
| Qwen api/workspace/region env、transport、text type、query instruct、output type | 未传播；Adapter 使用默认或硬编码值 |
| Qwen document/query egress | 已传播 |
| Qwen role-specific request policy/revision | 只传播合并后的单一 hash |
| Jina Reranker model/egress | 已传播 |
| Jina Reranker api_key_env、候选/token policy、request revision | 未传播 |

公开字段可能改变 topology/fingerprint，却不能保证改变实际请求。配置模型虽为
`extra="forbid"`，但 Factory 未把这些字段传给模型。

## Blob 写入与回滚

- v4 和 legacy IR Parser 都可创建私有 `InMemoryBlobStore`，解析成功后直接写入
  source 与 media。
- `BlockParser.blob_writes` 按内容收集媒体写入，图片节点可复用 blob identity。
- `_commit_blobs()` 记录调用过 `put()` 的 ID，后续失败时全部 `delete()`。
  `put()` 没有返回 CREATED/EXISTING，因此预先存在的共享 Blob 也可能被删除。
- source blob 使用 `document:{dver}`，物理内容去重与逻辑文档版本身份混在一起。

## Chunk Report 修改前计算

| 指标 | 修改前算法 |
| --- | --- |
| source span coverage | 有 Chunk 固定 1.0，否则 0.0 |
| cross boundary violations | 模型默认 0，报告不计算 |
| duplicated citable chars | 直接等于 repeated context chars |
| table row/cell coverage | 仅看被引用节点的祖先集合 |
| list marker coverage | 仅计 DERIVED_NUMBERING 数量 |
| child group/note ref/orphan relations | 未完整验证或计算 |
| stable ID duplicates | 计算 `len(chunks)-len(ids)`，但正常 validator 会先拒绝 |
| slot token violations | 按每个 slot limit 计算 |

## 表格序列化修改前示例

逻辑三列 `A | <EMPTY> | C` 的中间 Cell 若没有可见 fragment，`_row_fragments()`
会 `continue`，最终 citation/embedding/lexical 上下文成为 `A | C`。列坐标只在
metadata 中存在，空列位置在检索文本中被压缩。连续 `tblHeader` Row 也未构造
稳定的多行表头路径，当前只把最近一个 header row 的 fragments 传给后续行。

## 本阶段接口计划

- 新增 `ParseContext(document: DocumentRef)`，ParserPort 的 context 成为必填参数。
- 删除 `ParsingPolicy.metadata`；Runtime Identity 只存在于 ParseContext。
- `document_version_id(document_id, content_sha256)` 绑定逻辑文档和内容版本。
- 新增 `ParsedArtifact`，`ParseResult.artifacts` 返回 source/media 字节；Parser 不再
  持有或调用 BlobStore。
- BlobStore 增加 `put_if_absent/read/exists/delete` 与 CREATED/EXISTING 结果；独立
  artifact transaction 只回滚 CREATED。
- RagProfile/RagComponents 保存 resolved parsing/chunking policy；ChunkingPolicy
  从实际 single/hot-standby topology 和 slot capability 上限派生。
- Provider Config 显式接收 Profile 的所有受支持字段，并分别保存 document/query
  request policy identity 与 adapter revision。
- 静态覆盖/出网/schema 决策命名为 Slot Eligibility；实际查询向量调用命名为
  Query Embedding Router，Composition Root 保存真实 router。
- 扩展 cache key、search key、Chunk validator/report 和表格逻辑列语义。

## 向后兼容策略

- 旧 dver 不能直接视为新 dver；当前尚未正式持久化，不执行 SQLite/Qdrant
  数据迁移。
- P00-P05 fixtures 全部显式构造 ParseContext；不保留 identity-in-policy 的隐式
  fallback，防止旧模式继续进入 canonical policy。
- Legacy parser/chunker 仍保留适配器和明确兼容测试，但使用新 Context/Artifact
  合同。
- Blob Store 可暂保留 `put/get` 兼容方法供既有非 Parser 调用迁移；新事务只使用
  `put_if_absent/read/exists/delete`。
- Search 完整接线属于 P07；本阶段只固定足够宽的 key 和真实 Router schema。
- P06 必须使用本阶段的新 Identity、Context、Artifact、Blob transaction 和
  Chunk report 合同，不得把旧 dver 当作同一持久化主键。

本审计不宣称检索质量提升，不宣称真实 Provider 可用，也不启动正式 P06
SQLite/Qdrant 数据迁移。

## 阶段分支验收补记

实现后的独立门禁为专项 3 passed、Core/Composition/Application/Adapters
250 passed；完整离线 check 为 1205 passed、75 deselected、4 warnings；smoke
为 62 passed、1 warning。compileall、Ruff、mypy 209 source files、Google
docstring 和 `git diff --check` 均通过。全部 Provider 行为测试使用 Fake 或
MockTransport，External services actually called: none。
