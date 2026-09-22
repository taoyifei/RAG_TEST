# WB08R-03G V14.1 C5：D3 真实协议资格 P0 清单

## 结论

状态为 **FAILED / PAUSED**。源码 `4c981fa4c8579c98c2881bee1106b57f749d19e6`
已先完成任务书 A1、B、C、D1、D2 和 D3 资格脚本的一致性检查及相关离线门禁，
随后才访问当前真实推理服务。受控 A/B 确认了旧 C5 的 HTTP 400 根因，并证明
`uniqueItems` 可安全移交本地验证；但 D3 五种合成形状只有 1 项通过，不能签发字段协议
能力资格。

这些失败发生在严格实现计划之后。依照用户要求，本轮没有修改提示词、timeout、输入预算、
字段语义或资格真值，没有重试失败项，没有构建或部署候选，也没有进入业务五题、
Preflight-16 或 A～F。

## 已闭合的旧 P0：`uniqueItems` 不受当前结构化后端支持

受控实验使用相同重建 fixture、同一实际 Adapter、相同消息、模型、mode 和 160-token
输出预算。两次调用分立计账，不是线上 fallback 或自动 retry。

| 阶段 | 唯一 HTTP payload 差异 | 结果 |
|---|---|---|
| `reconstructed-original` | 保留 `$.response_format.json_schema.schema.properties.r.items.properties.c.uniqueItems=true` | HTTP 400，完整错误为 `Grammar error: Unimplemented keys: ["uniqueItems"]` |
| `reconstructed-transferred` | 只删除上述 keyword | HTTP 200，`finish_reason=stop`，输入 374 / 输出 67 tokens |

因此当前服务的能力配置应固定 `allow_unique_items=false`，候选唯一性继续由同一字段合同的
本地验证器严格执行。不能推导为“其它 schema 形状已经合格”。

证据 SHA-256：

- A safe：`687ab0ead08475b44cf41cb7a618afca8a3379d1b093758812888b26d87285c3`。
- A private：`614d5430f6ed3ff8477b47395e81b86cbe1809acb30f9c81c4ef092808f73454`。
- A 完整 HTTP 诊断：
  `0fd71ea0957a4574fb512d51a6a9030b232032780c54c769361a223c789454d3`。
- B safe：`e44de5975c6cdae16e2d0024989e588d8ad4680a9cb0bd3751dde987918cfc4a`。
- B private：`25687952288d680f226234981e3c553e52f9e4e9f31a33f69229abc27eea50a2`。

## P0-01：等义单字段资格请求超时

P0 ID：`P0_V141_C5_D3_PARAPHRASE_TRANSPORT_TIMEOUT`

`PARAPHRASE` 使用当前 Product 字段解析方法、当前连接、`response_format`、V2 字段合同和
真实服务。它只发送一次，无 Provider retry。

### 已观察

- 预检估算：消息 651 + schema 603 = 1254 tokens，低于 6144 输入上限。
- 客户端在 8011 ms 得到 `ReadTimeout`；配置的 connect/pool/read/write timeout 均为 8 秒。
- 执行状态：`TRANSPORT_FAILED`。
- 安全 reason：`FIELD_RESOLUTION_PROVIDER_UNAVAILABLE / HTTP_TRANSPORT`。
- 服务端日志在相应窗口观察到 `Running: 1, Waiting: 0`，但没有观察到可归属此请求的
  access 完成行、abort、error 或 timeout 行。

### 未观察

- 模型是否最终生成了内容。
- 客户端取消是否到达服务端。
- 超时由排队、prefill、decode、structured grammar 或其它因素造成。
- 将 timeout 调高后是否能正确完成。

服务端根因为 `NOT_OBSERVED`。本轮没有用提高 timeout、重试或改变 schema 来猜测修复。

### 影响与恢复门槛

单字段等义选择是字段协议的基本形状。一次未完成即可使 D3 不合格。后续批次若获授权，
需先在保持请求合同不变的条件下补齐 request correlation 和服务端完成/取消观测，再决定
是否属于容量/时延配置或协议执行故障；不得用无限重试掩盖。

## P0-02：`NOT_FOUND` 得到 HTTP 200，但输出违反状态组合合同

P0 ID：`P0_V141_C5_D3_NOT_FOUND_OUTPUT_INVALID`

### 已观察

- Provider transport 成功，账本记录输入 296、输出 62、合计 358 tokens。
- 本地统一合同验证拒绝输出，错误为
  `FIELD_RESPONSE_STATUS_COMBINATION_INVALID`。
- 执行状态正确落为 `OUTPUT_INVALID`；没有消费任何字段解析结果，也没有把它写成
  “资料不足”的语义结论。

### 关键观测缺口

当前成功响应路径没有把这次原始 `message.content` 写入私有诊断，因此无法从现有证据判断
究竟是 `NOT_FOUND` 携带非空候选、其它状态使用了非法候选数，还是另一种状态组合错误。
精确 `s/c` 组合必须记为 `NOT_OBSERVED`，不能根据错误名补造。

这不改变本地验证器正确拒绝非法对象的结论，但使模型输出偏差无法做字段级复盘。后续若获
授权，应为“HTTP 成功但结构/语义合同失败”增加与 A1 同等级的有界私有原始正文诊断；不得
写普通日志或前端 Trace，也不得放宽状态组合合同。

## P0-03：双 Atom 输出结构合法，但字段身份语义错误

P0 ID：`P0_V141_C5_D3_TWO_ATOMS_SEMANTIC_MISMATCH`

