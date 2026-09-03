# Search quality boundaries

## 当前可以陈述的事实

P08.5 离线 Evaluation V3 使用合成 DOCX、临时 SQLite/Filesystem、Memory
named-vector store 和 deterministic provider。通过门禁可以证明当前代码在这些
输入上执行了全量 Vector Point 对账、Scope 与 Active Pointer 约束、单 Writer
fencing、对称 CJK FTS、可恢复 GC、精确 Source Range、Evidence precision 和
拒答检查。

这些结论必须同时给出数据集 SHA、run/manifest SHA、lane、split 和样本量。
Retrieval、Evidence、Answer 指标必须按各自数据源命名，不能用 Citation
Validity 代替 Evidence precision，也不能从 Evidence 顺序反推 Retrieval 排名。

## 当前不能陈述的事实

本阶段没有调用 Jina、Qwen、远程 Reranker、Qdrant Server 或其他公网服务，
也没有使用企业文档。因此不能声称：

- 真实 Provider 语义质量、限流、成本或可用性已经验证；
- primary/standby failover 质量已通过；
- 本地同步耗时代表生产延迟或并发容量；
- 合成语料的满分可推广到真实业务文档；
- 相同向量维度意味着不同 Provider 的向量空间兼容。

未校准 dense-only 证据会拒答。只有明确绑定 Provider、模型、向量空间、策略和
校准状态的路径，才允许把 dense-only support 用于回答决策。

## 阶段状态解释

`P09_READY: true` 只表示 P08.5 的离线结构与质量门禁允许开始 P09 HTTP API
阶段。它不表示远程生产 Profile、真实 Provider、生产数据迁移、容量、安全审查
或正式发布已经完成。

```text
OFFLINE_EVALUATION_V3_READY: true
PRIMARY_LIVE_EVALUATION_STATUS: BLOCKED
STANDBY_LIVE_EVALUATION_STATUS: BLOCKED
REMOTE_PRODUCTION_PROFILE_READY: false
P09_READY: true
```
