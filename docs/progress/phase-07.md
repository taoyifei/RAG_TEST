# Phase 07：自适应检索、证据与拒答

## 阶段分支证据

- Integration base SHA：`bfcc8d271b3bbf73aa66f9b596eff902cdf9acb4`
- Feature branch：`codex/p07-adaptive-retrieval`
- P05.5 anchor：`bb13930ba3b06bcfcecb3f0acdf7c023e16afe1b`，已验证仍为
  integration base 的祖先。
- P06 merge：`687350d4e942cdda75af42fed8b71ead94f7c1dc`
- `main` 与 `Industry` 保持只读；开工基线分别为
  `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` 和
  `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a`。

- Feature commits：
  - Core contracts：`3b070ff`
  - Query embedding router：`c02109e`
  - Revision-bound stores：`9724bfa`
  - Retrieval/evidence/answer pipeline：`512de1f`
  - Offline tests and fault injection：`cb6cfbd`
  - Design and feature evidence：`4ba78ec`
- Remote feature SHA：`4ba78eceab9d88677b696891e54e5e97ff8b0973`
- Integration merge commit：`1e8c32242a42ef816c02bbb4d207f828fa1b62dc`
- Remote integration gate-tested merge SHA：
  `1e8c32242a42ef816c02bbb4d207f828fa1b62dc`

## 交付范围

- 一个 SQLite 读事务冻结 `ActiveRevisionQuerySnapshot`；请求内 Exact、FTS5、
  Dense、Hydration 和结构扩展始终使用同一 revision。
- 确定性 NFKC Query Analyzer、五类 QueryKind、bounded rewrite 与 planner。
- Single 和 Hot-Standby 均使用非空 `QueryEmbeddingPort`。Hot-Standby 一次请求
  只选择一个 slot，并严格绑定 `dense_primary` 或 `dense_standby`。
- Exact identifier/quoted phrase、SQLite FTS5 与一个 selected Dense channel；候选
  只携带身份，正文从 canonical SQLite Chunk V3 批量 hydration。
- rank-only RRF（`k=60`）、独立 reranker circuit、严格响应校验、显式 bypass 与
  bounded must-keep exact。
- 双向 neighbor、table continuity 和 bounded section expansion；source-span
  dedup、来源多样性与 Evidence token/document/section caps。
- 可解释 provisional confidence、稳定 refusal、extractive answer 和 Support ID/
  quote/source-span validator。
- 绑定实际 revision/slot/rerank/filter/access/conversation/rewrite 的进程内 Final
  Cache，以及不含 query、正文、向量、Provider body、prompt 或 secret 的 SAFE Trace。
- 旧 `QueryService` 和 HTTP schema 保持不变；P08 前不切换默认生产链路。

## 验证结果

- P07 targeted：58 passed。
- 更新后的 P06/non-null-router 与 architecture 定向集合：70 passed。
- Feature 分支完整 `check`：1267 passed，75 deselected，4 warnings；compile、
  Ruff、mypy（262 source files）和 Google docstring（0 missing sections）通过。
- Feature 分支 `smoke`：69 passed，1 warning。
- Integration 合并后完整 `check`：1267 passed，75 deselected，4 warnings；
  compile、Ruff、mypy（262 source files）和 Google docstring
  （0 missing sections）通过。
- Integration 合并后 `smoke`：69 passed，1 warning。
- `git diff --check`：通过。
- 故障注入覆盖 Lexical、Dense、Reranker、canonical Chunk、Neighbor、Generator 和
  Active pointer drift；覆盖 active revision 切换后的 cache key 失效。
- 所有测试使用 `RAG_TEST_NETWORK=offline`、临时目录和 Fake/Deterministic
  Provider。真实 Jina、阿里云、Qdrant Server及其他公网服务调用次数为 0。

## 决策与风险

本阶段未触发 `DECISION_REQUIRED`。RRF、rerank、expansion、evidence 和 confidence
参数均为 provisional；P07 只证明结构、隔离、故障切换和失败关闭，不声称检索质量提升。
Circuit 与 Final Cache 都是进程内状态，不代表多 worker 协调或持久缓存。Filter 只允许
`document_id`、`section_id`、`role` 以及 `allowed_document_ids`；未知字段 fail closed。
Qdrant Server、真实 Provider、HTTP 生命周期、UI 和发布部署均未在本阶段验证。

P07_READY: true
