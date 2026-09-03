# P09 Job、幂等与 Writer Lease

写请求先规范化为 canonical request hash，再以
`scope + operation + Idempotency-Key` 持久声明结果身份。同 Key 同请求返回原结果；
同 Key 异请求返回 `IDEMPOTENCY_CONFLICT`。

DocumentVersion 由 document ID 与内容摘要确定，IndexRevision 由知识库、完整版本快照
和 index fingerprint 确定。Job ID 绑定目标 Revision，而不绑定调用方幂等键。因此两个
不同 Key 指向同一 Revision 时只会有一个 Job 和一个 Writer。

P09 复用 P08.5 的 SQLite Revision Writer Lease 和 fencing。构建状态、stage、attempt、
安全错误、retryable、lease owner 状态及 primary/standby slot 进度均从持久控制面读取，
不向 API 暴露 fencing token。组合根启动时先恢复 stale Job/Lease，P07 再从持久
embedding cache 回填 Active Revision 的本地向量状态。

同步 SDK 方法会先持久化无正文 `ingestion_requests` 和 Job，再返回 queued/running Job；
有界单进程 Worker 领取后调用 P06 Builder。默认 Worker 并发上限为 1，queued/running
持久请求总量上限为 64，超限返回可重试的 `QUEUE_LIMIT_EXCEEDED`。进程内 Future 只负责
唤醒，SQLite 队列才是恢复事实源。启动时 stale interrupted/running 请求回到队列，跨
重启继续执行。关闭 runtime 时停止接收新任务并等待已领取任务写入终态。

取消只允许非终态 Job。queued 请求可原子取消；running 请求写入持久取消标记，Builder
在 parse、chunk、slot、validation 和 activation 阶段边界检查。重试只允许
`FAILED_RETRYABLE`，最多 3 次尝试，并复用原请求、Artifact、Revision 和缺失 Embedding
状态；P09 不执行不可见的自动重试，调用方可按 Job 的 `retryable` 和安全错误安排退避。
所有阶段写入与激活仍受 Lease、状态机和原子 Active pointer 保护。

Document/KB 进入 deleting 时，会在创建生命周期计划的同一 SQLite 事务中取消 queued
请求并标记 running 请求；激活写事务还会再次校验取消标记，关闭检查与原子切换之间的
竞态窗口。

GC 使用独立持久 Plan 和 item checkpoint。删除 API 只创建生命周期计划，不绕过
Tombstone、引用计数、快照漂移检查或 Filesystem Reconciliation。
