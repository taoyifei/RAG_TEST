# Chunk Schema V3 迁移

## 兼容边界

V3 的 canonical 正文字段是 `citation_text`。`Chunk.text` 仅为只读兼容属性，返回同一
字符串；代码不得同时维护两份可变正文。旧 HTTP/API payload 在迁移完成前仍可由 legacy
adapter 输出原 `text` 字段。

本阶段不修改现有 active collection、生产 alias、SQLite/Qdrant schema 或公共 HTTP/SDK
schema。Parser、Chunker、TokenCounter、chunk payload schema 或策略的变化都会改变
`index_fingerprint`，因此 V3 数据必须在 P06 创建新 `IndexRevision`，不能写入旧 active
collection。

## 双向适配

`legacy_chunk_to_core()` 把旧基础字段映射到 Chunk V3：

- `text -> citation_text`；
- 旧 span 转成有 SourceAnchor 的 original span；
- 旧 span 未覆盖的换行或间隔转成不可引用 separator；
- 文件显示路径不会进入 Core 稳定身份，并返回 `LEGACY_FILE_PATH_OMITTED` warning。

`core_chunk_to_legacy()` 显式把基础 V3 字段降级为旧 Chunk。调用方必须提供只用于显示的
文件名。原文与重复来源可以映射；旧 payload 无法表达的派生编号、separator、child groups、
note refs 或复杂结构会返回以下损失 warning：

```text
CHUNK_V3_SPAN_NOT_EXPRESSIBLE_IN_LEGACY
CHUNK_V3_RELATIONSHIPS_NOT_EXPRESSIBLE_IN_LEGACY
```

如果没有任何旧 payload 可表达的来源跨度，适配器直接失败，不生成伪来源。基础文本的
legacy -> V3 -> legacy 回归由自动测试覆盖。

## 迁移顺序

1. Registry 显式注册 `docx-structural-v3`；默认离线与 hot-standby profile 选择 Parser v4
   和 Chunker v3。
2. P05 只验证 `DOCX -> DocumentIR -> Chunk[]` 离线闭环和 legacy 读取基础字段。
3. P06 建立承载 V3 payload 与两个 named vectors 的 staging revision；不得原地覆盖旧索引。
4. 两个 required embedding slot 覆盖、维度、schema 和抽样读回全部通过后才能原子激活。
5. 旧 Query/index 在迁移期通过 adapter 读取基础字段；需要复杂表格、精确跨度或关系扩展的
   路径必须读取 V3 revision，不能把不可表达字段塞回旧 collection。

## 指纹要求

Index fingerprint 覆盖 Chunker descriptor/version、完整 `ChunkingPolicy`、TokenCounter ID、
exact/estimated 标志与模型兼容性、required slot 上限和 chunk payload schema version 3。
纯文件重命名不改变 document/chunk 稳定身份；内容、结构、策略或跨度变化必须创建不同 ID
和 revision。

## 回滚与安全

本阶段没有删除 legacy 代码、迁移数据库或切换生产 alias，因此回滚只需继续使用旧 Profile
和旧 active revision。不得通过 force push、覆盖旧 collection 或混用 primary/standby
向量空间完成回滚或迁移。
