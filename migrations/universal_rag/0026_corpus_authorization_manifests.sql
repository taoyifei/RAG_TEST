CREATE TABLE corpus_authorization_manifests (
    manifest_id TEXT PRIMARY KEY CHECK(manifest_id GLOB 'cauth_*'),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    active_index_revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    active_document_digest TEXT NOT NULL CHECK(active_document_digest GLOB 'sha256:*'),
    active_document_count INTEGER NOT NULL CHECK(active_document_count > 0),
    provider_connection_id TEXT NOT NULL REFERENCES provider_connections(connection_id),
    provider_model TEXT NOT NULL,
    operation_binding_identity TEXT NOT NULL CHECK(operation_binding_identity GLOB 'sha256:*'),
    operations_json TEXT NOT NULL,
    authorization_id TEXT NOT NULL UNIQUE,
    budget_campaign_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    approved_by_session_id TEXT NOT NULL REFERENCES console_sessions(session_id)
);

CREATE INDEX corpus_authorization_scope
ON corpus_authorization_manifests(knowledge_base_id, created_at DESC);
