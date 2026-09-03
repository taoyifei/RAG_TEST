# P08.5 Schema and reindex migration

## 不可变起点

0001—0005 内容未修改。阶段开始与完成前复核的 SHA-256 为：

| Migration | SHA-256 |
| --- | --- |
| `0001_control.sql` | `3d89ee3bd0a9e5aeaa2b630cd92f45589d1968a8fd43d85377a7153f3b658d0b` |
| `0002_artifacts.sql` | `dc1e0d00d230634ad1c65df688808330ad98c82e18377a1f04778918aa60a374` |
| `0003_revisions_chunks.sql` | `8661cc87f85b75ec29979b40c8fc2c54df06fb1f347c8d410265384b941232c6` |
| `0004_fts5.sql` | `af20e249520295263f1592da806db84aff2ad0fad4ab9dd3f53e1396c8005e49` |
| `0005_embedding_cache_gc.sql` | `459d3fc22f5952f608155ccd73f34cc40bad42db98289d15ec4881507d918c7e` |

## 新增 Migration

| Migration | 作用 | SHA-256 |
| --- | --- | --- |
| `0006_scope_integrity.sql` | 复合唯一索引、Scope/Version/Active Pointer Trigger | `1263cdd9668e16c15ec88ca1663aef8db8b9e4894da1dbd900a42136d6b7354b` |
| `0007_revision_writer_lease.sql` | Revision writer lease、Owner 约束与 fencing token | `8dc85dd66866701e1a0295184cd5b82a3d2a899515ff6c4ea0620b648c23c2b5` |
| `0008_fts_analyzer_v2.sql` | 新建 `chunks_fts_v2`，不修改 legacy FTS 表 | `c0a248daa52aab8dfaeed08f4e7495bb7cfbf61c7ea4932b4cea1a15a94ad883` |
| `0009_resumable_gc.sql` | 持久 GC item 状态与 Blob reconciliation | `182ab2889a9e82967e4dfcf777b949f560177af5de9df8e2f661dcf58deff74b` |

Migration 按文件名顺序在单库执行；已记录 checksum 不允许变化。新 Trigger 会
使历史脏数据在后续写入时 fail closed，因此正式迁移前必须先做只读一致性审计。

## FTS V1 识别和重建

Revision 的 `lexical_schema_json.fts_schema_version` 为 2 时读取
`chunks_fts_v2`。字段缺失或值为 1 且未声明 V2 component identity 时，只允许
legacy reader 读取 `chunks_fts`。未知版本、损坏 JSON 或不完整 V2 identity
分别返回 `REINDEX_REQUIRED` 或 corruption，不自动回退。

重建步骤：

1. 保留旧 Active Revision，不修改旧 FTS 行；
2. 用相同 canonical DocumentVersion 创建新的 deterministic Revision；
3. 使用 Lexical Analyzer V2 写入 `chunks_fts_v2` 和完整 schema identity；
4. 完成 SQLite、Blob、Vector inventory 和 writer fencing 验证；
5. 新 Revision 进入 ACTIVE 后，原子更新知识库 Active Pointer；
6. 旧 Revision 进入保留期，之后才可由可恢复 GC 处理。

本阶段只对合成、临时数据库执行迁移。若发现正式使用的 P06/P07 数据，必须
暂停并制定独立迁移方案，不能把重建当成已获授权。
