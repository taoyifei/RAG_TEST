# Phase 09 Progress: Lifecycle API, SDK and Jobs

## 集成身份

- Integration base SHA: `ae82ee1952908593c03ade646b7278d964d36826`。
- Feature branch: `codex/p09-api-sdk`。
- Integration branch: `feature/universal-rag`。
- P08.5 merge: `2d2eeecbced072c6801b9d89fb1ebfb85dc28e94`。
- Feature commits before documentation: `beb0cf4`, `259e1fd`, `c7f0c14`,
  `a081097`。
- Documentation commit: 本文件所在提交；完整 SHA 在集成证据更新中补录。
- Integration merge commit: 待 `--no-ff` 合并后补录。
- Remote SHAs: 待推送并以 `git ls-remote` 核验后补录。
- `main` 与 `Industry` 保持只读。

## 交付范围

- Migration 0010 增加 Project、Knowledge Base、Document、DocumentVersion
  生命周期状态、持久幂等记录、可恢复 ingestion request 和受控删除计划。
- SQLite 持久层在事务中再次执行全局 `document_id` scope 约束；相同字节可共享
  Artifact，但不同逻辑文档保持不同 dver、Node 和 Chunk 身份。
- Lifecycle Application Service 复用 P06 Revision Builder、P08.5 Writer Lease、
  Scope Integrity、Vector Inventory、FTS V2 和 GC，不在 Router 重写解析或检索。
- 新文档、新版本和 Rename 是三个独立操作。新版本复用当前显示名；Rename 不产生
  dver 或 Revision。构建通过后才原子切换 Active Revision。
- 上传先写入数据根内的受控临时文件，执行 32 MiB 默认上限、扩展名和媒体类型检查，
  并在成功或失败后清理；API 不接受客户端文件路径。
- Job ID 由完整目标 Revision 决定。不同 Idempotency Key 指向同一 Revision 时复用
  一个 Job/Writer。SQLite 是队列事实源，单进程 Worker 默认并发 1、队列上限 64，
  支持重启恢复和阶段边界取消。
- 删除 Document/KB 只写 `deleting` 与 `lifecycle_operations`，并在同一事务取消未完成
  Job；不直接删除 Blob、Qdrant Collection 或任意路径。
- 同步 `RagSdk` 与 `/api/v1` HTTP API 共用 Lifecycle/Retrieval Services；公开响应
  返回实际 Revision、双指纹、slot/vector、route/rerank、cache、Evidence 和有界诊断。
- 完整 RetrievalDiagnostics 仅在显式 debug 开关和 Admin 权限下读取；脱敏 TraceEvent
  可由 Admin 按 trace/query/job 身份读取。
- OpenAPI v1 快照包含 21 个 path；旧 `/api/chat` 与 P00—P08 Adapter 未删除。

## P08.5 合同与质量边界

- Active Revision 查询强制读取 FTS V2；旧 FTS V1 返回 `REINDEX_REQUIRED`，不在请求
  内原地升级。
- `INDEX_CORRUPT`、`INDEX_NOT_READY`、`CHANNEL_UNAVAILABLE`、
  `PROVIDER_UNAVAILABLE`、`POLICY_DENIED`、`DENSE_UNCALIBRATED` 和
  `CONFLICT_ACTIVE_WRITER` 保持不同错误码；corruption 不返回普通 200。
- Cache Hit 测试确认 `provider_call_count=0`；公开响应不含完整候选或 Diagnostics。
- Evidence 使用 P08.5 query-aware 最小支持集；P08 历史 `evidence-cap-8` 不作为 P09
  默认优选声明。
- 本阶段未执行真实 Jina/Qwen Calibration，状态保持
  `remote_dense_confidence_calibrated=false` 与
  `REMOTE_PRODUCTION_PROFILE_READY=false`。

## 门禁证据

- Opening default-offline check: `1321 passed, 75 deselected, 4 warnings in
  210.42s`。
- P09 API/SDK/Application 定向门禁: `17 passed, 1 warning in 15.18s`。
- Required `tests/api tests/sdk tests/application`: `99 passed, 1 warning in
  24.67s`。
- Migration 断言修订专项: `2 passed in 1.10s`。断言继续精确要求 P08.5 版本 6—9，
  并新增 P09 version 10，不放宽 checksum drift 检查。
- Final compileall、全量 Ruff、strict mypy over 290 files、Google docstrings 通过。
- Final default-offline check: `1338 passed, 75 deselected, 4 warnings in
  229.18s`。
- Final smoke: `71 passed, 1 warning in 8.30s`。
- OpenAPI snapshot check、API smoke、SDK smoke 与 `git diff --check` 通过；两条 P09
  smoke 均报告 `network_calls=0`。
- External services actually called: none。

统一门禁曾因新增 migration 10 使两个旧“总数/最后四个版本”断言过期而得到
`1335 passed, 2 failed, 75 deselected, 4 warnings`。修订为同时精确校验 P08.5
版本 6—9 和 P09 版本 10 后，完整门禁如上转绿。

## 决策与剩余风险

- HTTP 新文档的 `display_name` 使用 UTF-8 查询参数，避免非 ASCII Header 在标准客户
  端编码阶段失败；新版本入口不接受显示名，Rename 是唯一改名入口。
- JobRunner V1 是单进程有限并发实现；SQLite 队列、Job、Lease、slot progress 和
  GC 是可恢复事实源，进程内 Future 仅负责唤醒。
- P06 stale running Job 的接管窗口仍为 5 分钟；这是 fencing 安全窗口，不伪装成
  即时接管。Starlette TestClient 弃用提示为现有依赖 warning。
- 没有真实 Provider、Remote Qdrant、企业文档或生产负载证据；远程质量与生产就绪
  保持 false。

## 状态

阶段分支门禁已通过；以下集成状态将在 `--no-ff` 合并、合并后复验和远程 SHA 核验后
更新：

```text
P08_5_CONTRACTS_CONSUMED: true
REMOTE_PRODUCTION_PROFILE_READY: false
P09_READY: false
```
