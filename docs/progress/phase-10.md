# Phase 10 Progress: React Console and Offline Browser E2E

## 集成身份

- Integration base SHA: `61587e2d0f41eef0a59f69a4686ec4531f0cc50d`。
- Feature branch: `codex/p10-web-ui`。
- Integration branch: `feature/universal-rag`。
- Feature commits:
  `108a730af5507d463788a21d620c39d4c16698a2`、
  `0c86bb1fc1591640fdec4c44ff3b0fbab9005020`、
  `b5ea9859dd22b8c30f5a05533d63032e676a6199`。
- Integration merge commit:
  `6ffbb3880bce0459661ef486a937d24aeffc566c`（`--no-ff`）。
- Remote feature SHA 已用 `git ls-remote` 核验为
  `b5ea9859dd22b8c30f5a05533d63032e676a6199`；远程集成 tip 在本证据提交
  推送后由最终交接给出。
- `main` 与 `Industry` 保持只读。

## 前端与入口

- 使用 React 19、TypeScript strict、Vite 8、Vitest 5、Playwright 1.62、
  Testing Library、axe-core 和轻量 Lucide 图标；依赖锁定于
  `frontend/package-lock.json`。
- P10 FastAPI host 同时提供 `/api/v1`、静态 `assets` 和 SPA fallback。
  旧 `app.js`、Debug HTML/CSS/JS 及旧样式入口已删除，不维护第二套正式 UI。
- 页面包括总览、项目、知识库、文档、任务、Revision/Chunk、检索诊断、证据问答和
  系统状态。导航显示当前 Project、KB、Profile 与 Active Revision。
- 总览已移除营销标语，保留紧凑的当前工作范围与上传入口。
- 生产构建输出 `frontend/dist`，但该目录、依赖、覆盖率、Playwright 报告、截图和
  Trace 均已加入 `.gitignore`，不进入 Git。

## API、身份与工作流

- OpenAPI v1 快照由 21 个 path 增至 25 个 path；生成 TypeScript 类型在构建前用
  `openapi-typescript --check` 核验。
- 新增管理员授权、scope 绑定的 Job 列表、Revision 检查、Chunk 浏览和报告读取。
  Core Port 与 Application Service 返回稳定读模型，不泄漏 SQLite/Qdrant 类型。
- 新文档、新版本和 Rename 使用不同 API。Rename 后用 API 回读，确认不创建新 dver；
  相同字节上传为第二个逻辑文档时 document ID 不同、Source Artifact 相同，删除一个
  不影响另一个。
- Job、slot、writer/fencing、激活结果、实际 Document/Chunk/FTS 数和 coverage 均从
  API 读取；前端不自行修改状态。
- Schema/Port/API 扩展均为向后兼容的只读增量；本阶段没有新增数据库 migration，
  P05.5 Document/Version/Artifact 身份和 P08.5 Revision 合同未改变。

## 检索、证据与质量状态

- Search/Answer 直接展示服务端 route、selected slot、cache、degraded、Evidence 与
  refusal。管理员 Diagnostics 提供 channel、RRF rank/contributions、rerank 和阶段耗时，
  浏览器不重算 raw score。
- Evidence V2 展示 Document/Version/Chunk、SourceSpan、入选原因、检索来源、
  fusion/rerank rank、表格上下文与 publishable；`<EMPTY>`、`<OMITTED>` 仅出现在
  embedding/lexical context，不作为 citation 原文。
- System 页面显示 Offline Evaluation V3、Primary/Standby LIVE 状态、远程生产状态、
  lexical analyzer 和 active revision schema。`not_verified` 使用中性状态，不显示为
  Healthy。
- Provider Probe 需要 Admin、网络警告和第二次确认，并携带显式网络授权及预算 Header；
  不自动周期探测，也不展示 Secret。
- Token 只保存在 React 内存，不进入 URL、localStorage、日志或 bundle。Error Envelope
  显示 code、HTTP status、stage 和 retryable；检索与 SSE 支持 AbortSignal。

## 可访问性与真实离线浏览器验证

- 有可见焦点、跳到主要内容、表单 label、表头、关键 live region、文字状态、
  reduced-motion、Modal/Drawer Focus Trap 与 Escape。
- Playwright 启动 loopback FastAPI 和临时数据目录，通过 Windows Chrome 驱动 WSL
  服务；没有伪造浏览器结果。
- 桌面完整流程创建 Project/KB，上传含“青岛啤酒采购流程”的合成 DOCX，等待 single
  deterministic slot 与 Active Revision，查询“青岛啤酒”并打开精确 Evidence。
- 同一流程加入相似噪声文档，最终引用不发布噪声；还验证 Rename、版本更新、Artifact
  复用、逻辑文档删除隔离、RRF、axe 和表格空列/omitted/merge 边界。
- 375 px 用例验证窄屏导航与 System 状态。设备矩阵共收集 6 项，按配置互斥跳过 3 项，
  实际执行 `3 passed, 3 skipped, 0 failed in 13.6s`。

## 门禁证据

- Frontend install check、ESLint、TypeScript strict、OpenAPI type check 通过。
- Component/API tests: `3 files passed, 8 tests passed`。
- Production build: `1829 modules transformed`；JS 228.08 kB（gzip 70.89 kB），
  CSS 9.43 kB（gzip 2.92 kB）。
- Feature tree final check: compileall、Ruff、strict mypy over 296 files、Google
  docstrings 通过；`1340 passed, 75 deselected, 4 warnings in 239.88s`。
- Feature tree smoke: `71 passed, 1 warning in 8.60s`。
- Post-merge frontend gates全部通过，Playwright 仍为
  `3 passed, 3 skipped, 0 failed in 13.6s`。
- Post-merge final check: compileall、Ruff、strict mypy over 296 files、Google
  docstrings 通过；`1340 passed, 75 deselected, 4 warnings in 241.95s`。
- Post-merge smoke: `71 passed, 1 warning in 8.96s`。
- `git diff --check` 与 staged diff check 通过。

门禁过程中，旧静态前端文件删除后，两个遗留测试仍读取旧路径；已改为验证 React/Vite
资产合同与“旧 API 不挂载第二套 UI”，随后完整门禁通过。Ruff 曾因一行 82 字符停止，
拆行后从头重跑通过。

## 外部调用与剩余风险

- 应用和测试实际调用的远程 RAG 服务：无。没有调用 Jina、阿里云、远程 Qdrant、ERP、
  下单系统或真实企业文档。
- 工具安装曾访问 Node.js 下载站、npm registry 和 Playwright CDN；WSL Chromium 下载
  被连接重置，E2E 改用宿主已安装的 Chrome。Git 提交按目标推送 GitHub。
- `npm audit --audit-level=high` 两次访问 registry 审计端点均得到 `socket hang up`，因此
  当前锁文件的在线漏洞审计结论为未验证，不伪装成 0 vulnerabilities。
- Starlette TestClient 弃用提示和测试内非 TLS Qdrant 提示为现有 warning。
- 本阶段不包含生产容器发布、真实 Provider 探测或真实质量校准；P11/部署阶段仍需验证
  固化静态构建的容器装配和远端限流/费用行为。

## 状态

P10 的稳定 API、精确 Evidence、中文 FTS V2 浏览器流程、可访问性与前后端门禁均已通过；
远程质量边界保持不变：

```text
CJK_UI_E2E_READY: true
EVIDENCE_UI_V2_READY: true
REMOTE_PRODUCTION_PROFILE_READY: false
P10_READY: true
```
