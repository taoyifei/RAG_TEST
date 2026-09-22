# WB-08R 准备阶段 SSO 登记清单（CAS Ticket v1.3）

状态：`REGISTRATION_INPUTS_PENDING`

用途：登记阶段 SSO 的真实外部输入，不代表已完成注册、联调或安全验收。

## 已确定的协议

- 身份源为 RDMS（RuoYi），KB 为 SP。
- 本期使用 CAS 式一次性 Ticket，不引入 OIDC、SAML 或 IAM。
- 用户名和密码只进入 RDMS；KB 不直连密码表，也不建立用户表。
- RDMS 在 `sys_sso_client` 登记应用，`service_urls` 以逗号分隔多个精确回调。
- KB 只从后端调用 `/sso/validate`，不读取 IdP Redis。
- 本期不要求 JWKS、discovery、refresh token、MFA、统一角色映射平台或实时 SLO。

## 需要外部提供的信息

| 项目 | 要填的实际值 | 当前状态 | 责任方 |
| --- | --- | --- | --- |
| RDMS 浏览器 authorize URL | 浏览器可达的完整 `/sso/authorize` URL | 待提供 | RDMS / 网关 |
| RDMS 后端 validate URL | 后端直连 21000 或实际 `/prod-api/sso/validate` 完整 URL | 待提供 | RDMS / 网络 |
| `clientId` | 已登记的 KB 应用 ID；明确测试与生产是否分别注册 | 待登记 | RDMS 管理员 |
| `clientSecret` 交付 | 只记录安全注入来源和是否到位，不填写明文 | 待提供 | RDMS / 部署负责人 |
| 8289 浏览器 origin | 实际 scheme、host 和 port，不按容器地址猜测 | 待确认 | KB 负责人 |
| 8289 callback | `<测试浏览器 origin>/kb/sso/callback` | 待登记 | 双方 |
| 正式浏览器 origin | 以用户实际访问的 18288 或正式域名为准 | 待确认 | KB / 网关 |
| 正式 callback | `<正式浏览器 origin>/kb/sso/callback` | 待登记 | 双方 |
| 外网 origin | 无外网环境时明确写本期不实测 | 待确认 / 可后置 | 网关 |
| 可信代理与前缀 | 哪一跳处理 `/kb`，保留哪个 host 和 proto | 待确认 | 部署负责人 |
| 两个测试账号 | 只记录可用性，不保存用户名和密码；先验证能登录 RDMS | 已通过带外方式提供 1 个，仍缺账号切换用第 2 个 | RDMS 管理员 |
| KB 管理员入口 | 保留既有管理员机制，不按 RDMS admin 角色自动放权 | 用户已确认；本地回归已覆盖，待 8289 联调 | KB 负责人 |

## 后续实现必须保持的最小合同

1. 浏览器整页跳转 authorize；KB 后端不能代替浏览器请求并写 pending cookie。
2. `POST validate` JSON 包含 `ticket`、`service`、`clientId`、`clientSecret`。
3. 按真实 HTTP 状态处理失败；200 时校验 `data.userId`、`service` 和 `state`。
4. Ticket 为 32 位 hex、TTL 10 秒；callback 立即换票，429 和超时不自动重试同一 Ticket。
5. 内部业务 owner 来自受信返回的 `userId`，例如 `rdms:<userId>`，不采信前端 `userId`。
6. KB 会话先按既有实施方案固定 2 小时；与规格滑动续期的差异显式保留，不在准备阶段实现。
7. 8289 与正式入口均需登记；精确匹配包括端口与路径，同 host 不同端口的 Cookie 使用不同部署名。
8. SSO 部门字段暂不缩小问答检索范围；04 仍为影子路由。

## 准备阶段完成线

本清单已明确协议、参数、提供方和后续接口合同。地址、Client 或账号可以保持
`PENDING`，不阻断阶段 04；没有真实输入时，后续 SSO 阶段不得标记
`AUTH_READY`。

本文不记录任何真实 Client Secret、Cookie、Ticket、令牌或个人账号。没有证据时
不把示例公网域名写成实际服务，也不从源码猜测 RDMS 用户表名。阶段 06 继续遵守
人工试用与明确批准发布流程。

## 2026-09-22 现场只读核对

- 54 的 18288 仍是转发到 60 的 8288，属于正在使用的服务，本阶段禁止停止、
  替换或重建。
- 60 的旧 8289 候选健康，但未发现监听 21000 的 RDMS/SSO 服务，也没有取得
  authorize URL、validate URL、`clientId`、secret 或已登记 callback 的证据。
- 因此真实登录输入仍为 `REGISTRATION_INPUTS_PENDING`；代码和候选配置可以
  完成，但不能用占位地址启动后冒充真实 SSO 登录通过。
- `/kb/admin` 明确保留原 Bootstrap Token / 管理员 Cookie。即使 SSO 身份携带
  `admin` 角色或通配权限，也只能得到公共用户主体，不能调用管理员 API。
