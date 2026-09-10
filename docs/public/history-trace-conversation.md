# History、Operational Trace 与会话边界

## 两类记录

Product Query History 和 Operational Trace 使用不同存储，但共享同一个公开
`trace_id`：

| 记录 | 保存内容 | 默认保留与访问 |
| --- | --- | --- |
| Query History | 加密问题、答案、结果、引用、usage 和用户历史元数据 | 默认 7 天；读取正文时重新检查 owner、Project、KB、活动 Revision 与来源 |
| Operational Trace | 根 Trace、Span、候选决定、Provider 安全元数据、Reason 与显式 Artifact | 独立 TTL；不保存问题、答案、Prompt、模型 delta、Cookie、Authorization 或思考过程 |

`trace_<32hex>` 是当前公开格式。读取与导出同时接受旧 `<32hex>`；同一批请求中
同时给出两种等价写法会作为重复项返回 422。

## 支持包和技术导出

管理员接口：

```text
GET  /api/v1/admin/history-traces/{trace_id}/export
POST /api/v1/admin/history-traces:export
GET  /api/v1/admin/operational-traces/{trace_id}/export
POST /api/v1/admin/operational-traces:export
```

History 支持包始终为 ZIP，包含 `MANIFEST.json`，以及每个 ID 各自的
`history.json` 和 `operational-trace.json`。批量最多 100 个 ID；成员数、单成员、
单项、总原始字节与压缩字节均有硬上限，缺失或超限时整包失败，不静默截断。
成员路径、时间戳、权限、压缩方法和 JSON 排序固定；清单记录每个成员的 SHA256、
字节数和正文/Trace 可用状态。

默认 `include_history_body=false`，只导出安全元数据。只有管理员明确选择正文、
记录仍在保留期内、正文保存策略允许且 Project、KB、Revision 与引用来源仍可读时，
才包含问题、答案、result 和 evidence。否则仍生成 metadata-only 包，并写明
`body_unavailable_reason`。正文不会复制回 Operational Trace、普通日志或缓存。

旧 flat event 导出标为 `legacy-flat-1` 和不完整；V3 前只有 History 的记录使用
`missing-pre-v3` / `NOT_CAPTURED_BEFORE_V3` 占位，不伪造 Span、候选或耗时。

## 清理语义

`DELETE /api/v1/history` 只删除 Query History 与旧 flat events，不删除独立
Operational Trace、源文档或索引。管理员可在 Operational Trace 页面执行到期
prune；在途支持包 lease 会阻止相关记录被清理，避免得到半包。

## Conversation

不传 `conversation_id` 时保持单轮行为。传入后，服务端身份绑定 owner、Project、
KB 与 conversation ID；ID 最长 128 字符。默认空闲 TTL 为 24 小时，最多保留 8
轮，单个问题最多 2000 字符，会话问题与已验证事实摘要合计最多 16000 字符。
正文使用现有主密钥加密；只保存用户问题、已验证 claim 的最小摘要以及独立的
Revision/Document/Version/Chunk 支持身份，不保存 Prompt、模型 delta 或 CoT。

下一轮前会重新验证活动 Revision 和来源。来源删除、撤权或 Revision 不兼容时，
相应事实摘要被丢弃，但历史问题不会被伪造成仍受支持的事实。同一会话串行执行；
只有唯一成功 final 才幂等提交。取消、失败、断连或 partial 不提交；REFUSED 只保存
问题而不保存 claim。切换知识库或退出不会串用会话。

## Feedback、singleflight 与缓存

终态 ANSWERED/REFUSED 查询可提交 `useful`，无用时可选 `INCORRECT`、
`INCOMPLETE`、`WRONG_SOURCE`、`OUTDATED`、`TOO_SLOW` 或 `OTHER`。canonical
反馈表只保存 scope、布尔值、有限原因码和投影状态；不保存自由文本、问答、证据、
IP 或凭据。Operational Trace 的 `feedback_useful` 是可恢复筛选投影。

singleflight 仅用于非流式 SAFE 请求。等价键覆盖 owner、权限、Project、KB、活动
Revision、检索方案、serving/cache identity、原问与已接受语义/变体、会话摘要、
过滤器、limit、相关内容和生成行为。共享的是底层计算结果；每个请求仍分配独立
Trace ID 并写入独立 History/Trace。流式、DIAGNOSTIC、FULL 或身份不同的请求不
合并。单个 follower 取消不影响其他订阅者，全部订阅者取消才尝试协作取消上游。

进程内结果缓存默认 TTL 300 秒，使用 LRU，并同时限制为 512 条和约 64 MiB；大小
估算包含 answer、evidence 与 related content。缓存不持久化明文，关闭后拒绝写入，
安全指标只暴露数量、估算字节、命中、未命中、淘汰和过期计数。
