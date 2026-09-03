# Search and Answer

The P07 application service accepts a project/knowledge-base-scoped query and
returns the actual revision and fingerprint summary, route and rerank modes,
ranked evidence, confidence/refusal status, an extractive answer when supported,
and a safe trace identifier.

`DENSE_UNAVAILABLE` means query embedding or its selected vector space could not
be used. It does not mean the knowledge base is empty; exact and FTS5 retrieval
may continue. `INDEX_NOT_READY` means there is no active immutable revision.
`INDEX_CORRUPT` means persisted channel identities cannot be hydrated from the
canonical SQLite chunk store and fails closed.

Answers are extractive in P07. Support IDs are assigned by the application and
must resolve to citable source spans. Generated rewrite text is retrieval-only
and cannot become evidence. Query text, vectors, provider bodies, prompts,
secrets and private candidate bodies are omitted from default traces.

The P07 final-result cache is process-local and reads only after the actual
embedding slot and rerank execution mode are known. Its hashed key binds
project, knowledge base, active revision, serving fingerprint, filters, access
policy, conversation and rewrite identity. It does not store the raw query;
the authorized result may remain in process memory only for the runtime
lifetime and is cleared when that runtime closes. P07 does not provide a
durable or cross-worker answer cache.

P07 parameters are provisional and no semantic-quality or production-readiness
claim is made without P08 evaluation.
