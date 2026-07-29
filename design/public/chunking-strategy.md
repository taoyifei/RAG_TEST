# DOCX section-aware chunking v2

本文说明当前 DOCX 分块的可追溯契约、结构消融方法和定参边界。当前
`384/512/64` 仍是候选，不能仅凭结构报告冻结；生产参数必须由真实
embedding/reranker 的 tuning 结果确定，再用隔离的 holdout 做最终验收。

## 1. fixed length 只作为 hard cap

固定长度窗口不知道标题、小节、段落、表格行或图片边界，会把引用位置和语义
边界混在一起。token 数在本实现中只承担两项职责：

- `target_tokens` 用来在完整原子单元之间选择更接近目标的局部边界；
- `hard_max_tokens` 防止任何一次模型输入无界增长。

正常分块不能为了凑齐 target 跨小节、跨表格或跨图片。只有一个原子内容自身
超过 hard max 时，才允许在原子内部切分。

## 2. 四层结构

section、run、atomic、span 从外到内约束分块：

1. **section**：标题只开启小节，不生成可检索 chunk。标题前正文进入稳定 root
   section；重复标题由 `heading_index` 区分。一个普通正文 chunk 只能属于一个
   section。
2. **run**：同一 section 内连续正文和列表项组成 TEXT run；每张表独立形成
   TABLE run；每张成功或低置信 OCR 图片独立形成 OCR run。表格、图片和失败 OCR
   都会终止相邻 TEXT run。
3. **atomic**：段落、列表项、表格行和 OCR 行是首选原子边界。短原子可以按原序
   合并，不能删除、改写或重排原文。
4. **span**：每个 chunk 保存有序 `source_spans`。span 记录 element ID、locator、
   chunk 内半开字符范围、原始元素半开字符范围和表头重复标记；人为加入的段落
   分隔符不伪装成来源字符。

`Chunk.locators` 必须等于全部 span locator 的有序去重结果。span 必须非空、
有序、不重叠、位于 chunk text 内，chunk/source 范围长度必须一致。

## 3. 正文和列表

同一 TEXT run 内的短段落可合并。普通段落之间使用两个换行，连续列表项之间
使用一个换行。每次只在完整原子边界比较“不加入下一个”和“加入且不超过 hard
max”与 target 的距离；距离相同时选择较早边界。最后一个尾块能完整并入前块且
不超过 hard max 时才合并。

标题路径只进入 `embedding_text`，不进入可引用的 `text`。普通块之间不使用
统一 overlap，因此不会为了窗口滑动复制相邻段落。

## 4. 超长原子和 overlap

单个段落、表格行或 OCR 行超过 hard max 时，按固定优先级寻找边界：

1. 双换行；
2. 单换行；
3. 句号、问号、感叹号；
4. 分号；
5. 逗号、顿号、冒号；
6. 空白；
7. 没有可用边界时才 hard token cut。

在 target 内选择最高优先级的最远边界；target 内没有边界时可继续搜索到 hard
max。overlap 只存在于同一超长原子的相邻 segment，且只能复用不超过配置上限的
完整上一句或上一行。找不到完整语义后缀时 overlap 为零，不能截取半句话。

## 5. 表格和 OCR

每张表使用独立 neighbor group。第一行是当前 V1 表头候选，每个后续 segment
重复表头；重复 span 使用 `is_repeated=true`，不会冒充新的原文位置。普通数据行
只能在完整行之间切分；单行过长时先尝试单元格边界，再使用超长原子规则。

每张图片使用独立 OCR neighbor group。OCR 文本优先按原始换行打包，单行过长
才使用相同的语义 splitter。图片不与正文或其他图片合并；同一媒体多次引用仍由
不同 locator 和 chunk identity 区分。pending/failed OCR 不形成证据，低置信
OCR 保留原置信语义，不能单独支撑确定回答。

## 6. 稳定 ID、邻接和重建

