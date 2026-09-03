# Evaluation V2

P08 adds a versioned, default-offline evaluation path alongside the legacy
evaluation modules. It exercises the production P06 revision builder and P07
retrieval, evidence, citation, and refusal contracts with generated DOCX only.
It does not measure real Jina or Qwen semantic quality.

## Commands

Run from the repository root with the repository Python 3.11 environment:

```bash
.venv/bin/python scripts/dev.py eval-validate-dataset \
  --dataset evaluation/datasets/synthetic
.venv/bin/python scripts/dev.py eval-run \
  --dataset evaluation/datasets/synthetic \
  --profile configs/profiles/dev-offline.json \
  --lane offline-structural
.venv/bin/python scripts/dev.py eval-compare \
  --baseline evaluation/reports/<baseline-run-id> \
  --candidate evaluation/reports/<candidate-run-id>
.venv/bin/python scripts/dev.py eval-report \
  --run evaluation/reports/<run-id>
```

Every run directory is created exclusively. Reusing a run ID or overwriting a
result file fails. Local run directories are ignored by Git because they
contain host-specific timing and memory measurements; accepted evidence is
promoted explicitly into versioned `baselines/` and `manifests/` summaries.

## Split and selection rules

The dataset loader validates strict Pydantic schemas, logical document scope,
fixture coverage, case identities, and group isolation. `tuning` labels may be
used by the bounded single-variable ablation matrix. Selection APIs reject any
report whose split is not `tuning`. The selected candidate then runs once on
`holdout`; tuning and holdout metrics are never averaged.

The offline matrix includes baseline, Exact only, FTS5 only, deterministic
dense-primary only, Exact plus FTS5, rerank bypass, neighbor-expansion bypass,
evidence caps, table-context removal, and 256/320/384 Chunk candidates. Live
dense-standby, Jina reranking, and failover quality remain blocked until a
separately authorized run supplies flags, positive budgets, and credentials.

## Output and safety

A complete run contains canonical observations, structured errors, tuning
metrics, selected holdout metrics, ablation and gate reports, selected config,
and a redacted manifest plus SHA-256 sidecar. The manifest rejects query/body,
secret, vector, prompt, raw-response, and absolute-path fields. It records
external services actually called; the offline value must be empty.

Bootstrap intervals are emitted only with at least five samples. Smaller
slices are marked `insufficient_sample`. `not_executed`, `not_instrumented`,
and `not_applicable` states remain explicit rather than being converted to
numeric zeroes.

## Quality boundary

An offline pass supports claims about deterministic structure, identity,
revision and scope isolation, metric arithmetic, citations, refusal state, and
regression detection. It cannot support claims about provider semantics,
limits, cost, production latency, or remote failover quality. Review
`docs/public/evaluation-and-quality-claims.md` before publishing results.
