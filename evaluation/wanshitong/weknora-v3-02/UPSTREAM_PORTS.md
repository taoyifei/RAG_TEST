# WeKnora V3-02 上游映射

固定参考：`Tencent/WeKnora@1edcd54b43606d9079bb36650efe3f68707a79ea`。本方固定 Go 分块二进制 SHA-256 为 `491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f`。本阶段未改 Go 内核、文档解析器或阅读域。

| 上游参考 | 本方接入 | 保留的语义与适配边界 |
| --- | --- | --- |
| `internal/application/service/chat_pipeline/query_understand.go` | `weknora_query.py`、`weknora_pipeline.py` | 受控历史只生成至多一个附加检索问法，始终保留原问；改写失败回退原问。明确数值、否定、引号内容和《文档名》不得被删。调用复用既有 Qwen、取消令牌与请求预算。 |
| `internal/application/service/chat_pipeline/rerank.go` | `reranking.py` 的候选排序视图 | 利用标题与查询命中的连续窗口给既有内网 Reranker 排序；不改 `citation_text`、原文 Artifact 和 `SourceSpan`，不把截短视图作为生成原文。 |
| `internal/application/service/chat_pipeline/merge.go` 及 V2 已接入的父级回读 | `weknora_pipeline.py`、`natural_context.py` | 命中子块后优先完整父级；超预算时只取同版本、带真实来源区间的局部材料；继续尝试后续候选。统一估算输入、模型输出与安全余量，并把实际送模别名和区间写入 Trace。 |
| 上游普通问答与来源编号做法 | `natural_answer.py`、候选 API | 合法引用才发布答案；缺失或非法引用作为管理员草稿与诊断，禁止把无引用正文当成成功知识答案。 |

本方沿用原有 Dense、Lexical、Exact、RRF、Qdrant、SQLite、内网 Embedding、Reranker 和 Qwen。现有服务端 Provider 协议、认证、SSO、用户页面及旧 P09 问答链未被替换。V3-03 的正常页面接入和实时流式仍是后续阶段。

父子分块实测还发现一个本方报告聚合问题：跨子块重复出现的 `DERIVED_NUMBERING` 原先按 Span 次数统计，超过原始列表项数而触发校验。本阶段改为按原始节点 ID 去重，只修报告计数，保留来源映射与 Go 输出；合成多列表项及真实 DOCX 均已验证。
