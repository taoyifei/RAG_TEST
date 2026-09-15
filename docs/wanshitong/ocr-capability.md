# 湾事通 OCR 与 PDF 能力边界

## 已有能力归属

PDF 解析继续使用 Universal 当前 `ProductPdfParsing` 及其 Connection 合同。Universal
基线支持完整 PaddleOCR-VL 文档解析的 `POST /layout-parsing` 或官方 SDK 路径，并将
结果接回既有 Document IR、Chunk、索引、Evidence 与引用链路。

湾事通不得建立第二套 PDF parser、OCR 状态库、索引身份或降级正文链路。

## Industry 参考现状

固定 Industry SHA 中的 OCR 是 PaddleOCR 3.5.0 PP-OCRv5 server det/rec，客户端和
服务端使用 `POST /v1/ocr`。它面向图片文字行识别，不是 PaddleOCR-VL
`POST /layout-parsing` 的 PDF 版面解析能力。

因此：

- 不得把 `/v1/ocr` 改名、包装或描述为 `/layout-parsing`；
- 不得把 PP-OCRv5 文本行结果伪装成 PDF 页面与版面结构；
- 不得为了复用 Industry OCR 而替换 Universal `ProductPdfParsing`；
- Industry OCR 只作为当前服务器能力和部署经验参考。

## Demo 决策

- WB-00 不部署、构建、配置或调用新 OCR。
- 后续湾事通配置只有在现有 Universal PaddleOCR-VL Connection 可用并完成定向验证后，
  才能开放 PDF 上传。
- 未配置 PaddleOCR-VL 时，公共与管理员界面必须明确禁用 PDF 上传；对应服务端入口也
  应失败关闭并返回可理解的未配置结果。
- `pypdf.extract_text()` 或其他文本抽取只能用于既有安全预检合同，不能成为第二套
  PDF 正文来源。

## WB-00 未验证项

本阶段没有启动 OCR 容器、加载模型、上传 PDF、访问 GPU 或发送真实请求。OCR
在线性、准确率、版面完整性、资源占用和服务器可用性均未验证。
