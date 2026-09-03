# 0008 Revision writer lease and fencing

## 决策

同一个 deterministic Revision 同一时间只能有一个数据库确认的 Writer。
Migration 0007 新增 `revision_build_leases`，以 `revision_id` 为主键，绑定
`owner_job_id`、单调递增 `fencing_token`、UTC acquire/heartbeat/expiry 时间和
状态。

Builder 在写入任何 Revision 派生状态之前取得 Lease，默认租期为 300 秒。
同一 Owner 可续期；未过期的其他 Owner 被拒绝；过期 Lease 可由新 Owner
接管并递增 token。释放只允许当前 Owner 和当前 token 更新。

## Fencing 边界

SQLite adapter 在 canonical Chunk、Revision 文档、FTS、Embedding 状态、
向量证据和激活相关写入前验证当前 token。旧进程即使仍在运行，也会因 token
过期或已被接管而 fail closed。Job heartbeat 同步延长所持 Lease，避免正常
长任务被误接管。

数据库 Trigger 同时验证 Lease Owner 的 Job 确实指向同一 Revision。Lease
不替代 Scope、状态机或 Vector inventory 门禁，三者都必须通过后才能激活。
