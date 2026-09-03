# P07 Retrieval Preflight

## Git baseline

- Audit date: 2026-09-03 (Asia/Hong_Kong).
- Integration branch: `feature/universal-rag`.
- Integration base: `bfcc8d271b3bbf73aa66f9b596eff902cdf9acb4`.
- P05.5 anchor `bb13930ba3b06bcfcecb3f0acdf7c023e16afe1b` is an ancestor of the integration base.
- P06 merge: `687350d4e942cdda75af42fed8b71ead94f7c1dc`.
- P06 report commit: `bfcc8d271b3bbf73aa66f9b596eff902cdf9acb4`.
- Feature branch: `codex/p07-adaptive-retrieval`, created from the integration base.
- The starting worktree was clean. `main` and `Industry` are read-only at
  `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` and
  `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` respectively.

## P06 persisted contracts observed

- SQLite schema version 5 owns project/KB/document identity, immutable revision
  state, canonical Chunk V3 rows, FTS5 rows, exact identifiers, embedding slots,
  coverage, cache, budget, active history, blob references and GC plans.
- `SqliteControlStore.active_revision_id()` and `revision_vector_spec()` expose
  persisted active identity and the exact named-vector schema. P07 will add one
  transactional query-snapshot read so a request cannot mix revisions.
- `SqliteFtsStore.search()` takes a revision-bound `LexicalSearchRequest` and
  returns 1-based ranks. `search_exact()` uses the versioned exact table and a
  controlled identifier normalizer.
- `VectorStorePort.search_named()` requires explicit slot and vector name;
  Memory and Qdrant adapters reject cross-space requests.
- `SqliteControlStore.chunk_rows()` is the canonical hydration source. Vector
  payloads are identities only and are not trusted as answer text.
- P06 tests cover process reopen, FTS5 special-character safety, exact lookup,
  deterministic vector query, and complete `dense_primary` plus
  `dense_standby` counts for hot standby.

## Executed gates

- `python scripts/dev.py doctor`: Python 3.11.15, source-tree package import,
  SQLite FTS5 3.51.2 and temporary directory checks passed; Node was an expected
  optional skip.
- A synthetic DOCX was ingested into a temporary persistent P06 runtime. The
  resulting active revision was reopened and validated with
  `python scripts/dev.py index-validate`.
- Validation evidence: 1 document, 1 chunk, 1 FTS row, 1
  `dense_primary` vector, source-span coverage 1.0, zero listed structural
  violations, and deterministic probe passed.
- Opening `python scripts/dev.py check`: compileall, Ruff, mypy over 234 source
  files and Google docstrings passed; pytest reported 1226 passed, 75 deselected,
  4 warnings.
- Opening `python scripts/dev.py smoke`: 63 passed, 1 warning.
- External services called: none. Providers were deterministic and data was
  synthetic in a temporary directory, which was removed after validation.

## Implementation consequences

P07 may proceed. It must introduce a single immutable Active Revision Query
Snapshot read, a non-null query embedding port for both topologies, bounded
channels and candidate hydration from SQLite. It must not replace P06 storage,
change identity rules, freeze P08 quality parameters, or switch the legacy HTTP
path by default.
