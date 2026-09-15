# 湾事通 OCR 能力边界

## 两类接口不是同一能力

| 能力 | Industry 只读参考 | 湾事通 PDF 所需能力 |
|---|---|---|
| 引擎 | PaddleOCR 3.5.0，PP-OCRv5 server det/rec | PaddleOCR-VL 完整文档解析产线 |
| HTTP 路径 | `POST /v1/ocr` | `POST /layout-parsing` |
| 主要输入 | 图片媒体 | PDF 文档及其物理页 |
| 主要输出 | 文本行、置信度与可用框信息 | 页面与版面结构化解析结果 |
| 能否互相替代 | 否 | 否 |

Industry 的 `src/rag_app/ocr/client.py` 和 `service.py` 在固定参考 SHA
`5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` 中使用 `/v1/ocr`。该服务是
PP-OCRv5 图片文字识别，不是 PaddleOCR-VL `/layout-parsing`，不能据此宣称 PDF
结构化解析已可用。

## 当前 Demo 决策

- WB-00 不部署、不构建、不配置任何新 OCR 服务。
- 不把 Industry OCR merge 或复制到湾事通分支。
- 不创建假 OCR、固定响应或静默降级来模拟 PDF 成功。
- 只有后续阶段明确配置并验证 PaddleOCR-VL 文档解析能力后，才能开放 PDF 上传。
- 未配置 PaddleOCR-VL 时，PDF 上传入口必须明确禁用；对应服务端路径也应失败关闭，
  返回可理解的“不支持/未配置”结果，而不是接受后在后台失败。
- 不允许用 PP-OCRv5 `/v1/ocr`、`pypdf.extract_text()` 或其他纯文本抽取冒充
  `/layout-parsing`。本地 PDF 预检可以检查格式、损坏、加密和页数，但不等于正文解析。

## WB-00 验证边界

本阶段仅人工核对固定 SHA 中的接口路径与本文措辞，没有启动 OCR 容器、加载模型、
上传 PDF、发送真实 OCR 请求或验证 GPU。OCR 在线性、识别准确率、版面完整性和资源
容量均未验证。
