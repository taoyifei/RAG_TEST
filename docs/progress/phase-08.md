# Phase 08 Progress: Frozen Evaluation and Quality Gates

## Integration identity

- Integration base SHA: `76a9d68edec80454928bde78599ca8c1dca55fc8`.
- Feature branch: `codex/p08-evaluation`.
- Feature commits: pending final commit audit.
- Integration merge commit: pending final integration.
- Remote feature/integration SHAs: pending final push verification.
- P06/P07 prerequisites: merge commits `687350d4e942cdda75af42fed8b71ead94f7c1dc`
  and `1e8c32242a42ef816c02bbb4d207f828fa1b62dc`; P07 report commit
  `76a9d68edec80454928bde78599ca8c1dca55fc8` records `P07_READY: true`.

## Delivered modules and compatibility

- `evaluation/v2` defines strict dataset/run models, generated fixtures,
  leakage validation, immutable artifact I/O, retrieval/citation/refusal and
  engineering metrics, provenance-bearing gates, bounded ablations,
  tuning-only selection, safe comparison/reporting, and CLI orchestration.
- `evaluation/datasets/synthetic` contains generated public synthetic content;
  legacy evaluation modules and frozen/results artifacts remain unchanged.
- P07 `RetrievalPolicy` gains additive channel, rerank, and neighbor switches.
  Serving fingerprints now bind those resolved values. No HTTP or SDK schema
  changes and no persisted schema migration are required.
- Evidence assembly can publish multiple distinct citable source spans from one
  Chunk while preserving evidence/document/section budgets and excluding
  separators. Heading-only non-citable nodes no longer reduce citable-source
  coverage. Extractive validation normalizes boundary whitespace consistently.

## Dataset, lanes, and decisions

- Dataset: `p08-synthetic-v2`, 19 cases, tuning 9 / holdout 10, group split by
  document/template family. Final dataset and run manifest SHAs are recorded
  after the committed-code evidence run.
- Lane A `offline-structural`: implemented with generated DOCX, temporary
  SQLite/filesystem, memory named vectors, deterministic provider, and no
  network.
- Lane B `live-primary`: `BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION`.
- Lane C `live-standby`: `BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION`.
- External services actually called: none.
- No decision-pause condition was encountered. `main` and `Industry` remain
  read-only; no production data, database, provider, or network service was
  used.

## Evaluation evidence

Pre-commit controlled runs selected `evidence-cap-8` from the tuning-only
single-variable matrix. An initial immutable diagnostic run exposed duplicate
Chunk evidence inflating nDCG above 1; metric de-duplication and a regression
test were added before accepting evidence. Two subsequent local runs produced
legal `nDCG@10 = 1.0`, passed all configured holdout gates, and passed
`baseline_not_regressed`, including the scope-isolation tuning slice. Because
their manifests predate the feature commit, they are diagnostic only. Final
accepted results are recorded after the implementation commit.

## Gate evidence

- Opening `check`: 1267 passed, 75 deselected, 4 warnings; compile, Ruff, mypy,
  and docstrings passed.
- Opening `smoke`: 69 passed, 1 warning.
- P08 evaluation tests: 18 passed in the latest focused run.
- Dataset validation: 19 cases, 9 tuning, 10 holdout; synthetic public content.
- Final pre-merge and post-merge full gate counts: pending.

## Remaining risks

- Live Jina/Qwen and real reranker/failover quality are unexecuted and block
  production promotion.
- Citation Source Precision on the synthetic holdout exposes broad evidence
  publication despite valid SourceSpans; it is not currently a promotion gate.
- TTFT and Qdrant timing are not applicable to the synchronous memory-vector
  lane. Per-channel latency, SQLite query timing, and artifact reuse are not
  instrumented. Local latency/memory are not production evidence.
- Several table and negative slices have insufficient sample sizes; perfect
  observed values must not be generalized.

## Status

```text
OFFLINE_EVALUATION_READY: pending final gates
PRIMARY_LIVE_EVALUATION_STATUS: BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION
STANDBY_LIVE_EVALUATION_STATUS: BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION
REMOTE_PRODUCTION_PROFILE_READY: false
P08_READY: pending final gates and integration
```
