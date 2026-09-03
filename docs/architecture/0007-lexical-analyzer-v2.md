# 0007 Lexical Analyzer V2

## 决策

新增同步、无基础设施依赖的 `LexicalAnalyzerPort`，索引文档与 Query 必须使用
同一个 `deterministic-cjk-bigram` V2 实现。分析器执行 NFKC、casefold、ASCII
标识符保留、CJK 完整短语、bigram 与 unigram 派生。文档保留 token 频次；
Query 去重以限制 MATCH 表达式规模。

默认边界如下：

- 文档字段最多 100000 字符、20000 个 token；
- Query 最多 2048 字符、256 个唯一 token；
- 单个 CJK run 按最多 128 字符的重叠窗口处理；
- 不使用随机 hash、网络词典或外部分词服务；
- `citation_text` 和 canonical text 不被改写，空格只存在于派生 FTS 文本。

## FTS Schema

Migration 0008 新建 `chunks_fts_v2`。每行记录 analyzer identity，并存储
分析后的标题、层级、标识符和正文。新 Revision 的 `lexical_schema_json`
绑定以下字段：

```text
fts_schema_version=2
analyzer_id=deterministic-cjk-bigram
analyzer_version=2
query_builder_version=2
```

V2 Query Builder 对完整短语给出独立短语条件；同一 CJK group 内的 bigram
使用 AND，group 与标识符之间使用有界 OR。所有 token 都经过 FTS quote，
表名只从关闭的 Schema 白名单选择。

## 兼容策略

`fts_schema_version` 缺失或为 1 的旧 Revision 只路由到 `chunks_fts` legacy
reader。未知 Schema 或冒充 V2 的不完整 identity 返回 `REINDEX_REQUIRED`。
旧 Revision 和 0001—0005 migration 不原地修改；需要 V2 的知识库应构建并
激活新 Revision。
