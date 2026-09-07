# Word 文档解析安全边界

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

Profile 可以显式调小限制。best_effort 不会放宽任何资源或安全限制。DOCX 路径使用
全部 ZIP/XML 限制；旧版 DOC 路径使用文件大小、输出大小和总解析时间限制。

## DOCX 路径在正文前拒绝的输入

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

## 旧版 DOC 路径

- 只接受 `.doc` 扩展名、OLE Compound File 签名与 `application/msword` 或
  `application/octet-stream`；扩展名、MIME 与签名不一致时先拒绝；
- Runtime 镜像固定安装 `antiword 0.37-17`。应用以非 root 用户通过固定参数调用，
  不经过 shell，不解释宏；输入只写入 0400 私有临时文件；
- 子进程地址空间限制为 256 MiB，输出文件上限取 ParsingPolicy 的单条目与总输出
  上限较小值，超时沿用 ParsingPolicy，超时后终止整个子进程组；
- stdout 只写私有临时文件，stderr 丢弃，失败仅返回稳定安全错误；Parser 本身不读取
  Provider 凭据，也不发起 Provider 请求；
- 转换器与应用共享容器网络命名空间。安全边界依赖固定本地可执行文件、固定参数和
  资源限制，不把它描述成独立无网络沙箱。

旧版 DOC 只生成正文纯文本段落。表格、图片、页眉页脚、脚注、编号、批注、修订和
文本框不保证结构化保留；ParseReport 固定记录
`LEGACY_DOC_FLATTENED_TEXT`。后续是否将切片发送到远程 Provider 仍由知识库 Profile、
显式授权和预算门控制。

## 外部关系与敏感信息

外部 hyperlink 或图片默认只记录关系类型和 URI scheme，不保存 host、path、query 或
fragment，也不建立网络连接。`external_relationships=reject` 可让文档直接失败。完整正文、
批注正文、作者、URL、API Key、绝对路径和二进制不会进入 ParseReport、异常 details 或
默认 inspect 输出。

`scripts/dev.py inspect-document` 默认只打印输入摘要前缀、Parser 身份、节点/issue 数、
story 计数和 coverage。只有显式 `--include-content` 才会把正文写入明确指定的 JSON 或
标准输出。

## Blob 与外部服务

源 DOC/DOCX 和媒体按 SHA-256 写入宿主 BlobStore。相同媒体只写一份 Blob，每个显示实例仍有
独立 ImageNode。写入中途失败会删除本次已经写入的 Blob。Parser 不读取 Provider Key、
模型名、维度或 query instruction；offline、Jina-only 与 Jina/Qwen hot-standby Profile
的解析结果必须相同。
