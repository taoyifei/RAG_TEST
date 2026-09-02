# Document Model V1

## 公共对象

宿主通过 `ParserPort.parse(ParseSource, ParsingPolicy)` 同步获得 `ParseResult`。

- `DocumentSource` 保存逻辑文档/版本、显示名、真实检测后的媒体类型、摘要、大小和
  BlobRef；显示名与扩展名不是格式信任依据。
- `DocumentIR` 保存根节点 ID、扁平节点、显式关系和 ParseReport。
- `DocumentNode` 使用 `NodeKind`、`SourceAnchor` 和可选文本、列表、网格、图片、修订
  属性。IR JSON 不包含 DOCX XML 节点、bytes 或 `Path`。
- `ParseIssue` 只记录 code、severity、action、anchor、count 和安全消息，不记录原文。
- `ParseReport.coverage` 是 represented visible text nodes 除以 visible text nodes。

Schema version 固定为 `"1"`。字段语义改变时需要新版本与迁移函数，不原地复用含义。

## 内置 Parser

显式 Registry 名称为 `legacy-docx-ir`。它检查 ZIP/package、`[Content_Types].xml` 和
Word 主文档 content type，不信任文件扩展名，也不接受 URL。

`ParserCapabilities` 对 tables、images、numbering 返回 `partial`，对 headers/footers、
footnotes、revisions、comments 和 text boxes 返回 false。完整 OOXML 结构属于 P04 的
`docx-ooxml-v4`，P03 不注册该名称。

## CLI

```bash
python scripts/dev.py inspect-document <path> [--profile profile.json]
python scripts/dev.py inspect-document <path> --output-json /tmp/ir.json
python scripts/dev.py inspect-document <path> --output-json /tmp/ir.json \
  --include-content
```

默认标准输出只有 hash prefix、parser ID/version、节点/问题数、story counts、覆盖率和
elapsed。没有 `--output-json` 就不写文件；没有 `--include-content` 就不写正文。

## 当前限制

P03 不修改生产索引、Qdrant schema 或 Query API。扁平旧表格没有 cell provenance；
修订/批注/story 的完整正文语义在 P04 前不会被伪装成已支持。`ready_with_warnings` 状态在
P09 落地，本阶段只提供可用于该状态判断的 ParseReport。
