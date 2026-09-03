# Evaluation and Quality Claims

## What P08 offline evidence establishes

The P08 offline lane uses generated, non-sensitive DOCX fixtures, temporary
local state, deterministic embeddings, and production P06/P07 contracts. A
passing run can establish that the tested code preserves logical document and
version identity, Active Revision and knowledge-base isolation, named-vector
routing checks, structural table context, source-bound citations, deterministic
refusal behavior, and reproducible metric and regression-gate computation.

The frozen synthetic dataset has 19 cases grouped by document/template family:
9 tuning and 10 holdout. The holdout result is reported separately and is not
fed back into parameter selection. Small category slices explicitly carry
`insufficient_sample`, even when their observed value is perfect.

## What it does not establish

Offline deterministic embeddings do not establish Jina or Qwen semantic
quality, remote reranker benefit, provider limits, failover quality, cost, or
production availability. Local in-process latency and memory do not establish
deployment performance. Neither provider lane ran in P08 because credentials
and explicit egress authorization were absent.

The offline-selected `evidence-cap-8` policy is provisional and applies only to
this synthetic evidence. It is not a production parameter freeze. In
particular, the current extractive generator publishes all supported evidence
within its budget, so Citation Validity can be 1.0 while Citation Source
Precision remains materially lower. Those two metrics must not be conflated.

`REMOTE_PRODUCTION_PROFILE_READY` remains false until both Jina primary and
Qwen standby lanes are separately authorized, run with bounded budgets, and
pass accepted gates. Equal 1024 dimensions never permit searching a Jina query
vector in `dense_standby` or a Qwen query vector in `dense_primary`.

## Publication rules

- State the exact dataset SHA, run/manifest SHA, lane, split, sample count, and
  interval status with every numerical claim.
- Label thresholds by their recorded source. Provisional engineering gates are
  not accepted product requirements.
- Keep tuning, holdout, primary, and standby results separate.
- Report blocked or not-instrumented fields literally; never convert them into
  a pass or a numeric zero.
- Do not publish query text, private document content, secrets, absolute paths,
  raw provider responses, or embeddings from run artifacts.
- If holdout results influence further tuning, version a new holdout or demote
  the observed set to tuning before making another generalization claim.
