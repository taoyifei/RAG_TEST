# 本地索引生命周期

## 初始化和导入

```bash
python scripts/dev.py init-data --profile configs/profiles/dev-p06-memory.json
python scripts/dev.py ingest <kb-id> <docx> --document-id <logical-id>
```

`ingest` 把新文档与当前 Active 的其他逻辑文档组成完整 KB snapshot。显示名变化只更新
document 元数据；相同 document 和 bytes 使用同一 dver，并且不会因默认幂等导入创建新
Revision。ParsingPolicy 或 index contract 变化会创建新 Revision，但仍复用同一 dver。

## 查看、校验和回填

```bash
python scripts/dev.py job <job-id>
python scripts/dev.py index-info <kb-id>
python scripts/dev.py index-validate <revision-id>
python scripts/dev.py index-backfill <revision-id> --slot <slot-id>
```

`index-validate` 会先从持久化 cache 恢复完整 Point，再从实际 Store 重算激活条件。
`index-backfill --slot` 校验指定 slot 存在，但为避免 Qdrant 普通 upsert 清空另一 Named
Vector，仍一次写入每个 Point 的全部 required vectors。

## GC

```bash
python scripts/dev.py index-gc --plan
python scripts/dev.py index-gc --apply <plan-id>
```

GC 默认 dry-run。Plan 绑定 database identity、Active/Retired、运行中 Job、Blob 引用、
Vector namespace、Embedding cache 和候选快照。Apply 前重算；任何漂移均拒绝。Qdrant
collection 删除失败时 SQLite 权威 Revision 不会被伪装为已删除。
