# Phase 10.5 进度与 P11 进入证据

## 已实现

- 默认 CLI、FastAPI lifespan 与 React 已接入唯一 Product Runtime。
- AES-256-GCM 页面托管和环境变量托管 Credential 已接入。
- 固定 Provider Catalog、Connection、五项 Mock 验证和验证历史已持久化。
- 知识库级 Retrieval Profile Revision、双 fingerprint 和三态影响预览已接入。
- HttpOnly 管理员 Session、CSRF、TTL、轮换、退出与限速已接入。
- 一次显示、keyed HMAC、作用域、过期与吊销的 API Token 已接入。
- 前端已拆分、中文化，并加入 onboarding、模型服务、检索方案和接口访问页面。
- Compatibility Manifest 取代普通启动时的多重 Git revision 完全对齐要求。
- 新增 product-check、product-smoke、安全测试和完整离线浏览器 E2E。

## 证据边界

所有 Provider 请求使用 `httpx.MockTransport` 和公开合成文本。Jina、阿里百炼、远程
Qdrant 与公网模型实际调用数为 0。Mock 验证证明配置、认证头、响应合同、错误分类、
持久化和 UI 流程，不证明远程质量、配额、网络、校准或生产容量。

## P11 门状态

除 `REMOTE_PRODUCTION_PROFILE_READY=false` 外，P10.5 的产品、Secret、Session、
Token、Profile、影响预览、持久验证、中文前端、最小部署合同、兼容检查和离线 E2E
进入项均为 true。`P11_ENTRY_READY=true` 表示可以开始受控真实 Provider 接入，不表示
远程生产 Profile 已就绪。
