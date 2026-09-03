# P06-P08 midterm code-fact audit

## Audit boundary

- Phase: P08.5 quality hardening.
- Repository: `taoyifei/RAG_TEST`.
- Actual start SHA: `55de66bd872756d7c80929c1560be44daf3a700c`.
- Integration branch at audit: `origin/feature/universal-rag` at the same SHA.
- Feature branch: `codex/p08-5-quality-hardening`.
- `main` and `Industry` remain read-only.
- External services actually called: none.

## Commit graph and phase anchors

The first-parent integration history at the start of this phase is:

```text
55de66b docs(progress): record P08 integration evidence
f608bc3 merge: integrate P08 evaluation gates
76a9d68 docs(progress): record P07 integration evidence
1e8c322 merge: integrate P07 adaptive retrieval
bfcc8d2 docs(progress): record P06 integration evidence
687350d merge: integrate P06 local persistence
bb13930 merge: sync P05.5 fingerprint evidence
```

All required ancestors were verified against `origin/feature/universal-rag` with
`git merge-base --is-ancestor` and returned exit status 0:

- P05.5 anchor `bb13930ba3b06bcfcecb3f0acdf7c023e16afe1b`.
- P06 merge `687350d4e942cdda75af42fed8b71ead94f7c1dc`.
- P07 merge `1e8c32242a42ef816c02bbb4d207f828fa1b62dc`.
- P08 merge `f608bc30361b731afd7f18345342b6408dbe7341`.

## Applied migration identity

P08.5 will not edit migrations 0001-0005. Their start-of-phase SHA-256 values
are:

| Migration | SHA-256 |
| --- | --- |
| `0001_control.sql` | `3d89ee3bd0a9e5aeaa2b630cd92f45589d1968a8fd43d85377a7153f3b658d0b` |
| `0002_artifacts.sql` | `dc1e0d00d230634ad1c65df688808330ad98c82e18377a1f04778918aa60a374` |
| `0003_revisions_chunks.sql` | `8661cc87f85b75ec29979b40c8fc2c54df06fb1f347c8d410265384b941232c6` |
| `0004_fts5.sql` | `af20e249520295263f1592da806db84aff2ad0fad4ab9dd3f53e1396c8005e49` |
| `0005_embedding_cache_gc.sql` | `459d3fc22f5952f608155ccd73f34cc40bad42db98289d15ec4881507d918c7e` |

## P06 persistence facts

### SQLite and active pointer

The current schema has single-column foreign keys for project, knowledge-base,
document, version, revision, and chunk identities. It does not constrain all
cross-row scope relationships. In particular, raw SQL can currently pair a
document with a knowledge base from another project, pair a revision document
with a version from another document, or set `knowledge_bases.active_revision_id`
without proving that the revision belongs to that knowledge base and is ACTIVE.
Application checks exist around normal activation, but they are not a database
integrity boundary. `active_revision_history` likewise has no cross-KB checks.

### Blob catalog

`blob_objects` records staged, available, and quarantine catalog states and
`blob_references` records logical ownership. Current GC only plans cataloged
staged objects without references. It does not inventory the controlled
filesystem and therefore cannot distinguish physical-only files from
catalog-only corruption. Physical deletion is followed by catalog deletion or
state rollback in one process, but there is no durable per-item intent spanning
crashes.

### Revision writer and state

`ingestion_jobs` provides idempotency per knowledge base and heartbeats. The
validator counts RUNNING writers. There is no database-backed single-writer
lease or fencing token for a deterministic revision, so a stale process can
continue writing after another process recovers the same build. Job and
revision stages are updated separately, and the schema does not encode slot
coverage in the reported job stage.

### Garbage collection

`gc_plans` persists a snapshot and plan hash, but an apply deletes each vector
namespace before the SQLite revision in a non-resumable loop. Only unprotected
RETIRED revisions are candidates. Retained terminal/retryable/interrupted
failures and a physical/catalog blob difference are not represented as durable
plan items.

## Vector-store facts

`VectorStorePort.validate_vector_revision()` currently returns only aggregate
point/vector counts and an invalid count. `RevisionValidator` compares those
counts with canonical chunk counts and retains a deterministic fetch/search
probe, but it does not compare every stored point identity and payload field to
the canonical chunk set.

The Qdrant implementation scrolls all pages and converts records into
`NamedVectorPoint`. Its validation path calls `_all_points(...,
tolerate_invalid=True)`, which drops unconvertible raw records before setting
`point_count`; the returned invalid count therefore cannot reveal those raw
records. Pagination has no repeated/invalid-offset guard. Point IDs are not
proved to equal `vector_point_id(revision_id, chunk_id)`. The Memory adapter
checks scope, required names and dimensions, but does not apply the complete
Qdrant/canonical identity and finite/non-zero-vector rules.

## Lexical facts

