# WB-08R 阶段 05 反馈闭环验收边界

记录时间：2026-09-22

## 当前结论

| 维度 | 状态 | 说明 |
| --- | --- | --- |
| 工程实现 | `ENGINEERING_READY` | 05-A～05-D 已实现，相关离线门禁通过 |
| 内部 SSO | `AUTH_INTERNAL_PENDING` | 测试 callback 已登记，仍缺 4 项真实连接参数 |
| 真实试用 | `USER_PILOT_PENDING` | 尚未完成 3～5 名内部用户、每人至少 2 个真实问题与反馈 |
| 阶段终态 | `NOT_FEEDBACK_READY` | 真实 SSO 和用户试用未完成，不声明 `FEEDBACK_READY` |
| 8289 运行态 | `IMAGE_STAGED_NOT_STARTED` | 新镜像已加载到 60，但当前 8289 仍运行阶段 04 镜像 |

阶段 05 的工程实现已完成，但实施方案把普通用户开放前的
`AUTH_INTERNAL_READY` 作为前置条件。为避免用匿名模式伪造真实登录通过，本次只将
候选镜像加载和发布文件暂存到 60，没有启动或替换 8289。

## 已完成范围

- 05-A：canonical 反馈与详情同事务写入；详情失败整笔回滚；事务提交后的 Trace
  投影失败转为可恢复的 `PENDING`，不谎报用户反馈保存失败。
- 05-B：公共页面支持一键“有帮助”、细分负反馈、可选说明、重复点击保护、失败
  重试与输入保留；401 引导重新登录；仅完整终态允许反馈。
- 05-C：复用既有管理员令牌/会话和 CSRF，提供反馈列表、四段详情、复核状态、
  根因、管理员备注、版本冲突处理和受权文档记录入口。SSO 身份不获得管理员权限。
- 05-D：提供帮助率、待处理量、已确认错引/误拒答、真实 SSO 与非 SSO/replay
  时延分桶以及只含安全元数据的导出。统计和反馈不会自动改模型、路由、来源、
  权重或标准答案；`Evaluation Candidate` 仍只是候选。
- 兼容旧 canonical 反馈、旧 reason 和缺失详情/Operational Trace 的记录；实际
  `FAILED` 完成态可反馈，不伪装成 `REFUSED`。

## F-01～F-08 验收证据

| 编号 | 已验证结果 |
| --- | --- |
| F-01 | 正反馈、负反馈、可选说明、旧请求、幂等和正负切换均通过 |
| F-02 | 负反馈未选原因按 `OTHER`；正反馈清理当前负反馈详情；旧 `OUTDATED` 保持原值 |
| F-03 | 详情写失败时主表回滚；投影失败时 canonical 已保存且可恢复 |
| F-04 | 跨 owner 公共访问失败；管理员接口继续使用原管理员鉴权 |
| F-05 | 旧 Trace、缺 Operational Trace 和仅 canonical 反馈均不导致页面 500 |
| F-06 | 复核、根因、来源、备注密文、乐观锁冲突及 `RESOLVED` 验证条件均通过 |
| F-07 | 导出不含问题、答案、用户说明、管理员备注、完整 Trace 或 secret |
| F-08 | 冻结依赖的 5 条问答在反馈前后输入、答案、结果和状态逐项相同 |

实际执行结果：

- 后端相关回归：16 passed，1 warning。
- 前端相关回归：10 个测试文件，50 passed。
- 目标 Python 文件 Ruff、mypy 通过。
- 前端目标 ESLint、TypeScript 检查通过；`npm run build`（含 OpenAPI 检查与
  Vite 生产构建）通过。

以上是代码与离线自动化证据，不是 8289 真实 SSO、真实用户试用或生产证据。

## 候选镜像与运行边界

- 阶段 05 代码 revision：`c8286736745dc2b3b97b0a21417f82bb76165583`。
- 已加载到 60 的候选：`rag-test-wanshitong:wb08r05-c828673`；镜像 ID
  `sha256:3d725515839143994b89ec6e83dcdedc3703236589223f14e1506edc014c718a`。
- 镜像内产品资产：38 个文件、1,141,311 bytes；manifest SHA-256
  `2840310b16be185164f8f93f576f74d7cb734fa41e52003f6b7901d2a3d2a4bb`。
- 60 上的发布文件：`/data/tyf/wanshitong-wb08r05-c828673/release`；加载前后镜像
  归档 SHA-256 均为
  `114f6ee672ad2fde47a3837c5864f7d85b9cb13838d8ef64b9db2b1741beeb78`。
- 当前 8289 未改变：容器 `wanshitong-prep-candidate-app` 仍运行阶段 04 镜像
  `rag-test-wanshitong:wb08r04-8f794efe2fd2`，绑定 `127.0.0.1:8289->8088`，
  只读核对时 live/ready 均为 HTTP 200。
- 生产 `wanshitong-app` 仍使用 `rag-test-wanshitong:604ef63`；本阶段没有停止、
  重建、替换或发起功能测试到 8288/18288。

本文件的文档提交晚于镜像 revision；文档变化不改变镜像内运行代码，因此镜像仍
以 `c828673` 为准确代码身份。

## 清理结果

- 60 上无容器引用的旧 SSO 候选镜像
  `rag-test-wanshitong:wb08r-sso-36529b4` 已删除。
- 正在运行的阶段 04 镜像、生产镜像以及仍被停止容器引用的阶段 03g 回退镜像均
  保留，没有把运行态或回退点当成过期镜像删除。
- 本地阶段 05 镜像、临时构建目录以及 54/60 的传输压缩包均已删除；60 上保留
  唯一待启动的阶段 05 候选镜像和发布文件。

## 尚需外部提供与后续验收

测试 callback 已确认登记为
`http://10.242.180.54:8289/kb/sso/callback`。真实 SSO 仍需：

1. 浏览器可达的完整 authorize URL；
2. 60 可达的完整 validate URL；
3. 已登记的 `clientId`；
4. 通过安全渠道交付并以文件注入的 client secret。

取得以上 4 项后，才能启动阶段 05 镜像到专用 8289，使用已提供的测试账号验证
真实登录，并执行 S-01～S-12。S-08 账号切换另需第二个测试账号。最后还需 3～5
名内部用户各提至少 2 个真实问题并提交反馈；完成前保持
`AUTH_INTERNAL_PENDING` / `USER_PILOT_PENDING`，不标记 `FEEDBACK_READY`。
