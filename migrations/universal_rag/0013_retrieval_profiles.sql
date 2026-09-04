CREATE TABLE retrieval_profile_revisions (
    profile_revision_id TEXT PRIMARY KEY CHECK(profile_revision_id GLOB 'pfr_*'),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'validating', 'active', 'retired'
    )),
    primary_connection_id TEXT NOT NULL REFERENCES provider_connections(connection_id),
    primary_embedding_model TEXT NOT NULL,
    primary_dimension INTEGER NOT NULL CHECK(primary_dimension > 0),
    primary_document_policy_json TEXT NOT NULL,
    primary_query_policy_json TEXT NOT NULL,
    standby_connection_id TEXT REFERENCES provider_connections(connection_id),
    standby_embedding_model TEXT,
    standby_dimension INTEGER,
    standby_document_policy_json TEXT,
    standby_query_policy_json TEXT,
    reranker_connection_id TEXT REFERENCES provider_connections(connection_id),
    reranker_model TEXT,
    failover_enabled INTEGER NOT NULL CHECK(failover_enabled IN (0, 1)),
    standby_budget_json TEXT NOT NULL,
    retrieval_policy_json TEXT NOT NULL,
    evidence_policy_json TEXT NOT NULL,
    index_semantic_fingerprint TEXT NOT NULL
        CHECK(index_semantic_fingerprint GLOB 'sha256:*'),
    serving_fingerprint TEXT NOT NULL CHECK(serving_fingerprint GLOB 'sha256:*'),
    created_at TEXT NOT NULL,
    activated_at TEXT,
    CHECK((standby_connection_id IS NULL) = (standby_embedding_model IS NULL)),
    CHECK((reranker_connection_id IS NULL) = (reranker_model IS NULL))
);

CREATE UNIQUE INDEX retrieval_profile_one_active
ON retrieval_profile_revisions(knowledge_base_id)
WHERE status = 'active';

CREATE INDEX retrieval_profiles_scope
ON retrieval_profile_revisions(knowledge_base_id, created_at);