`chunks_fts` uses FTS5 `unicode61`. Documents are inserted with original
`lexical_text`, title, heading, and identifier forms. `build_fts_query()` applies
NFKC/casefold and adds CJK bigrams only to the query, joining every token with
unbounded OR. Document and query analysis are therefore asymmetric. There is
no analyzer port or analyzer identity in stored FTS rows. Existing revision
contracts carry a lexical schema JSON blob, but v1 rows are not explicitly
separated from a future v2 reader.

## Retrieval failure and cache facts

`RetrievalService` catches every unexpected `Exception` from Exact and Lexical
and converts it to a degraded empty channel. This can hide SQLite corruption,
schema errors, malformed canonical JSON, and programmer errors. Dense has more
specific handling.

The result-cache key includes selected slot and rerank mode, so it is computed
and looked up only after query embedding, vector search, hydration, and
reranking. It cannot prevent provider calls. A cache hit replaces `trace_id`
but does not explicitly mark the returned result as a hit. The cache is distinct
from the existing provider/embedding cache and must stay so.

## Evidence and confidence facts

`EvidenceAssembler` receives only candidates and `RetrievalPolicy`. It moves
the first chunk from each document ahead of remaining chunks before selecting
every citable span that fits per-document, per-section and token caps. It has no
query, query-kind, rerank-mode, or slot context; diversity can therefore promote
low-relevance documents and broad chunks can publish multiple weak spans.

`ConfidenceEvaluator` derives Exact and Lexical support from any reranked
candidate, not specifically the candidates that produced final evidence.
Dense-only evidence is always insufficient, with no explicit calibrated-policy
mechanical path. Ambiguous support counts evidence items, so two unrelated spans
from one chunk can satisfy the numeric minimum.

## P08 observation and metric facts

`evaluation.v2.runtime._observe()` deduplicates `result.evidence` by chunk and
assigns that order to `retrieved_chunk_ids`. Recall, MRR, nDCG and negative
leakage in `evaluation.v2.metrics` consequently measure selected evidence, not
channel/fusion/rerank retrieval stages. Source-range evaluation counts matched
expectations but has no false-positive denominator. The published
`citation_source_precision` is document precision under an ambiguous name.

`provider_call_count` is inferred in the evaluation runtime, retries are fixed
to zero, and cache hit defaults rather than coming from safe retrieval
diagnostics. Per-channel/provider latency and actual retry counters are absent.
The synthetic v2 dataset is below the requested V3 coverage and source-range
labels cannot disambiguate repeated equal text with an occurrence or structural
anchor.

The existing P08 reports and `evidence-cap-8` variant are retained as diagnostic
evidence. P08.5 will mark them superseded rather than deleting them.

## P08.5 contract and compatibility plan

P08.5 will add, without replacing the existing package:

- safe vector-point audit and revision-inventory Core models plus
  `VectorStorePort.audit_revision()`; the old aggregate validator remains as a
  compatibility wrapper;
- additive migrations beginning at 0006 for scope integrity, writer leases,
  FTS analyzer v2, and durable GC items;
- a synchronous infrastructure-free lexical-analyzer port and deterministic
  local implementation shared by indexing and queries;
- additive writer-lease/fencing operations on the SQLite control adapter;
- safe retrieval diagnostics and a provider-independent base result-cache key;
- query-aware evidence context and calibrated dense-confidence policy fields;
- additive Evaluation V3 observation/schema/metric fields, with deprecated
  aliases only where compatibility requires them.

Old v1 FTS revisions will never be silently interpreted as v2. They will use an
explicit legacy reader or fail with `REINDEX_REQUIRED`; no old revision row or
migration checksum will be rewritten. Full Diagnostics remain internal and
contain IDs, ranks, safe scores/counters/reason codes only. They exclude query
text, source text, vectors, prompts, provider bodies, secrets, and absolute
paths.

## Opening-gate record

All Python gates ran with `RAG_TEST_NETWORK=offline` and the repository
`.venv/bin/python`:

- `scripts/dev.py doctor`: passed; Python 3.11.15, source-tree project import,
  SQLite 3.51.2 with FTS5, writable temporary directory; optional Node skipped.
- `scripts/dev.py check`: passed compileall, Ruff, strict mypy over 274 source
  files, Google docstrings, and default-offline pytest with 1289 passed, 75
  deselected, and 4 warnings in 203.82 seconds.
- `scripts/dev.py smoke`: 71 passed and 1 warning in 8.42 seconds.
- The brief's historical `scripts/dev.py eval --offline` spelling was executed
  and failed at argument parsing because `eval` is not a current command. The
  authoritative P08 entry documented by this checkout is `scripts/dev.py
  eval-run --dataset evaluation/datasets/synthetic --profile
  configs/profiles/dev-offline.json --lane offline-structural`.
- The authoritative offline run passed all configured gates as
  `p08-20260903T080946Z-945696f5`, selected the existing diagnostic
  `evidence-cap-8` candidate, and reported `external_services_actually_called`
  as an empty list.
- `git diff --check`: passed.

The old command's argument error is retained as entrypoint-drift evidence; the
successful current command is the opening evaluation gate. No external service
was called during the opening gates.
