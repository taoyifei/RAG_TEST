# Retrieval Pipeline V1

P07 adds a synchronous application pipeline over the P06 immutable index. One
request captures one `ActiveRevisionQuerySnapshot` and passes it unchanged
through analyze, plan, bounded expansion, exact, FTS5, one routed dense search,
RRF, canonical hydration, optional rerank, structural expansion, evidence
packing, confidence, extractive generation and citation validation.

The control store validates project, knowledge base, active state and revision
scope in one read transaction. Every channel request repeats the immutable
revision identity. A missing active pointer returns `INDEX_NOT_READY`; a missing
or mismatched canonical chunk returns `INDEX_CORRUPT` and generation stops.

Channel scores remain local diagnostics. Fusion uses only 1-based ranks with
provisional `weight=1` and `k=60`; each contribution is retained. Equal totals
sort by best channel rank, must-keep exact status, then chunk ID. These values
remain provisional until P08.

Expansion occurs only after rerank or explicit bypass. Same-group links must be
bidirectional and remain in one document version, section and neighbor group.
Table and section context is bounded. Evidence is deduplicated first by chunk,
then by citable source-span identity, and packed under document, section and
token caps.

SAFE trace events retain counts, reason codes, actual slot/vector identity,
circuit state, rerank mode, cache outcome and rank-only RRF contributions. They
do not retain query text, candidate bodies, vectors, provider bodies, prompts
or secrets.

The legacy `rag_app.query_service.QueryService` stays unchanged and remains the
runtime default. A compatibility adapter may delegate explicitly to the P07
service; shadow and migration selection are development concerns until P08/P09.
