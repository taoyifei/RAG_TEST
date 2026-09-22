# WB-08R 准备阶段 SSO 登记清单（CAS Ticket v1.3）

状态：`TEST_LOGIN_VERIFIED`

用途：记录阶段 SSO 的实际外部输入和 8289 联调边界。本文不保存任何密码、
Client Secret、Ticket、Cookie 或令牌。

## 已确定的协议

- 身份源为 RDMS（RuoYi），KB 为 SP。
- 本期使用 CAS 式一次性 Ticket，不引入 OIDC、SAML 或 IAM。
- 用户名和密码只进入 RDMS；KB 不直连密码表，也不建立用户表。
- RDMS 在 `sys_sso_client` 登记应用，`service_urls` 以逗号分隔多个精确回调。
- KB 只从后端调用 `/sso/validate`，不读取 IdP Redis。
- 本期不要求 JWKS、discovery、refresh token、MFA、统一角色映射平台或实时 SLO。

## 实际登记信息

| 项目 | 实际值或边界 | 当前状态 |
| --- | --- | --- |
| RDMS 浏览器 authorize URL | `http://172.24.7.172:21000/prod-api/sso/authorize` | 8289 实测可用 |
| RDMS 后端 validate URL | `http://172.24.7.172:21000/prod-api/sso/validate` | 60 实测可用 |
| 测试 `clientId` | `wanshitong_dev` | 已注入 8289 |
| 测试 `clientSecret` | 仅以 60 上权限 `0600` 的文件注入，不在文档和环境变量记录明文 | 已到位并实测 |
| 8289 浏览器 origin | `http://10.242.180.54:8289` | 已实测 |
| 8289 callback | `http://10.242.180.54:8289/kb/sso/callback` | 已登记并实测 |
| 正式浏览器 origin/callback | 以正式 18288 或正式域名为准 | 未登记、未测试 |
| 外网 origin | 本期未提供 | `EXTERNAL_NOT_RUN` |
| 可信代理与前缀 | 54:8289 转发到 60:18289；60 只剥离一次 `/kb`，应用保持回环绑定 | 已实测 |
| 测试账号 | 1 个账号完成登录；不在文档保存账号或密码 | S-08 仍缺第 2 个账号 |
| KB 管理员入口 | 保留既有 Bootstrap Token / 管理员 Cookie | SSO 会话访问管理员 API 实测 401 |

生产 `clientId` 和 Client Secret 虽由用户提供，但本阶段没有写入 8289，也没有
用于测试；正式入口必须在用户另行授权后配置和验证。

## 必须保持的最小合同

1. 浏览器整页跳转 authorize；KB 后端不能代替浏览器请求并写 pending cookie。
2. `POST validate` JSON 包含 `ticket`、`service`、`clientId`、`clientSecret`。
3. 按真实 HTTP 状态处理失败；200 时校验 `data.userId`、`service` 和 `state`。
4. Ticket 为 32 位 hex、TTL 10 秒；callback 立即换票，429 和超时不自动重试同一 Ticket。
5. 内部业务 owner 来自受信返回的 `userId`，例如 `rdms:<userId>`，不采信前端 `userId`。
6. KB 会话按既有实施方案固定 2 小时；与规格滑动续期的差异显式保留。
7. 8289 与正式入口需分别登记；精确匹配包括端口与路径，同 host 不同端口使用
   不同部署名。
8. SSO 部门字段暂不缩小问答检索范围；04 仍为影子路由。
9. 普通 SSO 身份不映射成 KB 管理员身份。

## 2026-09-22 现场结果

- 8289 单账号真实浏览器登录成功，页面显示受信用户，公共会话为 HTTP 200；
  退出只清理 KB 本地会话。
- 同一 SSO 会话访问管理员 API 为 HTTP 401，管理员入口仍使用原令牌/会话。
- 60 的 `pdf2md_default` 与 RDMS 私网地址发生 `/16` 路由冲突；经用户授权关闭
  原 Compose 项目后，authorize 和 validate 均可访问。未删除其镜像或数据卷。
- 54 的 18288 和 60 的 8288 生产服务没有停止、重建、替换或功能测试。
- 当前证据只满足单账号 happy path；完整 S-01～S-12、第二账号切换、正式入口
  和外网入口仍未运行，不能标记 `AUTH_INTERNAL_READY`。
