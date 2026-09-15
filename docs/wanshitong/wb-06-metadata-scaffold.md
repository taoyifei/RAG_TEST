# WB-06 文档元数据与快捷入口设计映射

## 范围与不变量

WB-06 继续复用一个隐藏 Project、一个隐藏 Knowledge Base 和唯一的 Universal
`ProductRuntime`。文档分类只作为结构化外部元数据进入现有索引数据流；本阶段不创建
第二套检索、Provider、History、Trace 或 Qdrant 身份，也不在 Exact、FTS、Dense、
RRF、Reranker 或 Evidence 选择阶段应用分类过滤。

公共问答请求仍只接受问题与会话 ID。服务端构造的未来筛选模型固定为空：
`department_keys=[]`、`category_prefixes=[]`、`topic_keys=[]`、
`shortcut_id=null`。因此新增元数据不会缩小默认召回范围。

## 类型与解析优先级

湾事通逻辑文档登记使用类型化元数据：

- `department_key`、`department_name`；
- 保持顺序的 `category_path`；
- `document_title`、`source_relative_path`、`topic_keys`；
- 固定 `visibility_scope=all_internal`；
- 当前为空的 `allowed_roles`、`allowed_groups`；
- `metadata_revision`、`created_at`、`updated_at`。

解析优先级依次为管理员显式值、浏览器目录相对路径、双横线文件名和未分类默认值。
路径先做 NFKC、首尾空白与分隔符规范化，再拒绝绝对路径、盘符、UNC、空段、点段、
控制字符、NUL、过长路径和规范化后出现的隐藏穿越。`source_relative_path` 不做 HTML
实体解码；仅展示用 `document_title` 将 `&amp;` 解码为 `&`。

`department_key` 由规范化部门名的可读 slug 与短 SHA-256 组成，不依赖 Python
`hash()`，因此跨进程和跨机器稳定。

## 持久化与传播

数据只有一条写入和传播链：

```text
湾事通上传元数据
  -> 通用 DocumentRef metadata + 湾事通类型化登记
  -> QueuedIngestion.request_json 不可变 Job 快照
  -> ParseContext / Document IR 文档级 metadata
  -> Chunk metadata（不进入 citation/embedding/lexical 文本）
  -> 既有 SQLite Chunk/FTS 持久化
  -> Qdrant 结构化 payload
  -> Evidence metadata
  -> 湾事通公共引用投影与管理员文档详情
```

向前迁移为 Universal `documents` 增加默认空的通用 `metadata_json`，并扩展现有
`wanshitong_document_metadata`，另存每个 DocumentVersion/Job 的提交时元数据快照。
Universal 模式继续使用空 metadata。新版本默认沿用逻辑文档分类；显式上传字段覆盖
自动解析值。软删除保留本 Document ID 的登记，其他 Document ID 不会复用该记录。

文档元数据不拼入正文、Embedding 或 lexical 文本，不参与 Chunk ID、semantic hash、
原文坐标或引用闭合。索引 payload schema 只增加可选结构化字段；空筛选下现有查询链
和缓存正文边界保持不变。

## 快捷入口与未来筛选

服务端定义类型化 `ShortcutDefinition` 和 `MetadataFilter`。四个草稿快捷入口均为
`enabled=false`、`visible=false`；公共 capabilities 只返回可见且启用的定义，所以本
阶段固定返回 `shortcuts=[]`。客户端不能提交 `filter_expression`，快捷入口也不表达
ACL。未来启用时只能由服务端按 `shortcut_id` 查定义并转换为类型化 metadata filter，
然后复用同一 KB 的既有检索链。

## 必要触点与回归边界

预计需要的通用触点限定为：

- `DocumentRef`/持久队列增加默认空 metadata；
- 生命周期在同一文档与 Active 快照间保存、恢复 metadata；
- Revision Builder 将外部 metadata 附着到 IR；
- 既有结构 Chunker 将 IR metadata 复制到 Chunk metadata；
- Qdrant payload 和 Evidence 只传播白名单字段；
- `RagSdk` 上传方法增加默认空的可选 metadata 透传。

其中 `src/rag_app/sdk.py` 与 `src/rag_app/application/retrieval/` 属于受保护触点；修改
仅用于把已有数据链补齐，必须由定向 Universal 回归覆盖。RRF、Reranker、Confidence、
Claim Validation、QueryExecutor、P09 流式协调和 Provider Runtime 不修改。
