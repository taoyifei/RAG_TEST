# P01 Core 类型与迁移映射

## 目的与边界

P01 在 `rag_app` 内增加格式中立 Core、窄 Ports 和显式 Composition Root。现有
FastAPI、DOCX、Qdrant、HTTP Provider、`RuntimeSettings`、`QueryService` 与数据库
schema 保持原位和原签名。本阶段采用单向 legacy adapter 逐步迁移，不移动、删除或让
旧模块反向依赖新 composition 包。

## 旧类型到新 Core 类型

| 现有类型或职责 | P01 Core 类型 | P01 迁移方式 | 后续阶段 |
|---|---|---|---|
| `contracts.Locator` | `core.models.SourceSpan` 的结构位置和 typed metadata | legacy adapter 显式转换；文件展示名只作 metadata，不进入稳定 ID | P03 完善格式特有定位 |
| `contracts.Element` | `core.models.DocumentNode` | 单向转换并返回丢失字段 warning；binary data 不进入 Core | P03 由 DOCX ParserPort 正式输出 IR |
| `contracts.ChunkSourceSpan` | `core.models.SourceSpan` | 保留字符范围、元素 ID 和结构路径 | P03/P05 扩展跨格式来源语义 |
| `contracts.Chunk` | `core.models.Chunk` | 单向转换；旧 `source_id/doc_version/pipeline_fingerprint` 显式映射 | P05 迁移索引写入 |
| `contracts.Parser` | `core.ports.ParserPort` | `legacy-docx` adapter 包装，不改变旧 parser 签名 | P03 完成 ParseResult 与策略适配 |
| `chunking.Chunker` | `core.ports.ChunkerPort` | `legacy-section-pack` adapter 包装，不移动旧实现 | P05 迁移完整 chunk 用例 |
| `clients.model_services.TeiEmbeddingClient` | `core.ports.EmbeddingPort` | P01 仅注册离线/占位构造边界，不调用 HTTP | P02 实现 Jina 与阿里 adapter |
| `clients.model_services.RerankerClient` | `core.ports.RerankerPort` | P01 保留旧链；新默认使用离线 lexical overlap | P02 实现 Jina reranker adapter |
| `index.qdrant.QdrantIndex` | `VectorStorePort` | legacy adapter 仅定义显式转换边界，不改 collection/schema | P05 named vectors 与 revision 迁移 |
| `state.StateStore` 及子 store | `MetadataStorePort` | legacy adapter 包装窄元数据能力，不改 SQLite schema | 后续按用例逐项迁移 |
| `retrieval.fusion.FusedHit` | `core.models.SearchHit` | 单向转换，去除基础设施对象并保留 channel/rank metadata | P05 迁移检索编排 |
| `generation.evidence.EvidenceItem` | `core.models.EvidenceItem` | 单向转换，正文只在显式结果字段中存在且 repr 隐藏 | P06 迁移 evidence packing |
| `generation.answer.AnswerResult` | `core.models.AnswerResult` | P01 只提供公共外壳，旧 API 继续返回旧类型 | P06 迁移生成与引用协议 |
| `query_service.QueryService` | `application.RagEngine` | P01 只负责装配、组件信息、健康与关闭；未迁移能力 fail closed | 后续逐用例替换 |
| `runtime.RuntimeBundle` | `composition.RagComponents` | 新对象不含 FastAPI/Qdrant/httpx 具体类型；资源由 ExitStack 管理 | API/CLI 后续改由 factory 构造 |
| `settings.RuntimeSettings` | `composition.Profile` + `core.EgressPolicy` | 旧设置继续有效；新 Profile 使用严格 JSON，无新 YAML 依赖 | 后续提供显式兼容映射 |

## 身份与指纹迁移

- 旧 `src_` 身份只在 legacy 转换中保留；新逻辑身份使用 `prj_`、`kb_`、`doc_`、
  `dver_`、`node_`、`chunk_`、`irev_`、`job_` 和 `trace_`。
- 新确定性 ID 只依赖逻辑身份、内容摘要、结构路径和规范化策略；显示名、绝对路径、
  数据库自增值和 Python `hash()` 均不参与。
- `IndexFingerprintInput` 覆盖解析、IR、分块、token counter、两个 document embedding
  slot、词法/vector/payload schema。
- `ServingFingerprintInput` 覆盖 query policy、router/circuit、检索/fusion、rerank、
  evidence、confidence、generator/citation；切换 reranker 只改变 serving fingerprint。

## Jina/Qwen3.7 主备边界

P01 建模 `primary` Jina 和 `standby` 阿里 Qwen3.7 两个独立 slot。即使维度同为 1024，
它们也只能分别写入和查询 `dense_primary` 与 `dense_standby`。Profile、EmbeddingRequest、
EmbeddingResult 和 VectorSearchRequest 都携带显式 `slot_id`；Store 不推断默认向量。
P01 只验证配置、能力与隔离，不发网络请求、不重建索引。

## 兼容顺序

1. P01 新增 Core/Ports/Composition 与只读、单向 legacy adapter，旧链不变。
2. P02 增加真实 Provider adapters，但默认离线且受独立 EgressPolicy 授权。
3. P03/P05 分别迁移 Document IR/Parser 和索引/检索，用 revision 原子激活。
4. 后续全部公共用例迁移且兼容窗口结束后，才讨论删除 legacy 转换层；删除属于单独审计。

## P01 验收保护

- AST 测试阻止 Core/Application 导入基础设施、adapter、API 或 composition。
- Registry 仅接受安全小写名字并由代码显式注册，不扫描模块、目录或 entry point。
- Profile 严格拒绝未知字段并脱敏导出；secret 只保存环境变量名。
- Factory 部分失败时只关闭已创建资源且每个资源最多关闭一次。
- 专项测试证明 schema round-trip、跨进程 hash、双 slot 隔离、恶意名称拒绝和旧链回归。
