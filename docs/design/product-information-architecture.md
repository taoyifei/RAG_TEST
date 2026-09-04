# 产品信息架构

一级导航为工作台、知识库、问答、模型服务。低频运维能力折叠为文档管理、处理任务、
索引记录、检索调试、检索方案、系统状态和接口访问。

路由由 `app/router.ts` 的 typed route table 管理。project、knowledge base 和 revision
进入 URL query，便于刷新与分享当前非敏感范围；同一范围可写入 sessionStorage。
Secret、管理员 Token、CSRF 和 Provider 对象不进入浏览器存储。

`App.tsx` 只负责装配 Provider 与工作区，页面、feature、hook、copy 和 style 分目录。
会话恢复完成前不挂载会发 API 请求的页面，避免旧 Cookie 与恢复轮换请求竞争。

查询答案先展示，Diagnostics 独立异步读取和折叠；诊断失败不会覆盖已成功的答案。
任务轮询在页面不可见时暂停，并对无变化结果退避。
