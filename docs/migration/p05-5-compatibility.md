# P05.5 Compatibility and P06 Migration Notes

## Identity

Old content-only document versions cannot be interpreted as new dvers. The new
identity includes `document_id`; the physical Blob remains content addressed.
P00-P05 synthetic fixtures were regenerated because dver, node IDs, source Blob
refs, and downstream stable IDs changed deterministically.

`document_id` is globally unique across project and knowledge-base scopes. P06
must validate this before creating persistent records.

## Parser callers

All callers now pass `ParseContext` separately from `ParsingPolicy`. The removed
generic policy metadata field has no compatibility fallback because accepting
runtime identity there would contaminate fingerprints. Legacy parser aliases
remain registered and use the same three-argument ParserPort and Artifact
result contract.

## Blob callers

New ingestion code uses `put_if_absent/read/exists/delete` and must retain the
CREATED/EXISTING result until commit or rollback. In-memory `put/get` methods
remain temporary compatibility wrappers for non-parser P00-P05 code; they are
not the P06 transaction API.

## Embedding and chunk consumers

P06 persists vectors per returned `chunk_ids` and slot, including a
`PARTIAL_CACHE_FILLED` result that embeds only missing positions. It must use
the resolved ChunkingPolicy and actual single/hot-standby required slots.
P07 uses `QueryEmbeddingRouter`; static slot eligibility is not an embedding
call router.

Consumers of ChunkingReport must read the calculated character and boundary
fields. A former fixed `source_span_coverage=1.0` value is not compatible
evidence.

## Migration boundary

There is no formal P06 SQLite or Qdrant dataset in this phase, so no database or
vector migration is executed. If such data is later discovered, migration must
be a separate decision and old/new IDs must not be silently equated.

External services actually called: none. These changes do not claim improved
retrieval quality or availability of real Jina, Aliyun, or Qdrant services.
