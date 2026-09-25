# 湾事通管理端构建契约

原生 WeKnora 前端仍按 `npm run build` 构建。湾事通管理员前端使用
`VITE_WANSHITONG_GATEWAY_AUTH=true npm run build`，将 `dist/` 挂在
`/kb/admin/`；该模式仅构建管理主入口，不构建原生 `embed.html`。

管理员先在湾事通现有 `/kb/admin/ops/` 登录。管理端启动时：

1. `GET /kb/api/v1/console/session` 必须返回 `authenticated: true` 与
   `csrf_token`。CSRF 仅留在页面内存。
2. `GET /kb/api/engine-admin/bootstrap` 返回 WeKnora `/auth/me` 同形的
   `{ success, data: { user, tenant, memberships, capabilities } }`。
   这些字段须从当前服务端管理员身份与实际绑定的引擎空间得出。
3. 原 `/api/v1/...` 请求由同源 `/kb/api/engine-admin/api/v1/...` 代理；
   受保护文件还使用 `/kb/api/engine-admin/files?...`。所有修改请求带
   `X-CSRF-Token`，浏览器不获取或保存上游 JWT/API Key。
4. 会话失效进入 `/kb/admin/ops/`；“返回湾事通”进入 `/kb/`，不触发
   原生 WeKnora 注销。

网关须在服务端校验管理员会话、CSRF、允许的方法与路径、实际空间和角色。
当前前端隐藏了原生账户、个人密钥、原生 API Key、IM/嵌入集成与沙箱设置。
IM/嵌入需要独立的公开回调和访客路由。沙箱终端和桌面使用 WebSocket，
在湾事通模式被禁用；只有网关补齐对应 WebSocket 代理后才能启用。
原生构建与原生登录流程不受该模式影响。
