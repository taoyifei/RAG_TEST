# 湾事通开发进度

## 基线

- Base SHA：`4f71f7db03990cf49cde0664bec68d6c25dfd544`。
- Base ref：`origin/feature/universal-rag`。
- 集成分支：`feature/wanshitong`。
- 当前阶段分支：`codex/wb-00-bootstrap`。
- Industry 只读参考 SHA：`5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。

## 当前阶段

WB-00：分支基线、架构合同与轻量门禁。目标是建立湾事通独立开发基线，不改变现有
RAG 行为。阶段分支只新增湾事通设计与进度文档，不合入 `feature/wanshitong`。

## 已确认需求

- 公共 `/` 与管理员 `/admin` 双入口。
- Project/KB 由服务端固定，并在公共界面隐藏。
- 公共会话无需登录；Query/Admin/模型/OCR 等长期 Token 不进入浏览器。
- History 加密保存 7 天。
- 只使用一个知识库，以文档元数据过滤业务分类。
- 第一版不展示快捷入口。
- 使用 Industry 参考的模型 Endpoint；Embedding、Reranker、LLM 各自最高 4 并发。
- 查询接纳为 `4 active + 8 queue`。
- Demo 使用内网 HTTP 主机端口 `8188`。
- Industry OCR 是 PP-OCRv5 `POST /v1/ocr`，不是 PaddleOCR-VL
  `POST /layout-parsing`。
- 当前不部署新 OCR；未配置 PaddleOCR-VL 时必须明确禁用 PDF 上传。
- Demo 采用阶段定向的轻量门禁，不运行全量回归与完整发布验收。

## 后续阶段

后续按 [phase-plan.md](../wanshitong/phase-plan.md) 分为服务端范围与容量、公共会话与
History、双入口界面、内网部署与 Demo 验收。每一阶段需要独立 Prompt、定向测试和
验证，不因 WB-00 文档记录而视为已授权或已完成。

## 明确未完成项

- 尚未实现湾事通专用 Runtime 或运行时配置。
- 尚未修改或验收 API、数据库、前端、Docker 与依赖。
- 尚未实现固定 Project/KB、元数据过滤、无登录会话或 Token 服务端托管的湾事通接线。
- 尚未实现或验收 7 天加密 History 的湾事通数据流与到期清理。
- 尚未接入或探测 Industry 模型 Endpoint，未验证模型质量、4 并发或 `4 + 8` 排队。
- 尚未部署 PaddleOCR-VL，也未实现/验收 PDF 上传禁用交互。
- 尚未执行 build、smoke、真实 Provider、OCR、Qdrant、浏览器或部署验收。
- 阶段分支尚待独立 Verification Prompt 验证；不得自行合入 `feature/wanshitong`。

本文刻意保持简短，不复制或改写现有 Universal `PROGRESS.md` 历史。
