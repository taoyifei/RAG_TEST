# Universal RAG Web Console

P10 提供单一正式 React 控制台入口。生产构建位于 `frontend/dist`，由
`rag_app.api.p10.create_p10_app` 与 `/api/v1` 共用同一 FastAPI 进程提供；旧静态
`app.js` 与 `debug.html` 不再作为正式入口。

## 页面与范围

- 总览显示当前 Project、Knowledge Base、Profile 与 Active Revision。
- 项目和知识库页面建立显式 scope；切换 Project 会清空旧 KB 与 Revision。
- 文档页面分别提供“新建文档”和“创建新版本”，并可回读版本、Content SHA 与
  Source Artifact。
- 任务页面轮询服务端 Job，展示 stage、状态、slot 进度和 fencing 安全状态。
- IndexRevision 页面展示实际/预期文档和 Chunk 数、FTS 数、slot coverage、三种
  Chunk 文本及 Parse/Chunking report。
- 检索诊断页面显示 API 返回的通道候选、RRF 逐通道贡献、重排结果、耗时与 Evidence。
- 问答页面以 SSE `final` 事件为最终事实，拒答会显示服务端状态和原因。
- 系统页面把 configured、not verified 与显式 Probe 分开，不把配置存在解释为健康。

Token 只保存在 React 内存状态，不进入 URL、localStorage、构建产物或日志。刷新页面
会清空 Token。Query 与 Admin 权限保持分离。

## Evidence V2

Evidence Drawer 直接展示服务端返回的 citation quote、Document/Version/Chunk、
SourceSpan、入选原因、表格上下文、融合/重排排名及 publishable 状态。控制台不自行
计算 RRF、置信度、路由或 Provider 健康状态，也不把 `<EMPTY>`、`<OMITTED>` 等结构
占位符当作原文引用。

## 可访问性

控制台提供键盘导航、可见焦点、跳到主要内容、Modal/Drawer Escape 与焦点循环、文字
加图标状态、表头、WCAG AA 对比度和 reduced-motion。真实离线 Playwright 同时覆盖
1280 桌面和 375 px 窄屏，并执行 axe 检查。

