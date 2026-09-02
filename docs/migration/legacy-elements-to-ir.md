# Legacy Element 与 Document IR 迁移

## 旧 Parser 到 IR

`LegacyDocxIrParser` 包装现有 `DocxParser.parse_with_audit()`，保留原安全限制，并使用
下列映射：

| 旧 Element | IR |
| --- | --- |
| HEADING | Heading root |
| PARAGRAPH，无 list_level | Paragraph root |
| PARAGRAPH，有 list_level | ListItem root + ListAttributes |
| TABLE | Table root + TableRepresentation child |
| IMAGE | Image root + BlobRef |

旧 `Locator.file_path` 只用作显示名，不进入 node ID。Locator 的逻辑序号进入
`SourceAnchor`。旧 audit 转成聚合 ParseIssue。每个旧表格同时设置
`metadata.legacy_flattened_table=true` 并报告 `LEGACY_TABLE_STRUCTURE_LOSS`。

旧图片字节先写 BlobStore；IR 不内嵌 `binary_data`。写入失败会清理本次已写 blob 并让
整个 parse 失败，不返回悬空引用。

## IR 到旧 Element

`document_ir_to_legacy_elements()` 只转换旧能力可表达的节点，并总是返回
`CompatibilityReport`：

- Heading、Paragraph、ListItem 还原为对应文本 Element；
- TableRepresentation 还原为扁平 TABLE，Table parent 不重复输出；
- Image 只有在 BlobStore 中摘要匹配时还原；
- Row、Cell、ContentControl、relationship、revision 和额外 metadata 无法表达时计入
  issue 或 skipped count。

基础 DOCX 回归比较 old parser elements 与 round-trip elements 的 kind、text、顺序和
`Locator.logical_key()`。复杂结构不会静默丢失。

## 迁移顺序

1. 宿主继续使用旧 Chunker 时，在 Parser 后调用 IR-to-Element adapter。
2. 新 Chunker 改为直接消费 Document IR 与 SourceAnchor。
3. 所有旧消费者迁移且有回归证据后，才能另阶段讨论删除兼容层。

P03 不迁移旧索引、SQLite 或 Qdrant collection，也不改变现有 HTTP/SDK schema。
