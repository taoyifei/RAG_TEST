# 湾事通 Demo 边界

## Demo 新增内容

湾事通只在 Universal Product Runtime 之外增加：

- 服务端固定并向公共用户隐藏的 Project/KB Scope；
- 无登录公共问答 Facade 与新的公共前端壳层；
- 对现有管理员页面的收敛；
- 单知识库上的文档分类元数据；
- 内网 HTTP `8188` 部署 overlay；
- 暂不展示的快捷入口扩展点。

这些是后续阶段目标；WB-00 本身没有实现上述运行行为。

## Universal 当前能力

解析、Document IR、Chunk、Index Revision、Qdrant、Exact/FTS/Dense、RRF、Reranker、
Evidence、Grounded LLM、引用校验、会话、7 天加密 History、Operational Trace、反馈、
缓存、Singleflight、备份与恢复均由 Universal 当前实现提供。

湾事通不得将其中任何一项降级、旁路、复制或改写成“后续再做”。

## WB-00 允许范围

- 固定并核对 Git 基线与祖先；
- 新增 `docs/wanshitong/` 中的五份合同；
- 新增 `scripts/check_wanshitong_universal_first.py`；
- 运行脚本、`scripts/dev.py doctor` 和 Git diff 轻量门禁；
- 提交并推送 `codex/wb-00-universal-baseline`。

## WB-00 禁止范围

- 修改 Runtime、API、数据库、前端、Docker 或依赖；
- merge/rebase/subtree merge/batch cherry-pick Industry；
- 新增 Provider/RAG/History/Trace/Qdrant 的第二实现；
- 删除、大面积重命名或用 Industry 内容覆盖 Universal Core；
- 删除、skip、xfail、noqa 或弱化既有回归；
- 运行全仓 pytest/mypy、全部 Playwright、完整 release acceptance、长时压测、fuzz、
  SAST 或渗透测试。

## 证据边界

WB-00 的 PASS 只证明固定基线、文档和机械保护合同成立。它不证明公共入口、固定
Scope、内网 Provider、PDF/OCR、部署端口或 UI 已可运行，也不提供模型质量、生产安全
或真实服务器状态证据。
