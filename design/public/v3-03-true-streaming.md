# V3-03 默认 Product 真实流式、停止与结算边界

日期：2026-09-09

公开性：本文只记录公开合成输入、代码身份和聚合测试结果，不包含私有文档、
文件名、正文、逐文件 hash、凭据、Prompt、完整问题/答案或 FULL Trace。

V3-03 已把默认 Product 的回答路径从“结束后一次性包装为流”改为真实的、有界的
SSE：真实 TCP 在 Provider 尚未结束时即可收到响应头、协议事件和已经完成来源校验
的完整 claim；浏览器增量展示这些临时 claim，只在唯一 `final` 后提交正式答案、
成功历史和缓存。断连、停止、超时、来源删除、会话撤销或 Token 撤销均不能把未
核验正文、成功历史或成功缓存留下。

本阶段没有调用真实 Provider，没有 push、merge、发布或生产部署。合成同步
Provider 的 token 数只验证结算合同，不代表真实费用。发布安全边界继续由最终
V3-04 候选的构建与扫描收据决定。

## 状态

| 状态项 | 状态 | 证据边界 |
| --- | --- | --- |
| `TRUE_STREAM_TCP` | `PASS` | loopback Uvicorn + HTTP 客户端；Provider 尾部阻塞时响应头、首字节和完整 claim 均已到达 |
| `TRUE_STREAM_BROWSER` | `PASS` | 真实 Chromium 桌面/移动视口；增量显示、正式收束、停止和知识库切换隔离 |
| `VERSIONED_SSE_PROTOCOL` | `PASS` | `rag-answer-sse-v1` 的 meta/stage/claim/final/error/cancelled、单调 sequence 与唯一终态 |
| `VALIDATED_CLAIM_PUBLICATION` | `PASS` | 只发布完整且已通过支持编号、quote、来源与 active revision 校验的 claim |
| `BACKPRESSURE_AND_TIMEOUTS` | `PASS` | 固定容量队列、逐事件 HTTP delivery ack、first-content/idle/total 三类时限 |
| `CANCELLATION_BUDGET_SCOPE_SAFETY` | `PASS` | 断连传播、不可中断上游占槽、实际 usage、无成功缓存、重试和在途重鉴权 |
| `NON_STREAMING_PARITY` | `PASS` | 同一 SDK/Application 查询链、相同最终 `SearchAnswerResult` 与引用校验 |
| `CURRENT_PARSER_CHUNKER_PRESERVED` | `PASS` | V3-03 未修改 Parser、Chunker、Document/Version、SourceSpan 或 index fingerprint |
| `LIVE_PROVIDER_STREAM_QUALITY` | `BLOCKED` | 无独立非生产凭据和本轮真实出网/预算授权；实际真实 Provider 请求为 0 |
| `V3_04_ENTRY` | `PASS` | 定向跨阶段回归 87 passed；发布和生产能力不由此状态外推 |

## 身份与默认路径

- 执行分支：`codex/universal-selective-word-v3`。
- 阶段进入点：`f6d27d7`。
- Provider 流与传输结算：`643b54a`。
- Product SDK/API 与可核验回答流：`4b10282`。
- 浏览器增量呈现：`2bafa24`。
- Google 风格文档与真实流源码合同：`e68b42d`、`a760637`。
- V3-04 收尾发现并修复的浏览器视口服务碰撞：`03c36ae`；它只隔离本地
  desktop/mobile 测试服务，不改变 Product SSE 协议。
- 默认入口仍是 `rag-app serve` → Product Runtime →
  `/api/v1/projects/{project_id}/knowledge-bases/{kb_id}:answer`；没有切回 legacy
  `/api/chat`，也没有恢复旧 Runtime 的全局状态。

main `af30f81` 和 Industry `5cc5d7b` 只作为源码参照。二者的默认服务仍走 legacy
`/api/chat`，且没有与当前 Product 在同语料、模型、缓存、硬件及预算下运行，
因此不提供流式时延的 A 数值对照。

## 最早偏差与最小修复

V3-00 的真实 TCP 复现已经证明，旧 Product 的 `stream=true` 只是在同步
`answer()` 完成后把最终 JSON 拆成 SSE；Provider 执行期间客户端收不到响应头或
内容。前端也先读取完整 body 再更新页面。因此“接口返回 SSE”并不等于真实首内容。

修复保留现有 Core/Ports/Application/Adapters/Composition Root，并把最早阻塞点
逐层打开：

1. Aliyun HTTP adapter 增加严格 UTF-8、事件边界和结构化增量解析；只有完整 claim
   才进入校验，重复字段、截断 JSON 或后续非法 claim 均不能发布。
2. generation 层把每个完整 claim 绑定 Support ID 和 quote，沿用既有来源、数字、
   否定和 active revision 校验；不是把未核验 token 直接显示为答案。
3. SDK/Application 将 retrieval、generation、validation 和最终结果放在同一请求
   快照与历史结算中，非流式和流式共享同一业务链。
4. API 在返回 `StreamingResponse` 前完成 schema、认证、CSRF、KB scope 和固定查询
   容量准入；响应开始后使用类型化安全错误事件，避免换成伪 200 JSON。
5. HTTP 协调器使用最大 16 个事件的队列，并要求消费者实际取走事件后 worker 才
   继续读取 Provider，从而让慢客户端形成真实背压。