稳定 chunk ID 覆盖 source ID、section ID、neighbor group ID、role、全部有序
span 的 element ID、locator logical key、字符范围、重复标记和完整 text。文件
纯重命名只改变展示路径，不改变 logical key；修改第二个 locator 或后续 span
会改变 ID。

`previous_chunk_id` 和 `next_chunk_id` 只连接同一 neighbor group 内的连续
segment。运行时扩展还会重新校验 source ID、文档版本和 neighbor group，缺字段
时失败关闭。

Qdrant collection metadata 使用 `payload_schema_version=2`，payload 保存
section、group、role 和全部 source spans。旧 chunker 或 payload schema 与 v2
不兼容，必须建立新 collection、完成全量索引和 snapshot 后原子切 alias；禁止
原地改写旧 active point。

## 7. 引用 locator

证据组装在进程内保留 source spans，但不改变外部回答 JSON Schema。回答校验会
枚举 quote 在 chunk text 中的全部出现位置：

- 每次出现都必须完整位于一个 span；
- 多次出现但都映射同一 locator 时可发布；
- 映射到不同 locator 时以 `AMBIGUOUS_QUOTE_LOCATION` 进入唯一一次修复；
- 跨 span 的 quote 直接拒绝；
- `ClaimSupport.locator` 返回实际包含 quote 的 locator，而不是固定取第一个。

audit manifest v2 保存全部 canonical spans。现场导出会重新校验 text SHA、span
顺序与范围、locators、stable ID、source/version、pipeline、active state 和精确
point count；audit JSON 只能留痕，不能回灌生产评分。

## 8. structural 与 retrieval 消融

structural mode 固定包含 legacy `384/512/64`，并默认比较 v2
`256/512/32`、`320/512/48`、`384/512/64`：

```bash
.venv/bin/python evaluation/chunking_ablation.py docs \
  --mode structural \
  --tokenizer deployment/assets/tokenizers/embedding/tokenizer.json \
  --pipeline deployment/config/pipeline.json \
  --corpus-policy deployment/config/corpus-policy.json
```

报告只含聚合计数，检查标题块、跨 section、跨 group link、hard max、原文覆盖、
重复字符、表格行、空块、重复 ID、quote locator 和自动编号。结构门槛只能排除
错误候选，不能选择检索参数或证明准确率提高。

retrieval mode 只调用 `load_tuning_cases()`，要求一个仅含文档键到相对路径、
不含问题和 expected 的独立映射。每个候选使用独立临时 Qdrant collection 和
SQLite state，不创建或切换 active alias；结束后删除临时 collection。它使用
真实 embedding/reranker 计算 Recall@5/10/20、MRR、rerank Recall@5，并单列
cross_chunk、table、numeric：

```bash
.venv/bin/python evaluation/chunking_ablation.py docs \
  --mode retrieval \
  --tokenizer deployment/assets/tokenizers/embedding/tokenizer.json \
  --pipeline deployment/config/pipeline.json \
  --corpus-policy deployment/config/corpus-policy.json \
  --retrieval-config deployment/config/retrieval.json \
  --dataset evaluation/frozen/questions.json \
  --document-map tuning-document-map.json \
  --qdrant-url "$RAG_QDRANT_URL" \
  --embedding-endpoint "$RAG_EMBEDDING_URL" \
  --reranker-endpoint "$RAG_RERANKER_URL"
```

缺少真实模型 revision 和 tuning 结果时，pipeline 与 retrieval 必须继续标记为
`provisional`。调参过程不得加载或查看 holdout expected。

## 9. 已知边界

Word 自动编号仍是明确 P1：`docx-parser-v3` 保留段落 run 文本和列表层级，但不
解析 `numbering.xml` 来渲染多级编号、restart 和 style 继承。审计只报告检测数
与未表示数，不猜测编号文本。

当前证据粒度和检索指标不足以证明 parent-child 的收益。只有 section-pack-v2
完成真实 tuning/holdout 验收后，仍能用明确的跨块失败样本证明现有证据组装不足，
且能定义 parent 与 child 的独立 ID、权限、版本和引用契约时，才考虑该方向。
