# Blob 与 ParsedArtifact 生命周期

Parser 只返回 content-addressed ParsedArtifact，不接触 Store。应用层验证 Artifact 后调用
Filesystem Blob `put_if_absent`，明确区分 CREATED 和 EXISTING。

新 bytes 先写入受控临时文件，flush/fsync 后用原子 hard link 发布；竞争者若已经发布同一
digest，则复核 size 与 hash 后返回 EXISTING。物理对象随后以 staged 写入 SQLite。一个
Parser 结果的全部 Blob references 在单个 SQLite 事务中提交，同时将对应对象标为
available。

source_document 引用属于 DocumentVersion，不属于 Revision；parsed media 引用属于具体
Revision。多个逻辑文档可共享同一物理 Artifact，但引用表分别记录所有 owner。业务回滚
不直接删除持久化 CREATED 对象，避免误删并发建立的共享引用。Blob 创建后、引用提交前的
崩溃会留下 staged orphan，只能在宽限期后经绑定快照的 GC plan 回收。

删除前 SQLite 写事务重新确认没有引用并标记 quarantine。物理删除失败会恢复 staged，
原始异常不会被 cleanup failure 覆盖。Active、受保护 Retired、运行中 Job、DocumentVersion
和任何 Blob reference 都受保护。