6. React 客户端按 SSE frame 增量解析，只把 `claim` 放入“已核验暂存内容”；
   `final` 才替换为正式答案。新的查询、停止、导航或 KB 切换会取消旧请求并丢弃
   迟到事件。

## 协议与发布边界

`rag-answer-sse-v1` 的公开事件顺序如下：

```text
meta → stage* → claim* → exactly one(final | error | cancelled)
```

- `meta` 只含协议、trace、project、KB 和 delivery 语义，不含问题正文。
- `stage` 只含 accepted/snapshot/retrieval/generation/validation 及有限安全属性。
- `claim` 是完整 `AnswerClaim`，带支持引用、active revision、连续 claim index 和
  `provisional=true`；它已通过当前发布边界校验，但仍须由 final 收束。
- `final` 携带与非流式路径相同的 `SearchAnswerResult`，是唯一允许写成功历史和
  查询缓存的终态。
- `error` 明确 stage、重试性及是否已经交付过 claim；错误体不包含未核验尾部。
- `cancelled` 明确取消已请求、是否尝试关闭上游，以及上游停止是 confirmed 还是
  unknown，不伪称不可中断调用已经停止。

响应使用 `text/event-stream; charset=utf-8`、`Cache-Control: no-store,
no-transform` 和 `X-Accel-Buffering: no`，并禁用 Nagle 延迟。序号必须严格递增，
scope 与 trace 必须在每个事件上一致；final 后的异常不能追加第二个终态。

## 取消、成本与权限

实际 loopback TCP 断连用例中，客户端收到首个 claim 后关闭连接：

- History 与 Operational Trace 最终均为 `CANCELLED/REQUEST_CANCELLED`；
- 不可中断的同步合成 Provider 返回前，QueryExecutor 的真实槽位保持占用；
- Provider 返回后记录 1 次 generation 调用和夹具实际报告的 30 tokens；
- 整体请求不写成功缓存；相同问题重试产生第 2 次真实 adapter 调用；
- 已发送的前缀不能被当作成功 final。

这 30 tokens 来自本地合成 responder，只证明“取消后仍按实际观测结算”，不代表
任何远程费用。可中断 HTTP 流会主动 close；无法确认上游停止时保持
`upstream_stopped=unknown`，不会提前释放容量或归零 usage。

在 Provider 产生下一条 claim 前删除文档、撤销 Session 或撤销 scoped access
token，均得到安全 error，`partial=false`，且没有 claim/final。每个发布边界都会
重查当前来源和授权，因此握手时有权不等于整个长流永久有权。

first-content、idle 和 total timeout 各有独立错误码；总时限由独立 timer 驱动，
即使客户端停止拉取首个 meta，也能取消 worker。时限或发送故障后的唯一终态和
容量回收均有定向测试。

## 实际验证

| 命令/运行 | 退出状态 | 结果 |
| --- | ---: | --- |
| V3-00.6/00.7/01/02/03 跨阶段定向 pytest | 0 | 87 passed、1 个既有 Starlette/httpx warning，67.41s |
| Provider SSE adapter 定向测试 | 0 | 完整 claim 提前发布、usage 保留、重复字段与后续非法 claim 拒绝 |
| P09 流协调定向测试 | 0 | delivery ack、三类 timeout、partial error、断连占槽、唯一终态 |
| 真实 loopback TCP 基线 | 0 | Provider 未结束时 2 秒门限内收到响应头、首字节和 claim；该门限不是 TTFC 测量值 |
| 真实 TCP 停止/权限竞态 | 0 | 断连不写成功、文档删除/Session 撤销/Token 撤销均阻断正文与 final |
| 前端 API 单元测试 | 0 | chunked SSE、CRLF/UTF-8 边界、协议/scope/sequence/终态错误与 AbortSignal |
| 最终 web-e2e | 0 | 11 passed、3 skipped，桌面/移动真实浏览器均通过；49.2s |

web-e2e 的流式场景整段耗时约为桌面 3.2s、移动 3.9s，它包含登录、建库、上传、
索引和多次请求，不能写成首内容时延。真实 TCP 测试证明顺序和“Provider 未结束前
已交付”，但只用 2 秒 deadline 判定，不把 deadline 当成精确 TTFC。3 个浏览器
skip 是既有外部 candidate 模式的条件分支，不隐藏 Product 流式、停止或隔离用例。

最终完整离线、前端、构建和浏览器门禁统一记录在 V3-04 验收报告及候选收据中，
避免用本阶段的定向数量冒充最终全量门禁。

## 重建、重启与剩余边界

V3-03 不改变 DocumentIR、Parser、Chunker、Embedding、向量空间或 index revision，
无需重解析、重分块或重嵌入。部署需要重新构建后端/前端并正常重启服务以加载新
API 和浏览器代码；旧客户端可继续请求非版本化 final-only 兼容流，新前端显式协商
`rag-answer-sse-v1`。

真实首合法内容、总时延、Token/费用和取消浪费仍缺独立非生产 Provider 分母，状态
为 `BLOCKED`。默认关闭或未授权的 Provider 不因离线 SSE 合同通过而变成已验证。
恢复条件是提供独立非生产凭据、明确数据出网与累计预算授权，并在相同候选身份上
运行真实 cold/warm、正常结束、停止、timeout 和撤权矩阵。
