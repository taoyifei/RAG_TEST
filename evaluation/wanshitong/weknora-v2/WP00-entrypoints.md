# WeKnora V2 接入基线

- 本方起点：`codex/wb08r-q1-algorithm-debt` 的 `e3e664cf4fdda16ef030233261a22587e556e848`；实施分支 `codex/wb08r-weknora-v2`。
- 上游固定：Tencent/WeKnora `1edcd54b43606d9079bb36650efe3f68707a79ea`（v0.8.0）。
- 现有 `RetrievalService.search_and_answer` 在 `src/rag_app/application/retrieval/service.py`；包含 QueryPlan/Atom、字段解析、物理表格事实、语义复核与逐 Claim 发布。旧公共 `/api/public/chat` 使用 `P09AnswerStream`，继续保持原协议。
- 已接入的 `ContextReader` 在重排后按 DocumentIR 回读；`context_reader_mode` 默认为 `legacy`。V2 标准主链独立执行 Hybrid 检索、重排、回读与普通聊天，不复用旧回答编排。
- 候选档位：`legacy` 保持当前行为；`wk-standard-v1` 使用旧索引和自然回答；`wk-standard-pc-v1` 使用独立父子索引。候选档位只在管理员测试入口可选，现有公共入口不切换。
- Product Runtime 默认仍使用旧分块器；仅在隔离候选实例设置 `RAG_WK_CHUNKER_MODE=parent-child` 时创建 Go 父子索引。新增文件格式也须显式配置 `RAG_WK_DOCUMENT_FORMATS`，默认仅 DOCX。
- 分块接入点：`ChunkerPort`、`RevisionBuilder`、`ContextReader` 与 SQLite canonical Chunk/SourceSpan；父子关系需要独立于现有 `parent_node_id`。
- 上传限制分别存在于 `wanshitong/upload_validation.py`、`document_metadata.py`、`admin_api.py`、`application/lifecycle.py` 和前端 `AdminDocumentsPage.tsx`；新增格式必须同步改动并逐格式验证。
- 候选验证先走离线单测和 8289 隔离实例。18288 服务及其镜像、数据卷不变。F017 只作一次质量观察，不作为接入门禁。

状态以真实执行为准：`IMPLEMENTED` 表示代码接线，`INTEGRATION_PASS` 表示真实模型/入口贯通，`QUALITY_MEASURED` 表示比较完成，`PROMOTION_READY` 需另有生产发布授权。
