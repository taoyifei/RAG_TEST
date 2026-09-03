# Phase 08 Progress: Frozen Evaluation and Quality Gates

## Integration identity

- Integration base SHA: `76a9d68edec80454928bde78599ca8c1dca55fc8`.
- Feature branch: `codex/p08-evaluation`.
- Feature commits: `579d75c`, `de96e49`, `1cf33a8`, `34480c1`, `ce59cdc`,
  `25490f0`, `6841ce2`, `c8693fc`, and `958b264`.
- Integration merge commit: `f608bc30361b731afd7f18345342b6408dbe7341`
  (`--no-ff`).
- Verified remote feature SHA:
  `958b2645b84b89a4da4ccf96d8db6734de9a98d8`.
- Verified remote gate-tested integration merge SHA:
  `f608bc30361b731afd7f18345342b6408dbe7341`. The later phase-report-only
  commit does not change executable behavior.
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
  document/template family. Dataset SHA is
  `sha256:404d5ed72cdc46da0468673aab92a2e3ba4f406741b36eb84bbe7224876f872a`.
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
test were added before accepting evidence. Runs whose manifests predate their
implementation commits remain diagnostic only.

The accepted committed-code baseline is `p08-20260903T065037Z-945696f5`, with
manifest SHA
`sha256:554485ea613b22bdf593b2e1e355ba942201b0bbf786a23f2a77dbc9a59f1838`.
The reproduction is `p08-20260903T065130Z-945696f5`, with manifest SHA
`sha256:d90e9963a1b4847632996528d634b209caf7baa2a4a348ce18910e0a451264ff`.
Both bind feature SHA `6841ce28f414c5b7063784be5b31f472aa94308e`,
select `evidence-cap-8`, pass every configured holdout gate, and have zero
selected-candidate error records. `eval-compare` reports
`baseline_not_regressed: true` for overall metrics and critical document,
table, revision, scope, and refusal slices.

Holdout Recall@1/5/10, MRR@10, nDCG@10, Answerable Accuracy, Refusal F1,
Citation Presence/Validity/Publishability, Document Recall@10, and Source Range
Coverage are 1.0 for this synthetic deterministic lane. Wrong scope, revision,
and vector-space attempts, unsupported claims, and evidence overflow are zero.
Citation Source Precision is 0.16261022927689595 with 95% bootstrap interval
[0.14638447971781304, 0.17883597883597885], and remains an explicit risk.
On the recorded Linux x86_64, 16-logical-CPU, synchronous in-process baseline,
query latency was p50 6.292 ms and p95/p99 7.736 ms, peak process memory was
115292 KiB, and index-build throughput was 12.690 chunks/s. These host-specific
values are engineering observations, not production performance claims.

## Gate evidence

- Opening `check`: 1267 passed, 75 deselected, 4 warnings; compile, Ruff, mypy,
  and docstrings passed.
- Opening `smoke`: 69 passed, 1 warning.
- P08 evaluation tests: 18 passed in 4.29 seconds.
- Dataset validation: 19 cases, 9 tuning, 10 holdout; synthetic public content.
- Mandatory pre-merge evaluation Run
  `p08-20260903T065434Z-945696f5`: selected `evidence-cap-8`, all holdout
  gates passed, and external services actually called was empty.
- Final pre-merge compileall: passed with no output.
- Final pre-merge Ruff: all checks passed.
- Final pre-merge strict mypy: 274 source files, no issues.
- Final pre-merge Google docstrings: `missing_google_sections=0`.
- Final pre-merge default-offline pytest: 1289 passed, 75 deselected,
  4 warnings in 206.03 seconds.
- Final pre-merge smoke: 71 passed, 1 warning in 7.40 seconds.
- Final pre-merge `git diff --check`: passed.
- Post-merge compileall, Ruff, strict mypy over 274 files, and Google docstrings
  passed; default-offline pytest reported 1289 passed, 75 deselected, and
  4 warnings in 205.56 seconds.
- Post-merge smoke: 71 passed, 1 warning in 7.84 seconds.
- Post-merge `git diff --check`: passed before the integration push.

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
OFFLINE_EVALUATION_READY: true
PRIMARY_LIVE_EVALUATION_STATUS: BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION
STANDBY_LIVE_EVALUATION_STATUS: BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION
REMOTE_PRODUCTION_PROFILE_READY: false
P08_READY: true
```
