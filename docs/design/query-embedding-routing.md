# Query Embedding Routing

`RagComponents.query_embedding_router` is a non-null `QueryEmbeddingPort` for
both `single` and `hot_standby` topologies. `SlotEligibilityPort` remains a
static coverage/schema/egress decision helper and is not an embedding caller.

For single topology, the router checks persisted primary coverage and schema,
applies local-versus-remote egress rules, checks the per-operation circuit, then
calls the primary provider with query role policy. There is no invented standby.
Failure becomes an explicit dense-unavailable outcome unless the plan declares
dense mandatory.

For hot standby, routing is primary then standby. Jina can search only
`dense_primary`; Qwen can search only `dense_standby`. The standby coverage,
egress, circuit and budget are checked before query text is sent. One request
emits at most one dense channel and never reuses a cache entry across slots.

Circuit identity is provider, operation and model. Embedding and reranking use
different operation keys. Circuit state is process-local in P07; no claim is
made that it coordinates multiple workers. Half-open work is driven by the next
real authorized request or an explicit synthetic probe, never a paid background
probe.
