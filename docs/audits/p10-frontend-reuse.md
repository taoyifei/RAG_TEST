# P10 前端复用审计

审计基线为 `feature/universal-rag@61587e2d0f41eef0a59f69a4686ec4531f0cc50d`，
日期为 2026-09-04。审计只读取当前分支、`main`、`Industry`、P09 OpenAPI
快照和 P09 API/Application 代码；没有调用远程 Provider。

## 现有入口与技术栈

- 当前仓库只有 `frontend/` 静态 HTML、CSS 和原生 JavaScript，没有
  `package.json`、lockfile、React、Vite 或 TypeScript。
- `frontend/index.html` 调用旧 `/api/chat`、会话与反馈接口；
  `frontend/debug.html` 调用旧 `/api/admin/traces` 系列接口。两者不使用
  P09 `/api/v1` 生命周期、检索或系统状态合同。
- `main` 的前端文件与当前分支相同。`Industry` 只额外包含 Bearer 变体，
  没有可直接移植的 React 组件或 P09 typed client。
- P09 `docs/public/openapi-v1.json` 冻结了 21 个 path，包含 Project、Knowledge
  Base、Document/Version、Artifact、Job、Search/Answer、System/Provider 和安全
  Retrieval Diagnostics。

## 可复用内容

- 旧回答页的中文拒答文案、最终事件覆盖临时流状态、Trace ID 可复制、
  `prefers-reduced-motion` 与 `aria-live` 处理可作为行为参考。
- 旧调试页使用 `textContent` 渲染外部数据、不执行任意 HTML，并把管理令牌
  限制在 `sessionStorage`；这些安全原则继续保留，但新控制台默认只保存在内存。
- 旧 CSS 的可见 focus、长 ID 换行、表格横向容器和浅色企业界面可以作为
  视觉基线，不复制具体选择器或等权卡片布局。
- P09 的 `/api/v1`、统一 Error Envelope、SSE `meta/retrieval/final`、
  Idempotency-Key、scope 授权和 Query-aware Evidence 是新 UI 的唯一事实源。

## 必须重写的调用

- Project、Knowledge Base、Document、DocumentVersion、Job 和 Artifact 全部改用
  `/api/v1`，不再从旧会话接口推断生命周期状态。
- Search/Answer 改用 scope 绑定的 `:search`、`:answer`；最终 Evidence、route、
  selected slot、RRF/rerank、cache 和 degraded 状态直接读取响应或管理员
  Diagnostics，前端不得重新排序。
- Provider、质量、FTS、GC 和 reconciliation 状态只读取
  `/api/v1/system/components`；Probe 必须显式确认网络与预算。
- API 类型由冻结 OpenAPI 自动生成并以检查命令防止漂移；所有写操作集中生成并
  复用 Idempotency-Key，检索与流式问答请求支持 AbortController。

## 不应复制的旧逻辑

- 不复制旧 `/api/chat` 的 conversation、feedback 或旧 Trace artifact 导出流程，
  它们不是 P10 控制台的稳定生命周期合同。
- 不复制旧调试页在浏览器内根据候选阶段推断 `retrieval/rerank/assembly loss` 的
  逻辑；P10 只展示服务端 RetrievalDiagnostics。
- 不复制以“请求是否报错”猜测 primary/standby、Configured 即 Healthy、或把全部
  retrieval candidates 当作 citations 的行为。
- 不持久化 Bearer Token 到 localStorage，不记录完整 query、正文、Token 或 Secret。

## API 缺口与最小扩展

P09 已有权威持久数据，但冻结 HTTP 面没有 Job 列表、Revision 检查和 Chunk 浏览
路由。P10 将只增加向后兼容、管理员授权、scope 绑定的只读 `/api/v1` 读模型：

- Job 分页列表；
- Revision 状态、实际 document/chunk/FTS/slot coverage、Writer 与 validation 摘要；
- canonical Chunk V3 分页与固定字段过滤；
- 每个 Revision 的 ParseReport/ChunkingReport 摘要。

这些路由只调用 Application Service 和 Core Port，不把 SQLite/Qdrant 类型泄漏到
API，不改变 P09 已冻结路径、身份、检索、Evidence 或错误语义。

## 唯一正式入口

`frontend/` 原静态入口将被 React/Vite/TypeScript 控制台原位替换。生产只提交一次
构建使用的源码和 lockfile，由同一个 FastAPI P10 host 提供静态 assets 与 SPA
fallback；旧 HTML/JS/CSS 不作为第二套正式 UI 保留。开发使用 Vite proxy，生产与
离线 E2E 使用同一 FastAPI 静态入口。
