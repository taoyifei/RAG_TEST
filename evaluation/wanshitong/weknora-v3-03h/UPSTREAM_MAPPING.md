# V3-03H 上游映射与合同冻结

- 本方起点：`9b366520453229e5b05770ae350ac9fe27ced69b`。
- 原固定上游：WeKnora v0.8.0 `1edcd54b43606d9079bb36650efe3f68707a79ea`。
- 本轮审查上游：WeKnora v0.8.2 `3e8b0bfc80b845b2d4b2ed683994748741450a97`。
- 来源范围修订：`wk-source-scope-h1`；引用协议：`wk-ref-h1`；自然链修订：`weknora-natural-v3-03h`。公共 SSE 事件名、sequence 和终态合同不变，故不升公共协议版本。

## 选择性映射

| 上游位置 | 本方采用的行为 | 边界 |
| --- | --- | --- |
| `internal/types/search.go` 的 `SearchTargets` | 在原问题、当前 KB 与目录快照下，生成一次范围决策；改写不能改变该范围。 | 不移植上游多 KB、标签和 Agent 路由。弱文档措辞只是检索提示。 |
| `internal/modelcontext/registry.go`、`README.md` | 仅为本轮实际送模材料登记 `cN`，持久身份留在服务端。 | 不移植 `dN/bN/wN`、工具参数、MCP、历史导航注册或资源句柄。 |
| `internal/modelcontext/citations.go`、`stream.go` | 系统专用 `<ref id="cN"/>` 协议；流式缓存可能未完成的标签，完整标签才核验并输出公开引用编号。 | 未知、畸形、伪造公开标签和旧 `[S1]` 不能成为新协议引用。 |
| `chat_pipeline/references.go`、`chat_completion_stream.go` | 引用证据只来自当前材料，增量与最终状态独立处理。 | 保留湾事通已有 SSE、受控引用卡、会话和 `citation_binding_only`。 |

旧 `[S1]` 检查函数只保留给旧回放／调用方。新自然链每轮独立建 registry，不能把两种模型协议拼入同一请求。缺失或无效引用仍只保留受控草稿。
