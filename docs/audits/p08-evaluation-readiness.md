# P08 Evaluation Readiness Audit

## Git and phase prerequisites

- Audit date: 2026-09-03 (Asia/Hong_Kong).
- Integration branch: `feature/universal-rag`.
- Integration base: `76a9d68edec80454928bde78599ca8c1dca55fc8`.
- P05.5 anchor `bb13930ba3b06bcfcecb3f0acdf7c023e16afe1b` is an
  ancestor of the integration base.
- P06 merge: `687350d4e942cdda75af42fed8b71ead94f7c1dc`.
- P07 merge: `1e8c32242a42ef816c02bbb4d207f828fa1b62dc`.
- P07 report commit: `76a9d68edec80454928bde78599ca8c1dca55fc8`.
- `docs/progress/phase-07.md` records `P07_READY: true`, including offline
  E2E, failure-injection, citation, full-check, and smoke evidence.
- Feature branch: `codex/p08-evaluation`, created from the integration base.
- The starting worktree was clean. `main` and `Industry` are read-only at
  `af30f81fbcbd0577c16fbf59bb9bce8f29a3de91` and
  `5cc5d7bcc28a2ebd8e61dbc511930b99cfbe324a` respectively.

## Opening gates

- `python scripts/dev.py doctor`: Python 3.11.15, editable package plus
  source-tree import, SQLite FTS5 3.51.2, Git root, and temporary directory
  checks passed; Node was an expected optional skip.
- `python scripts/dev.py check`: compileall, Ruff, strict mypy over 262 source
  files, and Google docstrings passed; pytest reported 1267 passed, 75
  deselected, and 4 warnings.
- `python scripts/dev.py smoke`: 69 passed and 1 warning.
- `git diff --check`: passed.
- External services actually called: none.

## Persisted and serving contracts observed

SQLite schema version 5 stores immutable index revisions, revision-bound
documents and canonical Chunk V3 rows, FTS5 and exact rows, named embedding
slots and coverage, project-scoped embedding cache, provider usage, active
revision history, and GC plans. `active_query_snapshot()` freezes the ACTIVE
revision, index fingerprint, serving fingerprint, topology, named-vector spec,
lexical/exact namespaces, payload schema, and resolved `RetrievalPolicy` for a
request.

P07 exposes strict `SearchRequest`, `QueryAnalysis`, `RetrievalPlan`,
`SearchAnswerResult`, `EvidenceItem`, `ConfidenceDecision`, and redacted
`TraceEvent` models. Candidate text is hydrated from canonical SQLite chunks;
vector payload text is not trusted. Extractive publication uses the production
Support ID and SourceSpan validator and refuses separator or non-evidence text.

Current P07 retrieval values are provisional: channel top-k 24, fusion limit
48, RRF k 60, rerank candidates 24, one neighbor per side, section cap 2,
evidence budget 1024 tokens, per-document cap 4, per-section cap 3, and
must-keep cap 3. The confidence rules and default channel/rerank/expansion
selection are also provisional.

## Profiles, topology, and fingerprints

- `dev-offline`: deterministic single slot `primary` / `dense_primary`,
  lexical-overlap reranker, no egress. Its current index fingerprint is
  `sha256:463369eea01bc990cdfb9dbc7f4293524cbd7a5b1837bdd038c21bbc4f5f797f`
  and serving fingerprint is
  `sha256:2b45471196035095b3aefa12f60042151c4a1fad15b6fa1e8915b65472ccf4ee`.
- `dev-jina-only`: Jina v5 small, retrieval passage/query policies, 1024
  dimensions, L2 normalization, and `dense_primary`.
- `dev-jina-qwen37-hot-standby`: the same Jina primary plus Qwen3.7
  `document`/`query` request roles, the accepted query instruction, 1024
  dimensions, L2 normalization, and `dense_standby`. Jina reranker v3.5 is
  configured separately.
- Equal dimensions do not make the two dense spaces compatible. Every run must
  bind the actual slot and vector name and count any cross-space attempt.

## Evaluation and data readiness

The existing top-level `evaluation/dataset.py`, `evaluation/metrics.py`,
`evaluation/frozen/`, and `evaluation/results/` belong to an older deployment
acceptance workflow. P08 will not overwrite these artifacts. The v2 framework
uses versioned schemas and datasets, while every execution writes a new
`evaluation/reports/<run_id>/` directory with exclusive creation.

P08 synthetic fixtures contain only generated OOXML and invented identifiers,
facts, and tables. They contain no enterprise documents, secrets, user paths,
or production knowledge-base data. Group identities bind document/template
families so renamed and near-duplicate variants cannot cross tuning/holdout.

## Lane decision

- Lane A, `offline-structural`: executable. It uses generated DOCX, temporary
  SQLite/filesystem state, an in-memory named-vector store, deterministic
  embeddings, and the production P06/P07 parser, chunker, revision, retrieval,
  SourceSpan, citation, and refusal contracts.
- Lane B, `live-primary`: `BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION`.
- Lane C, `live-standby`: `BLOCKED_NO_CREDENTIALS_AND_EGRESS_AUTHORIZATION`.

No Jina, DashScope, workspace, or region credential is present in the audited
environment. More importantly, the user supplied no explicit authorization to
send even synthetic evaluation text to providers in this run. Live commands
must require both flags and bounded request/token budgets before checking
credentials or constructing a provider runtime.

P08 may declare the offline framework ready after all mandatory gates pass.
`REMOTE_PRODUCTION_PROFILE_READY` must remain false or blocked unless both live
lanes are explicitly authorized, executed, and pass accepted gates.
