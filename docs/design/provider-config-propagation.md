# Provider Configuration Propagation

## Composition rule

`RagProfile` owns the resolved `ParsingPolicy`, `ChunkingPolicy`, embedding
topology, and provider fields. The composition root passes those values into
strict adapter config models; it does not recreate default policies. Public
fields either affect the adapter request/config or are rejected while loading
the profile.

## Mapping matrix

| Profile input | Adapter config and request effect |
| --- | --- |
| Jina slot/provider/model/dimension | Slot validation, descriptor, request model/dimensions |
| Jina API-key env | Exact environment variable read at call time |
| Jina document/query task | Role-specific request `task` |
| Jina embedding type/normalization | Request `embedding_type` and `normalized` |
| Jina document/query egress | Role-specific pre-transport denial |
| Jina request-policy/adapter revision | Role result identity, cache identity, topology fingerprint |
| Qwen slot/provider/model/dimension | Slot validation, descriptor, request model/dimension |
| Qwen API/workspace/region env | Exact environment variable reads and endpoint construction |
| Qwen region/transport | Strict `cn-beijing` native transport validation |
| Qwen document/query text type | Role-specific request `text_type` |
| Qwen query instruct/output type | Query request `instruct` and request `output_type` |
| Qwen egress/policy revisions | Role denial, result identity, cache/fingerprint identity |
| Jina Reranker model/API env/egress | Strict model, exact env read, pre-transport denial |
| Reranker max tokens/candidates/revision | Local input limits and serving-policy identity |

Jina v5 deviations from its supported model, 1024 dimension, tasks,
embedding type, and normalization fail during profile validation. Qwen3.7
deviations from its model, 1024 dimension, Beijing native transport, text
types, dense output, or normalization fail at the same boundary.

MockTransport tests inspect actual JSON bodies and authorization derived from
custom environment-variable names. Adapter construction and health inspection
perform no network call. Reranker token/candidate limits, request-policy
revision, egress mode, and environment-variable name also enter the Serving
Fingerprint, so changing their behavior cannot reuse the old search cache.

## Router and cache boundary

`SlotEligibilityPort` is the compatibility name for static coverage, schema,
and egress eligibility. `QueryEmbeddingRouter` performs actual primary/standby
calls and owns circuit and local budget state. The old
`EmbeddingFailoverRouter` name is a compatibility alias only; new P07 wiring
uses `query_embedding_router` from `RagComponents`.

Embedding cache keys contain the slot, provider, model, dimension,
normalization, role-specific request-policy identity, adapter revision, and a
text digest. Search-cache keys reserve project, knowledge base, active index
revision, serving fingerprint, selected slot, rerank mode, query digest,
filters, conversation identity, and rewrite identity.