### 真值

- A1 应为 `SUPPORTED_PARAPHRASE`，只选择与 A1 问法对应的一个字段身份。
- A2 应为 `SUPPORTED_PARAPHRASE`，只选择与 A2 问法对应的一个字段身份。
- 真值按字段身份绑定，不依赖 F1/F2 的排列顺序。

### 实际结果

- HTTP 成功，`finish_reason=stop`，输入 385 / 输出 81 tokens。
- A1 返回 `AMBIGUOUS`，同时选择本 Atom 的两个候选。
- A2 返回 `AMBIGUOUS`，同时选择本 Atom 的两个候选。
- Atom 集、候选范围和 JSON 结构均合法，因此本地合同正常接受；资格验真随后报告四项
  mismatch：两个 status 与两个 candidate identity。

这是“协议可解析但语义资格不合格”，不能通过只检查 HTTP 200 或 schema valid 放行。
依照停线规则，本轮没有改 prompt、字段描述或真值。

## P0-04：最大允许形状在客户端输入预检即被阻断

P0 ID：`P0_V141_C5_D3_MAX_SHAPE_INPUT_BUDGET`

D3 的 4 Atom × 每 Atom 16 个候选形状按资格定义构造。当前精确估算为：

| 部分 | 数值 |
|---|---:|
| messages | 6570 tokens |
| schema | 983 tokens |
| 合计 | 7553 tokens |
| 当前输入上限 | 6144 tokens |
| user payload | 8411 bytes |
| schema | 866 bytes |

因此执行状态为 `REQUEST_REJECTED / PROVIDER_INPUT_TOO_LARGE`，Provider 调用数为 0。
当前 1280-token 输出预算和模型 8192 总上下文并不能使 7553-token 输入满足 6144 输入门禁。

任务书要求最大允许形状参与 D3，失败意味着当前 profile 声明的结构上界与实际输入预算
不能同时成立。后续只能在新批次中显式缩小已签发的能力上界，或版本化分批/短 ID 协议；
不得截断候选、默认缺失 Atom 已通过，也不得把“未发请求”写成 Provider 不支持该形状。

## D3 总账

| Case | Provider 调用 | 执行状态 | 资格结果 |
|---|---:|---|---|
| `AMBIGUOUS` | 1 | `SUCCEEDED` | PASS |
| `PARAPHRASE` | 1 | `TRANSPORT_FAILED` | FAIL |
| `NOT_FOUND` | 1 | `OUTPUT_INVALID` | FAIL |
| `TWO_ATOMS` | 1 | `SUCCEEDED` | FAIL（语义错配） |
| `MAX_SHAPE` | 0 | `REQUEST_REJECTED` | FAIL（本地预算） |

- D3：5 case、4 次真实 chat 调用、0 retry、1 pass。
- A/B 诊断：2 次真实 chat 调用。
- 本批 chat 总调用：6；业务调用：0。
- 在 chat 探针前另有 3 次 tokenizer endpoint 调用，只用于部署 tokenizer 计量，未混入 chat
  调用数。
- D3 safe SHA-256：
  `0fec7dd32a8b0684e5e990ec6f95e7f9f827e795746b0a760f0eecc153d04066`。
- D3 private SHA-256：
  `fcd7da7732e466b68430a19d8423c287f11c6b9744b0f13685a728bde205ac2c`。

## 计划外历史问题

以下两项在本轮起点或提交 A 的独立干净 worktree 同样复现，不是 D3 造成，也没有纳入本次
修复。按用户要求集中记录供后续决定：

1. `test_cache_hit_precedes_query_embedding_and_reranker`：首轮结果为
   `PROVIDER_UNAVAILABLE / GENERATION_OUTPUT_INVALID`，未形成可复用缓存，第二轮不能在
   embedding/reranker 前命中。
2. `test_certified_excerpt_keeps_each_atom_source_scope`：期望 A2 为 `SUPPORTED`，实际为
   `MISSING`。

没有删除断言、放宽门禁或修改非 C5 业务规则来制造通过。

## 停线、运行环境与清理

- 未构建新候选镜像，未部署 8289，现有 8289 始终保持基线
  `rag-test-wanshitong:wb08r03f-bd00c56`。
- 生产 60:8288 与 54:18288 未修改，健康检查为 200。
- 已确认并精确删除 60 上一个已停止三天、占用旧 8289 绑定的历史 WB08R02 容器
  `wanshitong-wb08r02-app` 及其精确镜像 `rag-test-wanshitong:wb08r02-9c3b029`；未删除
  volume，未执行全局 prune。该本地 Docker cache 项已不可从 cache 恢复，但可由源码重建。
- 其余 8289 基线容器/镜像保留，live/ready 均为 200。
- 原有 10 个未跟踪评测文件未修改，哈希与任务开始时一致。

## 本机私有证据

源证据目录：

`/home/jerry/work/RAG-private-evidence/wb08r03-v141-c5-protocol-4c981fa`

其中 `analysis/d3-analysis.private.json` 是机器可读因果摘要，SHA-256 为
`6069206b2ca97a4bdb8931b373ec56750570cfb49033b936749963a048791af6`；
`analysis/vllm-log-excerpt.txt` 是服务端时间窗摘录，SHA-256 为
`0b877a31ee179dd68664af43c578d08fed6a8ecb4a9780b19677190d1a5517fb`。

Windows 分析包在报告提交后组装并写入最终 Manifest。私有材料包含内部问题和请求正文，
不进入 Git。
