# DOCX 解析安全边界

## 默认限制

| 限制 | 默认值 |
|---|---:|
| 文件大小 | 128 MiB |
| 总解压量 | 512 MiB |
| 单条目解压量 | 64 MiB |
| ZIP 条目数 | 10,000 |
| 单条目压缩比 | 200 |
| 总解析时间 | 30 秒 |
| XML 深度 | 256 |
| XML 节点数 | 1,000,000 |
| 嵌套表格深度 | 32 |
| 嵌套字段深度 | 32 |

Profile 可以显式调小限制。best_effort 不会放宽任何资源或安全限制。

## 在正文前拒绝的输入

- 扩展名不是 `.docx`，包括 `.docm`、`.dotm` 和伪装 ZIP；
- ZIP 绝对路径、`..`、反斜线逃逸、重复条目或加密条目；
- 条目数、单项、总解压量、压缩比或 monotonic timeout 超限；
- 缺少 `[Content_Types].xml`、`_rels/.rels` 或唯一主文档关系；
- content type 与主文档不匹配、内部 relationship 指向缺失 Part 或逃逸 package；
- 宏相关 content type 或 relationship；
- XML 语法、深度或节点数超限。

XML parser 固定使用 `load_dtd=False`、`resolve_entities=False`、`no_network=True`、
`recover=False` 和 `huge_tree=False`。Parser 不执行宏、字段、DDE、OLE、altChunk、HTML
转换或外部对象。

## 外部关系与敏感信息

外部 hyperlink 或图片默认只记录关系类型和 URI scheme，不保存 host、path、query 或
fragment，也不建立网络连接。`external_relationships=reject` 可让文档直接失败。完整正文、
批注正文、作者、URL、API Key、绝对路径和二进制不会进入 ParseReport、异常 details 或
默认 inspect 输出。

`scripts/dev.py inspect-document` 默认只打印输入摘要前缀、Parser 身份、节点/issue 数、
story 计数和 coverage。只有显式 `--include-content` 才会把正文写入明确指定的 JSON 或
标准输出。

## Blob 与外部服务

源 DOCX 和媒体按 SHA-256 写入宿主 BlobStore。相同媒体只写一份 Blob，每个显示实例仍有
独立 ImageNode。写入中途失败会删除本次已经写入的 Blob。Parser 不读取 Provider Key、
模型名、维度或 query instruction；offline、Jina-only 与 Jina/Qwen hot-standby Profile
的解析结果必须相同。
