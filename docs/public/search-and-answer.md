# Search and Answer

The P07 application service accepts a project/knowledge-base-scoped query and
returns the actual revision and fingerprint summary, route and rerank modes,
ranked evidence, confidence/refusal status, an extractive answer when supported,
and a safe trace identifier.

`DENSE_UNAVAILABLE` means query embedding or its selected vector space could not
be used. It does not mean the knowledge base is empty; exact and FTS5 retrieval
may continue. `INDEX_NOT_READY` means there is no active immutable revision.
`INDEX_CORRUPT` means persisted channel identities cannot be hydrated from the
canonical SQLite chunk store and fails closed.

Answers are extractive in P07. Support IDs are assigned by the application and
must resolve to citable source spans. Generated rewrite text is retrieval-only
and cannot become evidence. Query text, vectors, provider bodies, prompts,
secrets and private candidate bodies are omitted from default traces.

The P07 final-result cache is process-local and reads only after the actual
embedding slot and rerank execution mode are known. Its hashed key binds
project, knowledge base, active revision, serving fingerprint, filters, access
policy, conversation and rewrite identity. It does not store the raw query;
the authorized result may remain in process memory only for the runtime
lifetime and is cleared when that runtime closes. P07 does not provide a
durable or cross-worker answer cache.

P07 parameters are provisional and no semantic-quality or production-readiness
claim is made without P08 evaluation.

受控试用问答页显式请求 `include_related_content=true`。在当前证据不能支持
回答时，`related_contents` 最多展示三条已授权 canonical 原文，每条最多
240 字符；这与正式 `evidence` 和 `answer` 分离。没有可安全展示的候选时不
渲染空卡片，必要重排实际失败时使用服务退化提示，不把相似片段冒充答案。
默认旧请求不返回新增展示字段，OpenAPI 与 SDK 均保留可选兼容合同。

查看原文复用现有管理员 Revision Chunk 读取入口，携带 `document_id` 和
`chunk_id` 精确定位；该定向读取重新检查当前活动 Revision 与文档删除状态。
未授予管理员读取权限的普通客户端不能使用此入口。问答正文与相关片段均不
写入浏览器持久存储，页面以纯文本展示文档里的 HTML、链接和指令。

当前查询扩展为 `RuleBasedNormalizer`，只执行 NFKC/空白规则标准化；
本轮没有增加生成式 Query Rewrite 或生成模型调用。
