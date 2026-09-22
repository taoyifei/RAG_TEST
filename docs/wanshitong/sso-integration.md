# 湾事通 SSO 集成说明

本实现以《03_阶段SSO_登录与会话_实施方案》为准，`SSO-SPEC.md` 仅作为
RDMS CAS Ticket v1.3 接口参考。当前状态为 `REGISTRATION_INPUTS_PENDING`；
这表示 KB 侧合同和部署门禁可验证，但真实 RDMS 注册参数尚未取得，不能标记
`AUTH_INTERNAL_READY`。

## 边界

- 普通工作台 `/kb/` 使用 RDMS 登录和 KB 两小时本地会话。
- 管理员页面 `/kb/admin` 与 `/kb/api/v1/...` 保留原 Bootstrap Token 和
  管理员 Cookie，不接 RDMS SSO。
- RDMS 的 `roles`、`permissions` 与 `deptId` 仅作为身份属性；不授予 KB
  管理员权限，也不改变检索范围。
- 退出仅清理 KB 本地会话，不实现 RDMS 全局注销。
- 18288/8288 当前服务不在本阶段切换范围；真实候选只允许使用 8289。

## 明确的 Spec 适配

原 Spec 提到用 `sessionStorage` 校验 state，但服务端不能读取浏览器存储。本实现
由 KB 签发加密、短时、HttpOnly 的 pending Cookie，并以其中冻结的 state、
service、return_to 和入口 ID 为服务端权威；前端存储只用于恢复尚未提交的输入
草稿。IdP 的 ticket、service 和 state 合同不变。

原 Spec 建议滑动续期，本阶段按主实施方案固定 7200 秒，不在每次问答或 SSE
请求中重新 validate。退出后 RDMS 仍可能保持登录，再次进入时直通属于预期。

## 安全与路径合同

- 外部前缀固定为 `/kb`；API、SPA 历史、静态资产、SSO 和管理员请求只补一次
  前缀。
- SSO 入口来自有限注册表。未知 Host、外域 return_to、协议相对 URL、callback
  回环路径及重复 query 参数均拒绝。
- callback 只调用一次固定 validate URL，connect timeout 2 秒、总目标 5 秒，
  禁止重试和跟随重定向。
- pending 与用户 Cookie 使用项目现有加密能力。用户 owner 固定为
  `rdms:<userId>`，不使用前端提供的用户字段。
- Uvicorn 对 `/sso/entry`、`/sso/callback` 删除完整 query 后再记录访问日志，
  避免 ticket/state 落盘。
- SSO 会话、匿名会话和管理员会话不互相降级。公共接口 401/403 触发浏览器
  整页登录，不在 SSE 中接收 HTML 重定向。

## 候选部署门禁

候选 Compose 使用独立 project/container：
`wanshitong-sso-candidate` / `wanshitong-sso-candidate-app`。运行时守卫只有在
显式 `--allow-sso-enable` 时才允许 SSO 差异，并逐项校验：

- `RAG_ROOT_PATH=/kb`、`RAG_WANSHITONG_AUTH_MODE=sso`；
- 非空且格式合法的入口注册表、deployment ID、validate URL 和 client ID；
- client secret 文件固定为 `/run/rag-secrets/sso-client-secret`；
- 候选保留旧可信 origins，并包含所有登记入口 origin；
- 端口、网络、挂载、只读根文件系统和其余应用环境继续与旧 8289 基准一致。

缺少 authorize URL、validate URL、clientId、secret 或精确 callback 注册任一项，
候选启动即停止，不允许切回匿名模式制造通过。

## 验收状态

本地自动化已覆盖设置、pending/session、validate 客户端、callback、公共主体、
CSRF、`/kb` 前缀、401 整页登录、本地退出及管理员隔离。真实 S-01～S-12 仍需
以 8289 的浏览器和 RDMS 完成；其中 S-08 账号切换还需要第二个测试账号。
外网入口未提供时最终状态只能是 `EXTERNAL_NOT_RUN`。
