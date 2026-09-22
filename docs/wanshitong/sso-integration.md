# 湾事通 SSO 集成说明

本实现以《03_阶段SSO_登录与会话_实施方案》为准，`SSO-SPEC.md` 仅作为
RDMS CAS Ticket v1.3 接口参考。当前状态为 `REGISTRATION_INPUTS_PENDING`；
这表示 KB 侧合同和部署门禁可验证，但真实 RDMS 注册参数仍不完整，不能标记
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

## 2026-09-22 实际交付证据

- SSO-A：`5bcfd7f`；SSO-B：`ba5508b`；SSO-C：`36529b4`。
- Python SSO、访问日志和候选守卫定向门禁：45 passed；Ruff 与目标 mypy
  通过。前端 TypeScript、ESLint、生产构建通过，7 个相关测试文件共 57 passed。
- 候选镜像 `rag-test-wanshitong:wb08r-sso-36529b4` 已加载到 60；镜像 ID
  `sha256:b9bf7968c602564063463de68ea72eb0699d6465bd90217a4a108215ce68882a`，
  内置 revision 为 `36529b4b7de6abdb1f0c62620731e6aa14aa8906`。产品资产自检为
  37 个文件、1,117,585 bytes，manifest SHA-256 为
  `c0a1b67748e492b9e5f153d0481721dc65320de135be90b991a1bb5a57d9c689`。
- SSO 阶段的候选发布文件位于
  `/data/tyf/wanshitong-wb08r-sso-36529b4/release`，旧 8289 inspect 基准保存在
  同级权限受限的 `evidence/baseline.private.json`。当时未创建
  `wanshitong-sso-candidate-app`，因为真实注册输入和 secret 不完整。
- 60 的 `wanshitong-app` 仍运行镜像 `rag-test-wanshitong:604ef63`；旧 8289
  `wanshitong-prep-candidate-app` 仍 running/healthy。54 的 18288 live/ready
  均为 HTTP 200，本阶段没有停止、重建或替换这两项服务。
- 本地候选镜像、构建目录以及 54/60 的传输压缩包已经删除。阶段 05 收尾时，
  未被容器引用的旧 SSO 候选镜像也已清理；包含 SSO 与反馈闭环的阶段 05 镜像
  已加载但未启动。旧 8289 当前仍是唯一运行回退点，不能在新候选未启动时误删。

用户已确认测试回调精确登记为
`http://10.242.180.54:8289/kb/sso/callback`。真实 SSO 仍缺 4 项：浏览器可达
authorize URL、60 可达 validate URL、已登记的 `clientId`、安全交付的 client
secret。这些是实施方案第 2 节的开工前输入，不属于客户端代码修复轮次；取得前
不能使用测试账号执行真实登录。
