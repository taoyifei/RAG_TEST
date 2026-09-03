# 0009 Resumable GC

## 决策

GC 采用持久化 Plan 与逐项状态机。Migration 0009 新增 `gc_plan_items` 和
`blob_reconciliation`。Plan 绑定数据库 identity、候选快照与 plan hash；
执行时逐项 claim，进程中断后从已记录状态继续，不重新猜测删除意图。

Revision item 的状态顺序为：

```text
planned -> claimed -> vector_deleted -> sqlite_deleted -> completed
```

Blob item 使用 `blob_reconciled` 后进入 `completed`。可恢复错误记录为
`failed_retryable`，保留安全错误文本和 attempt。再次执行同一 Plan 时只处理
未完成项。

## 删除前再校验

每次不可逆步骤前都重新读取保护状态。Active Revision、Running Job、未过期
Writer Lease 或快照不匹配会使该项拒绝删除。候选覆盖已退休 Revision 以及
没有活跃 Writer/Lease 的失败、可重试或中断构建；正在写入的数据不属于候选。

## Blob 对账

Filesystem CAS adapter 只扫描受控根目录中的规范 SHA-256 路径，拒绝 symlink，
并重新计算物理文件摘要。物理存在但 Catalog 缺失的对象进入 quarantine 记录；
Catalog 存在但物理缺失记为 corrupt；两侧一致记为 verified。对账输出不包含
文件正文或主机绝对路径。

本阶段只证明本地临时文件系统和 SQLite 状态机的恢复语义，不代表远程对象
存储或生产保留策略已经验证。
