# WB08R-03G V14.2：完整问题与后续处理清单

## 状态定义

本轮已经完成计划实现、真实协议资格、业务五题、Preflight-16 和 A～F。用户明确把真实问答验收改为“先跑通并定位问题”，因此本文把问题分为三类：

1. 已闭合的计划内问题；
2. 已恢复的运行环境问题；
3. 不在本轮继续修改、但进入生产前必须处理的质量与终态问题。

当前没有触发“三类计划外新问题修复仍失败”的停线条件；也没有生产发布授权。

## 已闭合：计划内四类问题

### `PLAN-01`：单字段等义请求 8 秒超时

字段任务不再复用 Planner 的单一短时限。当前使用单次 12 秒 read timeout、字段阶段 15 秒总 deadline、512-token 输出上限；最终三轮资格的 `PARAPHRASE` 均完成，最后一轮通过。

### `PLAN-02`：`NOT_FOUND` 输出状态组合非法

模型说明现在区分“候选数组为空”和“候选非空但没有对应字段”，明确四种状态的候选数量；本地验证仍严格拒绝非法组合。最终资格 `NOT_FOUND` 通过，未增加自动修正。

### `PLAN-03`：双 Atom 被整句字段词污染

程序精确匹配改用当前 Atom 的可信片段。明确输入/输出双问在产品路径形成两个独立 `EXACT`，后置模型调用为 0；没有临时改写完整 `business_query`。

### `PLAN-04`：4×16 巨型单调用超过预算

改为每次一个 Atom、完整保留 16 个候选，逻辑请求内顺序合并最多 4 个 Atom；共享总 deadline，不重试。最终 `MAX_SHAPE` 通过，64 个候选无截断。

## 已恢复：运行环境问题

### `RUNTIME-01`：首次业务请求无法持久化 Trace

- 现象：第一次 F024 已发送，随后 Trace SQLite 权限中断，`five-case-c1/private-replay.ndjson` 为 0 字节。
- 处理：恢复 Trace 数据库挂载权限后重新启动正式批次；没有修改算法。
- 证据边界：该请求发生在 Trace 落库前，不能补造 Trace。业务请求总账保留这一次，214/215 有完整 Trace。

### `RUNTIME-02`：新候选未执行内部模型配置

- 现象：权限恢复后的五题全部返回 `CONFIGURATION_REQUIRED`。
- 原因：候选部署后遗漏仓库要求的内部模型配置命令；不是问答证据不足。
- 处理：执行正式配置命令两次，输出一致，证明幂等；随后同版本最终五题走通。
- 约束：没有修改生产模型服务、没有把配置错误映射成业务拒答。

按保守口径，上述两类占用 2/3 的计划外运行问题处理额度；未出现第三类仍需反复修复的问题。

## 进入生产前的高优先级问题

### `PROD-P0-01`：F048 被误报为 Provider 不可用

四次复现完全一致：

| 项目 | 观察 |
|---|---|
| Case | `WB08R-F-048` |
| 出现位置 | B、C、E formal、E latency |
| 请求时延 | 38559.85～38982.77 ms |
| generation | 33535～33891 ms |
| Provider | 四次均 `SUCCESS / OK` |
| wire 输出 | 24 项；1 项接受、23 项拒绝 |
| 拒绝原因 | 23×`UNKNOWN_ATOM`，模型输出了未发送的 A2/A3/A4 |
| 最终原因 | `SEMANTIC_REVIEW_DEADLINE_EXHAUSTED` |
| 对外状态 | `PROVIDER_UNAVAILABLE` |

影响有两层：模型越过本次只发送 A1 的 wire 范围；系统又把复核 deadline 映射成 Provider 不可用。进入生产前应分别修复生成约束/消费隔离和终态分类，不能只延长 timeout，也不能把 23 个非法条目放行。

### `PROD-P0-02`：Full-96 存在错来源

E Gate 共记录 9 个 `WRONG_SOURCE_ANSWER`：

- formal：F013、F036；
- natural：N053、N058、N059；
- latency：N003、N005、F013、N059。

这是生产替换硬门，不因答案非空或带引用而降级为通过。完整问题、引用、回答和 Trace 均在分析包中。

### `PROD-P0-03`：错误拒答与 answer/source 缺口

- A：1 个 false refusal。
- B：F048、N059 的 `ANSWER_OR_SOURCE` 失败。
- C：18 项检查失败，涉及 F017、F048、N048、N050、N052、N053、N057、N058、N059、N060；6 个 false refusal。
- D：N003、N033、N052、N053、N059、N060 的 `ANSWER_OR_SOURCE` 失败；2 个 false refusal。
- E：7 个 false refusal；answer/limited rate 0.5806。

这些结果没有在本轮被“安全拒答”掩盖。建议进入 04 后按失败类别建立影子对照，不按题号逐条写特判。

## 尚未完成的人工真值工作

Preflight 与 A～F 的 observation packet 均为 `OBSERVED`，但本轮没有执行人工 semantic review。各 summary 中的 `MISSING_REVIEW` 是人工复核缺口，不是模型自动判定的失败，也不能被解释为准确率通过。

后续人工审阅应优先覆盖：

1. 9 个错来源检查；
2. F048 四次重复 Trace；
3. C、D 中重复出现的 N052/N053/N059/N060；
4. 五题中 F024 的拒答和危险时序题的限定忠实度；
5. F034 无证据控制题。

## 04 的建议处理顺序

04 可以开始 Shadow Routing，但保持只观察、不改变线上答案：

1. 先把 F048 的模型 wire 越界、语义复核 deadline 和终态映射拆成独立指标；
2. 用现有完整 Trace 聚类错来源、错误拒答和 answer/source 缺口，避免按题号补丁；
3. 对成对反例做人工真值复核，分别报告来源正确性、答案相关性和系统失败率；
4. 只有质量门禁重新通过且用户单独授权后，才讨论替换生产。

## 证据导航

分析包中的关键入口：

- `00_README.md`：证据范围和阅读顺序；
- `runtime-evidence/all-requests.private.ndjson`：214 条问题、回答、观察和 Trace 映射；
- `runtime-evidence/full-traces/`：214 条 canonical 完整 Trace；
- `runtime-evidence/gate-overview.safe.json`：各批终态、失败检查和复核缺口；
- `runtime-evidence/provider-unavailable-diagnosis.safe.json`：F048 四次机器可读归因；
- `runtime-evidence/request-accounting.safe.json`：215 次业务请求及 1 次无 Trace 缺口；
- `qualification/`：三轮真实协议资格 safe/private 结果；
- `source/`：源码提交 patch、变更清单和提交信息；
- `MANIFEST.sha256`：最终压缩包目录内文件完整性清单。
