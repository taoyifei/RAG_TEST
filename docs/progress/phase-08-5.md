# Phase 08.5 Progress: Quality Hardening

## 集成身份

- Start SHA: `55de66bd872756d7c80929c1560be44daf3a700c`。
- Feature branch: `codex/p08-5-quality-hardening`。
- Integration branch: `feature/universal-rag`。
- P06/P07/P08 merges: `687350d`、`1e8c322`、`f608bc3`。
- Feature commits before the documentation commit: `5915424`, `a377ba2`,
  `fc6a39b`, `2d72f39`, `cac35e2`, `f1c9e22`, `8bfee5e`, `fccd439`, and
  `e0f8f33`。
- Integration merge commit: 完成 `--no-ff` 合并后补录。
- `main` 与 `Industry` 保持只读。

## 交付范围

- Vector inventory 对每个 raw Point 与 canonical Chunk 的 identity、payload、
  named-vector 和维度全量对账。
- Migration 0006—0009 增加 Scope/Active Pointer Trigger、Revision writer
  lease/fencing、FTS V2 和可恢复 GC。
- CJK 文档与 Query 使用同一 deterministic bigram analyzer；V1 Revision
  走 legacy reader 或显式 `REINDEX_REQUIRED`。
- GC 保存逐项状态，恢复中断删除，并对账物理 Blob 与 Catalog。
- Exact/FTS 只对明确 transient 分类降级；corruption 与未知异常 fail closed。
- Result Cache 在 Query Embedding 和 Reranker 前查询，命中不调用 Provider。
- Evidence V2 按 Query 相关性选择 SourceSpan；Confidence V2 只使用最终
  Evidence support，未校准 dense-only 路径拒答。
- Evaluation V3 扩展为 52 条合成 Case，测量实际 Channel/Fusion/Rerank、
  Source precision 和工程诊断。

## 评测边界

旧 P08 `evidence-cap-8` 报告原样保留，并标为 `diagnostic_superseded`。其
Retrieval 指标来自 Evidence 反推，Citation Source Precision 实际是文档精度，
且语料覆盖不足，因此不能作为 V3 接受证据。

接受的 committed-code run 为 `p08-20260903T093442Z-8c342979`，绑定
`e0f8f335d611c2543d274ef4b95c09e8682fc8ca`，数据集 SHA 为
`sha256:bf2fc34f1d0f83447229f7272a00ae19040d237201debacfec34e658f1f3f164`，
manifest SHA 为
`sha256:a321d7534f3149fc67593701d2b98b4b59db9d68a5e134d31b377e5f6c132523`。
选中 `fts5-only`，全部 holdout gates 通过。

Holdout 共 24 Case，其中 20 条 answerable：Fusion Recall@1 为 0.9、
Recall@5 为 1.0、MRR@10 为 0.95、nDCG@10 为 0.9630929753571458；
Rerank Recall@1、Evidence document/chunk precision、Citation document/chunk
precision、Source Range precision/recall/F1、Answerable Accuracy 和 Refusal F1
均为 1.0；irrelevant evidence 为 0。

## 门禁证据

- Opening check: 1289 passed、75 deselected、4 warnings；smoke 71 passed。
- Required store tests: 22 passed。
- Required application and evaluation tests: 104 passed。
- P08.5 end-to-end tests: 2 passed。
- Final compileall、Ruff、strict mypy over 279 files、Google docstrings 通过。
- Final default-offline check: 1321 passed、75 deselected、4 warnings，
  219.92 秒。
- Final smoke: 71 passed、1 warning，8.49 秒。
- Final offline Evaluation V3: gates passed，selected `fts5-only`。
- Final `git diff --check`: passed。

External services actually called: none。

## 状态

以下状态以远程阶段分支和 `--no-ff` 集成验证完成为最终生效条件：

```text
OFFLINE_EVALUATION_V3_READY: true
PRIMARY_LIVE_EVALUATION_STATUS: BLOCKED
STANDBY_LIVE_EVALUATION_STATUS: BLOCKED
REMOTE_PRODUCTION_PROFILE_READY: false
P09_READY: true
```
