# Document IR V1 设计

## 边界

Document IR 描述 Parser 观察到的文档事实，不承担检索分块、Embedding、索引写入或
Provider 请求。`rag_app.core.models.document` 只依赖标准库和 Pydantic；DOCX、lxml、
BlobStore 实现和旧 `Element` 均留在 adapters。

V1 使用扁平节点表：

```text
DocumentIR
  source
  document / version
  root_node_ids
  nodes
  relationships
  parse_report
```

每个 `DocumentNode` 通过 `parent_node_id`、`child_ids` 和同级 `order` 表达树。全局
validator 以节点 ID map、parent 分组和三色访问状态完成 O(n) 校验，包括唯一 ID、根、
父子对称、连续顺序、无环、关系目标、来源身份和 ParseReport 计数。

## 文本与来源

`TextPayload` 分离 `exact_text` 与 `semantic_text`，并分别保存 SHA-256。P03 只统一
CRLF/CR 为 LF；保留 Tab、显式换行、编号、数字和单位，不加入标题路径，也不做摘要或
改写。

`SourceAnchor` 保存 package `part_uri`、story、稳定逻辑 `structural_path`、序号和可选
section/paragraph/table/row/cell/relationship/字符范围。它不保存 XPath namespace 噪声、
内存地址或机器绝对路径。

稳定身份不使用显示名或本地路径：

- `document_id` 由宿主提供，重命名不改变它；
- `document_version_id` 绑定文档内容摘要；
- `node_id` 绑定 version、part URI、结构路径、节点类型和内容摘要；
- Embedding slot、Provider task、instruction 和向量名不进入 node identity。

## ParsingPolicy

`ParsingPolicy` 明确 strict/best-effort、修订、批注、隐藏文字、story、图片、外部关系和
未知可索引结构策略。文件大小、解压总量、单 entry、entry 数、压缩比和解析超时始终
执行，best-effort 不会放宽这些边界。规范化策略 JSON 进入 index fingerprint。

P03 的 `legacy-docx-ir` 能力是诚实的过渡边界：

- 标题、段落和普通内容控件转成基础 IR；
- 列表和图片为 partial；
- 表格保存 Table + 扁平 representation，并报告
  `LEGACY_TABLE_STRUCTURE_LOSS`；
- 修订正文、批注正文、页眉页脚正文、脚注尾注正文不声明完整支持；
- 外部关系只计数，绝不访问目标；
- `best_effort + unknown_indexable_content=issue` 可跳过未知证据并降低覆盖率。

## Blob 和序列化

旧图片 `binary_data` 与源 DOCX 在 IR 返回前写入 BlobStore，IR 只保存 BlobRef。批量写入
中途失败时，adapter 反序删除本次已经写入的 blob。宿主注入的 Store 生命周期仍归宿主。

`canonical_document_ir_json()` 使用字段排序、紧凑 UTF-8 JSON，排除 elapsed、受控临时
路径和 bytes。默认调用方可移除正文；CLI 只有显式 `--include-content` 才写出文本。

## 双 Embedding Slot 边界

同一 Document IR 后续必须导出相同 `embedding_text` 给 Jina primary 和 Qwen3.7 standby。
Provider 前缀、task、text_type 和 query instruction 只属于 Provider adapter 和
IndexRevision 指纹，不写回 `exact_text`、`semantic_text`、node ID 或 chunk ID。
