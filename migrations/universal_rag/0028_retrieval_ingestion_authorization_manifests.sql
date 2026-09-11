CREATE TABLE retrieval_ingestion_authorization_manifests (
    manifest_id TEXT PRIMARY KEY CHECK(manifest_id GLOB 'riauth_*'),
    job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    profile_revision_id TEXT NOT NULL REFERENCES retrieval_profile_revisions(profile_revision_id),
    predecessor_index_revision_id TEXT REFERENCES index_revisions(index_revision_id),
    target_index_revision_id TEXT NOT NULL CHECK(target_index_revision_id GLOB 'irev_*'),
    source_binding_digest TEXT NOT NULL CHECK(source_binding_digest GLOB 'sha256:*'),
    active_document_digest TEXT NOT NULL CHECK(active_document_digest GLOB 'sha256:*'),
    source_document_count INTEGER NOT NULL CHECK(source_document_count > 0),
    source_size_bytes INTEGER NOT NULL CHECK(source_size_bytes > 0),
    profile_binding_identity TEXT NOT NULL CHECK(profile_binding_identity GLOB 'sha256:*'),
    operations_json TEXT NOT NULL,
    authorization_id TEXT NOT NULL UNIQUE,
    budget_campaign_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    approved_by_session_id TEXT NOT NULL REFERENCES console_sessions(session_id)
);

CREATE INDEX retrieval_ingestion_authorization_job
ON retrieval_ingestion_authorization_manifests(job_id, created_at DESC);

CREATE INDEX retrieval_ingestion_authorization_profile
ON retrieval_ingestion_authorization_manifests(
    profile_revision_id,
    target_index_revision_id,
    created_at DESC
);
