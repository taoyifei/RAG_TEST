# P01 Legacy 兼容层与删除条件

## 当前兼容方式

现有 `contracts.py`、DOCX Parser、Chunker、QdrantIndex、StateStore、QueryService、
RuntimeSettings、FastAPI 路由和公共响应 schema 均保持原位和原签名。新代码只增加 Core、
Ports、Composition 与 `adapters/legacy`，旧模块不反向 import Composition Root。

`legacy/contracts.py` 提供旧 Element/ChunkSourceSpan/Chunk 到新 DocumentNode/SourceSpan/
Chunk 的单向转换。文件展示路径和 binary data 不进入 Core，转换结果显式返回 warning；
旧 source/section/group/role 仅作为 typed JSON metadata 保留。`legacy/query.py` 将旧证据、
AnswerResult 和 QueryOutcome 投影到最小 Core 外壳，不改变旧调用方。

P01 的 LegacyDocxParserAdapter 继续调用现有安全 parser，并要求 ParsePolicy 显式提供
project/kb/document 逻辑 ID。LegacySectionChunkerAdapter 只为新嵌入示例提供保留精确
来源的一节点一 chunk 兼容行为；旧生产 Chunker 和旧索引链没有替换。

## 后续迁移

1. P02 增加真实 Jina/Qwen/Jina-reranker adapters，默认仍离线。
2. P03 让 DOCX ParserPort 正式输出完整 IR，并解决修订、批注、页眉页脚语义。
3. P05 以新 IndexRevision 构建双 named-vector 和 FTS5，完整后原子激活；不覆盖旧索引。
4. P06 迁移 Query/Evidence/Answer 用例，公共 API 兼容测试继续保留。

只有全部公共调用方、数据迁移、回滚路径和兼容窗口均有自动证据后，才能提出删除转换层。
删除 legacy/Industry 代码属于单独决策门，P01 不执行。
