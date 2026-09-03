# Reranker Circuit and Bypass

Reranking receives a bounded fusion prefix. Candidate text is a deterministic
heading context plus citation window and never an unbounded embedding payload.
The application checks rerank egress and a dedicated
`(jina-reranker, reranking, model)` circuit before invoking the provider.

A successful response must cover a complete, unique and in-range candidate set
with finite scores. Equal scores preserve fusion input order. Failure never
writes zero scores; the service keeps the RRF order and records one explicit
bypass mode: provider unavailable, policy denied, circuit open, or disabled by
plan. Must-keep exact candidates are restored under a bounded cap without
crossing access or scope filters.

The circuit is deliberately independent from Jina embedding. Its state is
process-local and is surfaced as safe trace metadata only.
