# 湾事通 Demo 边界

## Demo 目标边界

湾事通是快速内网 Demo，不是生产发布。目标形态固定为：

- 公共 `/` 与管理员 `/admin` 双入口；
- 服务端固定且在公共界面隐藏 Project/KB；
- 公共用户无需登录，但所有长期 Token 留在服务端；
- 一个知识库，通过文档元数据过滤业务分类；
- 加密 History 保留 7 天；
- 第一版不展示快捷入口；
- 复用 Industry 内网模型 Endpoint，模型调用最高并发为 4；
- 查询接纳为 `4 active + 8 queue`；
- 以内网 HTTP 主机端口 `8188` 提供服务。

具体模型地址见 [architecture.md](architecture.md)，OCR 分界见
[ocr-capability.md](ocr-capability.md)。

## WB-00 允许范围

WB-00 只允许：

1. 从固定 Universal SHA 建立 `feature/wanshitong` 与
   `codex/wb-00-bootstrap` 分支；
2. 新增 `docs/wanshitong/` 下的设计资料；
3. 新增简短的 `docs/progress/wanshitong.md`；
4. 执行并记录 [lightweight-gates.md](lightweight-gates.md) 中的轻量门禁；
5. 提交并推送阶段分支，等待独立 Verification Prompt。

## WB-00 禁止范围

本阶段不得修改或新增以下实现：

- Runtime 与运行时配置逻辑；
- HTTP API、请求/响应 schema 或路由；
- 数据库 schema、迁移、存储实现或已有数据；
- 前端页面、样式、资源或构建产物；
- Dockerfile、Compose、部署脚本或镜像；
- Python、Node 或系统依赖；
- 模型 Provider、模型代理或 OCR 实现。

不得整体 merge、rebase 或 cherry-pick `Industry`，不得把 Industry 专用实现复制成未经
验证的湾事通运行时。

## 安全与能力边界

- 内网 HTTP `8188` 只适用于本 Demo 环境；没有验证公网暴露、TLS、生产鉴权或高可用。
- “无登录”不等于“无边界”。服务端仍须固定数据范围、隔离公共会话并保护 Token。
- 未配置并验证 PaddleOCR-VL 时必须禁用 PDF 上传；Industry PP-OCRv5 `/v1/ocr`
  不能替代 `/layout-parsing`。
- WB-00 不调用真实模型、OCR 或 Qdrant，不验证 Endpoint 在线性、模型质量、吞吐、队列
  时延或 7 天清理作业。
- 轻量门禁只证明分支和文档合同自洽，不证明 Demo 已可运行或具备生产就绪性。
