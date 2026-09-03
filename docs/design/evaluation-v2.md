# Evaluation V2 Design

## Purpose and trust boundary

P08 measures the production P06/P07 contracts without turning deterministic
fixtures into semantic-quality evidence. Offline runs prove dataset/schema
validation, identity and revision isolation, structural retrieval behavior,
metric arithmetic, evidence publication, refusal behavior, reproducibility,
and failure detection. Only separately authorized live lanes may support claims
about Jina or Qwen semantic quality, limits, cost, or failover quality.

The legacy evaluation modules and frozen deployment artifacts remain intact.
P08 code lives under `evaluation/v2`; JSON Schemas live under
`evaluation/schema`; versioned synthetic inputs live under
`evaluation/datasets`; immutable outputs live under
`evaluation/reports/<run_id>`.

## Dataset contract

Each case has schema version 2, a stable case and group identity, split,
category, difficulty, severity, logical scope, query, expected logical document
and chunk/source facts, and structural constraints. Dataset validation rejects
unknown fields, duplicate case IDs, missing group IDs, unknown document IDs,
invalid logical identities, and group leakage across tuning and holdout.

Documents are generated from non-sensitive declarative fixture specs. Content
bytes and display names are separate inputs. This permits explicit cases for
same bytes under different document IDs, rename-only versions, content changes,
same names with different content, similar documents in another KB, empty and
omitted table columns, spans, merges, multi-row headers, identifiers, and
negative/refusal conditions.

The dataset manifest records the split algorithm and group mapping. Tuning APIs
can read labels only for tuning cases. Parameter selection rejects holdout
inputs. Holdout is evaluated only with an already selected candidate; any later
tuning requires a new holdout version or explicit demotion of the observed set.

## Run state machine and manifest

A run uses an exclusive, never-overwritten directory. A successful run writes
the final `complete` manifest; a failed run writes a safe `FAILED.json` marker
without replacing partial evidence. A complete run contains:

- canonical run manifest and its SHA-256 sidecar;
- case observations and metrics, each with a recorded SHA-256;
- structured error records;
- per-category slices and bootstrap intervals where sample size is sufficient;
- a gate result with threshold provenance;
- a redacted environment/package inventory.

The manifest binds Git integration SHA, active revision, index and serving
fingerprints, profile, actual provider/model/slot/vector identity, request-policy
identities, adapter revisions, parser/chunker/tokenizer identity, resolved
retrieval/evidence/refusal parameters, dataset digest and case IDs, lane, seed,
network authorization, external calls, package versions, and result digests.
Full query/document text, secrets, absolute paths, prompts, vectors, and raw
provider bodies are forbidden.

## Offline runner

The offline runner derives an evaluation-only persistent composition from the
requested no-egress deterministic profile. It resolves the accepted P05.5
`docx-ooxml-v4` parser and `docx-structural-v3` chunker, enables the parser's
explicit footnote/endnote parse policy for structural coverage, and replaces
persistent destinations with temporary SQLite/filesystem plus an in-memory
named-vector store. Deterministic provider, generator, and deny-all egress
semantics remain fixed. Both requested and derived profile identities are
recorded.

The runner generates DOCX bytes, writes global document identities through the
control store, builds and activates immutable revisions, validates each active
revision, and queries through `RetrievalService`. It records actual
revision/slot/vector/rerank/evidence/refusal outcomes and production SourceSpan
validation. Temporary state is removed when the process exits.

Evaluation variants are explicit single-variable changes. Retrieval channel,
rerank, and neighbor controls are additive provisional `RetrievalPolicy`
fields; chunk-size candidates build new revisions. Each variant receives a
separate index/serving identity and result block. Primary and standby results
are never averaged or score-merged.

## Metrics and gates

Retrieval metrics include Recall@1/5/10, MRR@10, nDCG@10, exact identifier
hit@1/5, table-row recall, document recall, source-range coverage, negative
leakage, and wrong scope/revision/vector-space counts. Answer metrics include
answerable accuracy, refusal precision/recall/F1, citation presence/validity,
publishability, citation source precision, unsupported claims, and evidence
budget overflow. Missing channels are represented as not executed, not numeric
zeroes folded into means.

Engineering output records process latency percentiles, per-stage latency when
available, provider calls/retries, failover and reranker bypass counts, cache
hits, SQLite activity, vector search timing, peak memory, build throughput, and
artifact reuse. Comparisons are valid only for the same host, dataset, profile,
and process mode.

`evaluation/gates/p08-gates.json` stores values plus provenance. Safety
counters and deterministic citation/claim checks are absolute. Quality and
performance gates remain explicitly provisional until accepted product
requirements or a historical real-provider baseline exists. A candidate cannot
pass when a critical category regresses, minimum sample size is not met, or any
scope/revision/vector-space violation is observed.

## CLI and network safety

The four commands are `eval-validate-dataset`, `eval-run`, `eval-compare`, and
`eval-report`. Offline execution resolves only the deterministic provider and
never reads provider credentials. A live lane requires `--live-provider`,
`--acknowledge-egress`, positive request and token budgets, an approved live
profile, and required credential names. Missing authorization is a refusal,
never a skipped pass.

The current P08 delivery deliberately fails closed after those guards: no live
provider runtime is constructed without a separate authorized implementation
run. In this environment both live lanes are `BLOCKED_NO_CREDENTIALS` and are
not reported as passes.

Optional LLM judging, Rewrite, HyDE, and production-default algorithm changes
are out of scope and stay disabled. No command starts a network service or reads
the production data directory implicitly.
