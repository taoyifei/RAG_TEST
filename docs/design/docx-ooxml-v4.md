# DOCX OOXML v4 设计

## 目标与边界

`docx-ooxml-v4` 是 ParserPort 的本地 Adapter。它把一个受控 DOCX 字节源转换为 P03
Document IR，不分 Chunk，不调用 OCR、Embedding、Reranker 或 Generator，也不访问任何
外部 relationship。Core 模型不依赖 lxml；所有 XML、ZIP 和计数状态只存在于 Adapter。

## 处理流水线

1. `DocxPackage` 在正文读取前验证扩展名、ZIP 路径、加密位、条目数、解压量、压缩比、
   content types、根关系、主文档类型和宏标记。
2. `PartCatalog` 为每个 Part 保存 URI、类型、大小和 SHA-256，并把关系目标规范化为包内
   绝对 URI；外部关系只保留 scheme。
3. `StyleCatalog` 与 `NumberingCatalog` 解析可证明的样式继承和每个 numId 的独立计数状态。
4. `BlockParser` 按真实 child 顺序把正文、表格、内容控件、Section、修订、图片和文本框
   写入 `IrNodeBuilder`。
5. `stories.py` 解析 effective header/footer binding、note、comment 与 bookmark 关系。
6. `IssueCollector` 按规范化 JSON 聚合同类安全 issue；Parser 构造覆盖率和 story 计数。
7. 源 DOCX 与 media 使用内容摘要写入 BlobStore；任何一次写入失败都会逆序删除本次已
   提交的 Blob。

生产模块按职责拆分，均低于 500 行。`models.py` 中间对象不会出现在公共 API。

## 身份、顺序与确定性

文档版本由输入 SHA-256 派生。节点身份绑定 document version、Part URI、结构路径、节点
类型和内容摘要，不包含机器路径或 ZIP 顺序偶然值。相同 DOCX、相同 ParsingPolicy 重复
解析的 canonical JSON、节点 ID 和报告一致；`elapsed_seconds` 明确排除在快照外。

表格同时保存物理 Row/Cell 和逻辑 `CellGrid`。纵向 continuation 指向 anchor cell，omitted
grid 与空 cell 不合并。Header/Footer 的真实 Part 只解析一次，后续 Section 保存 effective
binding 与 `inherited_from`，不复制来源节点。

## 策略语义

安全超限在 strict 与 best_effort 下都失败。语义未知且含可索引证据时，strict 抛出
`UnsupportedDocumentFeature`；best_effort 生成不含原文的 UnsupportedNode、issue，并降低
coverage。外部关系、批注、页眉页脚、notes、图片、隐藏文字与修订均由冻结策略控制。

默认修订视图保留 ins/moveTo，排除 del/moveFrom。字段只读取已有 result；instruction 只
保存字段类型、目标名和摘要。TOC 结果是导航 metadata，不作为重复证据。

## 兼容迁移

Registry 同时保留 `docx-ooxml-v4`、`legacy-docx-ir` 与 P01 `legacy-docx`。开发离线 Profile
显式使用 v4，其他旧 Profile 默认值不变。`V4DocumentIrToLegacyElementsAdapter` 允许旧
Chunker 读取基础节点；复杂表格只输出一次扁平文本 Element，并产生
`V4_COMPLEX_TABLE_FLATTENED`，不声称旧合同完整表示了 grid。

Fixture 由 python-docx 提供基础 styles Part，再用受控 ZIP/XML patch 构造其 API 不支持的
复杂结构。写入器固定时间戳、权限、压缩方式和条目顺序；20 个合成文档各有 IR/report
快照和 SHA-256 manifest。
