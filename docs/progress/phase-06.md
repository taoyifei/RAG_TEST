# Phase 06：本地持久化索引与不可变 Revision

## 集成证据

- Integration base SHA：`bb13930ba3b06bcfcecb3f0acdf7c023e16afe1b`
- Feature branch：`codex/p06-local-index`
- Feature commits：提交后回填
- Integration merge commit：合并后回填
- Remote feature SHA：推送后回填
- Remote integration SHA：推送后回填

## 交付范围

- 五个单调 SQLite migrations，覆盖控制面、Artifact、Revision/Chunk、FTS5/Exact、Cache/GC。
- Content-addressed Filesystem Blob、SQLite catalog 和批量引用事务。
- project-scoped 持久化 Embedding cache、per-chunk/per-slot 进度、Job/Provider/Slot 预算。
- Memory 与 Qdrant local-memory/local-path 完整 Named Vector Point adapter。
- 不可变 Revision Build、实际 Store Validate、原子 Activate、幂等 retry/reopen backfill。
- GC dry-run plan、状态漂移拒绝和 Qdrant-first 删除边界。
- 严格 P06 Profile/Registry Config 与 init/ingest/job/info/validate/backfill/gc CLI。

## Schema、Port 与兼容性

DocumentVersion 只由 document ID 和 bytes 决定，Parse/Chunk/Embedding 合同属于 Revision。
旧 slot-specific Vector API 保留兼容；P06 canonical API 要求完整 Named Vector Point。P01-P05
默认 Profile 保持原名和行为，新组件使用 `memory-vector`、`qdrant-local`、`sqlite-fts5`、
`sqlite-control`、`filesystem-blob` 显式选择。

Migration 可空库执行、重复执行；已应用 checksum 漂移和未知更高版本 fail closed。P05.5
前无正式 P06 数据，因此没有执行旧 dver 或旧单向量 collection 的原地迁移。

## 验证结果

- 开工 `scripts/dev.py check`：1206 passed，75 deselected，4 warnings。
- 开工 `scripts/dev.py smoke`：62 passed，1 warning。
- P06 Store/Application 定向：17 passed。
- P06 Persistence/E2E 定向：3 passed。
- Feature 分支完整 `check`：1226 passed，75 deselected，4 warnings；compile、Ruff、
  mypy（234 source files）和 Google docstring（0 missing sections）通过。
- Feature 分支 `smoke`：63 passed，1 warning。
- `git diff --check`：通过。

所有普通测试使用临时 data dir 和 `RAG_TEST_NETWORK=offline`。实际调用 SQLite、Filesystem、
Memory Vector、Qdrant embedded local-memory/local-path 和 Deterministic Provider；真实 Jina、
阿里云、Qdrant Server及其他公网服务调用次数为 0。

## 决策与风险

本阶段未触发 DECISION_REQUIRED 条件。Qdrant embedded 模式不是 Server 生产等价证据；
Chunk V3 参数和质量提升声明仍保持 provisional；P07 才提供完整 Query Planner/RRF/Rerank/
Evidence/Answer 运行链路。

P06_READY: false
