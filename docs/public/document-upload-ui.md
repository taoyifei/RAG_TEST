# 文档上传 UI 语义

控制台把三类操作明确分开：

1. “新建文档”调用 `POST .../documents`。即使 Word 文档字节相同，也创建新的逻辑
   `document_id`；相同内容允许复用同一个 Source Artifact。
2. “创建新版本”调用 `POST .../documents/{document_id}/versions`，保留
   `document_id`。新内容产生不可变 `document_version_id` 和新的 IndexRevision。
3. “重命名”只调用 Document PATCH。界面明确提示不创建 dver、不重建索引，并在保存
   后用 GET 回读。

上传只发送浏览器选择的 DOC/DOCX 字节和 basename，不发送本地路径。扩展名、
`Content-Type` 与文件签名必须一致，改名伪装的文件会在正文处理前拒绝。每次写操作
生成 Idempotency-Key；客户端不自动重放上传。413、422、409 等错误使用统一 Error
Envelope 显示 code、stage、retryable 与 trace id 边界。

文档详情按版本展示 Content SHA、Source Artifact、大小、状态和时间。正文不在列表中
默认展开。删除需要二次确认，UI 等待服务端返回状态，不做乐观物理删除。

