# 湾事通 Demo 架构合同

## 文档状态

本文定义湾事通 Demo 的目标合同，不表示这些能力已在 WB-00 实现或完成运行验收。
WB-00 只建立分支、设计资料和轻量门禁，不修改现有 RAG 行为。

固定基线如下：

- Universal 基线：`feature/universal-rag@4f71f7db03990cf49cde0664bec68d6c25dfd544`。
- 湾事通集成分支：`feature/wanshitong`，从上述 SHA 创建。
- Industry 只读参考：`Industry@5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。

Industry 仅用于核对接口和部署参数，禁止整体 merge、rebase 或 cherry-pick。

## 入口与访问边界

- 公共入口固定为 `/`，供无需登录的内网用户发起问答和查看自己的会话历史。
- 管理员入口固定为 `/admin`，管理能力与公共问答界面分离。
- Project 与 Knowledge Base 由服务端固定绑定。公共界面不展示、不允许选择，也不接受
  浏览器提交的 Project/KB 覆盖值。
- 公共会话不要求用户账号登录。浏览器只持有同源、短期、不可解释的会话状态；
  Query/Admin Token、模型 Token、OCR Token 及其他长期凭据均只保存在服务端，不写入
  HTML、JavaScript、URL、浏览器存储或前端可读配置。
- 第一版不展示快捷入口，避免把尚未验收的预设问题或导航能力带入 Demo。

## 数据与检索边界

- Demo 只使用一个知识库，不创建多知识库切换体验。
- 文档业务分类通过文档元数据表达，检索时由服务端施加元数据过滤；浏览器不能通过
  自行传入 Project/KB 标识绕过固定范围。
- History 中的问题、答案及必要会话内容必须加密保存，保留期固定为 7 天；到期数据由
  后续实现负责清理。加密密钥不得进入浏览器。
- 7 天 History 是产品数据合同，不等同于永久审计日志，也不授权在 WB-00 修改数据库。

## 模型服务与容量

湾事通沿用 Industry 固定参考 SHA 中记录的内网模型 Endpoint：

| 角色 | Endpoint |
|---|---|
| Embedding | `http://10.242.180.60:8091` |
| Reranker | `http://10.242.180.60:8092` |
| LLM 1 | `http://10.242.180.57:8000` |
| LLM 2 | `http://10.242.180.57:8001` |
| LLM 3 | `http://10.242.180.58:8000` |
| LLM 4 | `http://10.242.180.58:8001` |

这些地址不追加 `/v1`。Endpoint 是后续部署合同，本阶段不探测、不调用，也不把它们
描述为当前可用。

- Embedding、Reranker、LLM 各自的模型调用并发上限为 `4`。
- 查询执行容量固定为 `4 active + 8 queue`，总接纳容量为 12；排队和超限响应由后续
  阶段按现有错误合同实现并定向验证。
- Demo 通过内网 HTTP 暴露在主机端口 `8188`。这是内网 Demo 约束，不构成公网或生产
  TLS 安全结论。

## OCR 与 PDF

OCR 能力边界以 [ocr-capability.md](ocr-capability.md) 为准。简要合同是：Industry OCR
为 PP-OCRv5 的 `POST /v1/ocr`，不是 PaddleOCR-VL 的 `POST /layout-parsing`；本阶段
不部署新 OCR。未配置可用 PaddleOCR-VL 时，PDF 上传必须明确禁用，不得用 Industry
OCR、文本抽取或虚假结果伪装成 PDF 文档解析。

## WB-00 变更边界

本阶段不修改 Runtime、API、数据库、前端、Docker 或依赖，不创建模型/OCR 替身，
也不改变任何现有路由、持久化或部署行为。后续实现必须由独立阶段授权，并保持以上
合同可定向验证。
