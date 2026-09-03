# Evaluation V3

## 目标与信任边界

V3 使用 52 条版本化合成 Case，继续运行生产 P06 Builder 与 P07/P08.5
Retrieval 路径。它验证离线结构、身份、隔离、来源精度、拒答和指标计算；
不验证 Jina、Qwen、远程 Reranker 或生产 Qdrant 的语义质量与性能。

`EvaluationCase` 可读取历史 schema 2 和新增 schema 3。扩展数据集标记为 3，
由 `evaluation/schema/p08-case-v3.schema.json` 描述。Source Range 可携带
`occurrence` 或 `structural_anchor`，运行时解析为 `node_id` 与精确字符范围。
同一文档出现重复 `exact_text` 且没有选择器时，Dataset 构建必须失败。

## Observation 来源

Observation 读取 `RetrievalDiagnostics` 中的实际通道、Fusion、Rerank、Neighbor
Expansion 和 Evidence ID。Recall、MRR 与 nDCG 不再从最终 Evidence 反推。
诊断仅含 ID、rank、有限 score、计数、耗时和原因码，不含 Query、正文、向量、
prompt、Provider body、secret 或绝对路径。

每条观测记录实际 provider、embedding、reranker call/retry，cache hit，Evidence
数量/token，以及 Exact、Lexical、Vector、SQLite hydration 和其他 stage elapsed。
未执行的能力保持 `not_executed` 或 `not_applicable`，不能伪装成测量值。

## 指标分类

Retrieval 指标：各 Channel Recall@1/5/10，Fusion 与 Rerank 的 Recall@1/5/10、
MRR@10、nDCG@10，以及 scope/revision/vector-space 安全计数。

Evidence 指标：Evidence document/chunk precision、Source Range precision/recall/F1、
每答 Evidence item 数、irrelevant evidence 数和 token 使用量。

Answer 指标：answerable accuracy、refusal precision/recall/F1、Citation document/
chunk precision、Citation validity/publishability 与 unsupported claim 数。

工程指标：各 stage latency、实际 Provider 调用/重试、cache hit、构建吞吐和本地
内存。工程指标只允许在同主机、同进程模式、同数据集、同配置之间比较。

## 选择与门禁

单变量候选只读取 tuning 标签，按安全约束、指标阈值和稳定字典序选择；选定
候选只在 holdout 运行一次。V3 至少要求 50 Case，并对 CJK、无标识符事实、
噪声、重复来源、多文档、表格、隔离负例、不可回答和跨 Chunk 长段落设下限。

旧 P08 `evidence-cap-8` 结果保留，状态为 `diagnostic_superseded`。它的旧指标
数据源和宽 Evidence precision 不满足 V3 接受条件，不能作为当前生产质量证据。
