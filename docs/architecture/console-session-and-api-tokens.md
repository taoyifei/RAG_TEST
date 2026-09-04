# 管理员会话与接口访问 Token

## 浏览器管理员会话

首次使用向 `POST /api/v1/console/session` 提交部署侧 Bootstrap Secret。服务端以 keyed
HMAC 保存随机 Session/CSRF token 的摘要，并设置 HttpOnly、SameSite=Lax Cookie。
非 loopback 请求同时设置 Secure。会话 TTL 有界，登录失败按客户端窗口限速且返回统一
错误。

写请求要求内存中的 `X-CSRF-Token`。刷新页面时，同源 GET 验证 Cookie、轮换 Session
并返回新的 CSRF token；旧 Cookie 立即吊销。页面支持主动轮换和 Logout。Bootstrap、
Session、CSRF 与 Provider Secret 都不写入 localStorage/sessionStorage。

## 外部 API Token

管理员可创建 `query:read`、`knowledge:read`、`knowledge:write`、`system:read` 的组合，
并可选绑定 project/knowledge base 与过期时间。完整 `ragk_` token 只在创建响应显示一次；
数据库保存 keyed HMAC、scope 和生命周期字段。列表不能恢复完整值，吊销立即生效。

外部 SDK 继续使用 `Authorization: Bearer`。迁移期 `RAG_QUERY_TOKEN` 和
`RAG_ADMIN_TOKEN` 可继续传入，但默认控制台只使用 Cookie Session。
