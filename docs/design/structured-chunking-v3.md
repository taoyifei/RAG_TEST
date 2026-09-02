# Structured Chunking V3 设计

## 范围

`docx-structural-v3` 只消费 `docx-ooxml-v4` 已生成的 `DocumentIR`，输出
canonical `Chunk` V3。Chunker 不重新打开 DOCX ZIP，不导入 lxml 或 python-docx，
也不访问网络。它负责结构规划、三视图渲染、Token 打包、来源跨度和最终不变量；
Parser、Embedding、索引持久化与检索质量调优不在本阶段。

默认参数为 `target=384`、`hard_max=512`、`overlap=64`、`min_tail=64`。
这些参数带有 `provisional=true`，P08 才能结合冻结数据集和真实 Provider 冻结。

## 数据流与边界

```text
DocumentIR
  -> SectionPlan
  -> Run
  -> AtomicUnit + ordered SourceFragment[]
  -> semantic split（仅超长 atom）
  -> ordered pack（仅同 run）
  -> citation / embedding / lexical render
  -> token + source + neighbor validator
  -> Chunk[] + ChunkingReport
```

- 标题开启 section；标题前内容进入稳定 root section。
- 主正文、表格、notes、图片 metadata、页眉页脚、Text Box、Comments 使用独立 run。
- pack 不跨 section、run 或 neighbor group，不重排、不删除 atom。
- neighbor 只连接同一 document version 和 group 的连续 chunk，链接必须双向且无环。
- 表格以完整 row 为 atom；nested table 是独立 child group，外层只保存 child group ref。

## Chunk V3

Canonical 字段包括 project、knowledge base、index revision、document version、role、
parent、section、neighbor、previous/next、child groups、note refs、三种文本、heading
path、identifiers、token 元数据、内容摘要和 metadata。`Chunk.text` 是只读属性，返回
`citation_text`，因此没有第二份可变正文。

`chunk_id` 由以下内容规范化生成：

- document version ID；
- chunker descriptor、policy 与 TokenCounter 构成的 fingerprint；
- role、parent、neighbor group；
- 有序 SourceSpan；
- citation text SHA-256。

显示文件名、绝对路径、向量和 active revision pointer 不进入 ID。纯重命名保持 ID；
内容、结构、来源跨度或策略变化会改变 ID。

## SourceSpan

`citation_text` 由有序、无间隙、无重叠的跨度完整覆盖：

| 类型 | 来源要求 | 可引用 | 用途 |
| --- | --- | --- | --- |
| `ORIGINAL_TEXT` | node、SourceAnchor、等长 source/chunk range | 是 | 原始可见文本 |
| `DERIVED_NUMBERING` | paragraph node、编号 metadata；无伪造 source range | 是 | 列表自动编号 |
| `REPEATED_CONTEXT` | 真实 node、SourceAnchor、等长 range；`is_repeated=true` | 是 | overlap、表头或纵向合并锚点 |
| `SEPARATOR` | 不含 node、anchor 或 source range | 否 | 换行和结构分隔符 |

所有非 separator 字符必须映射到真实节点。Embedding-only 标题、位置和表头上下文
不进入 citation spans。Quote validator 只允许完整落在单个可引用 span，或可无歧义地
连续映射到同一来源的 quote；跨 separator、来源跳转或 embedding-only 上下文不得发布。

## 三种文本

`citation_text` 保留逐字来源关系，不做 NFKC。段落使用双换行，列表项使用单换行，
表格按稳定的 `列: 值` 行序列化。列表标签使用派生编号 span；重复表头和纵向合并
锚点使用 repeated span。

`embedding_text` 在 citation 之外添加确定性、受预算控制的文档标题、标题路径、类型、
列表路径或真实表头上下文。它不添加 Jina 的 query/document task，也不添加 Qwen 的
`text_type` 或 instruction；这些是 Provider adapter 的职责。

`lexical_text` 从 embedding view 独立生成，执行 Unicode NFKC、casefold 和空白/标点
规范化，并提取 `P00001`、`GB/T 1234-2025`、`ABC-01` 一类 identifier。规范化结果
不能反向替代 citation 原文。

## Token 与打包

`TokenCounterPort.count()` 返回 count、tokenizer ID、exact 标志和模型兼容性。实现包括：

- `DeterministicUtf8TokenCounter`：离线测试的确定性权威计数；
- `HuggingFaceJsonTokenCounter`：只读取显式本地 `tokenizer.json`，禁止下载；
- `ConservativeEstimatedTokenCounter`：无精确 tokenizer 时按安全余量保守估算。

最终 chunk 同时复算 citation 与 embedding，记录二者最大值。有效上限为
`min(hard_max, profile_hard_cap, all required slot max)`；默认 required slots 是
`primary=32768` 和 `standby=131072`，所以当前 provisional profile cap 512 更严格。
任一 required slot 超限都在报告中计数，并由 validator 阻止继续进入 embedding 阶段。

同一 run 的 atom 顺序装包。加入下一 atom 不超上限时，选择与 target 距离更小的边界，
距离相同选择较早边界；短尾仅在不跨边界且不超上限时并回前块。超长 atom 按双换行、
换行、句末、分号、逗号、空白、hard cut 的优先级拆分，并保证严格前进。Overlap 只复制
上一段末尾的完整句或行且不超过 cap；找不到完整语义后缀时不复制。

## 结构策略

- 列表按 numId、level 和 restart group 保留身份，派生编号进入 citation。
- 表格按 row 打包；cell 坐标、gridSpan、vMerge 和 omitted cell 留在 metadata/span anchor。
- notes 默认独立 `NOTE` chunk，正文只保留 note refs；orphan note 仍可索引并标记。
- 图片仅在 alt/caption 具有语义时形成 `IMAGE_METADATA`；media bytes 和 OCR 不进入 Chunk。
- Header/Footer 与 Comments 默认 metadata-only，可由明确 policy 生成独立低权威 chunk。
- Text Box 是独立 story/run，不与主正文拼接。

## 报告与质量边界

`ChunkingReport` 聚合角色计数、token p50/p95/max、exact/estimated、来源覆盖、重复字符、
表格 row/cell、列表标签、orphan、稳定 ID、required slot 上限、warning 和耗时。报告只验证
结构与安全不变量，不能证明真实检索质量。结构消融复用同一 IR snapshot 比较三个候选，
不调用真实 Embedding，不选择最佳候选。
