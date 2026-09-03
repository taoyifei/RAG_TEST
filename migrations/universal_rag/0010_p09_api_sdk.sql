ALTER TABLE projects ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'
CHECK(lifecycle_status IN ('active', 'archived'));

ALTER TABLE knowledge_bases ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'
CHECK(lifecycle_status IN ('active', 'archived', 'deleting'));

ALTER TABLE documents ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'
CHECK(lifecycle_status IN ('active', 'archived', 'deleting', 'deleted'));

ALTER TABLE document_versions ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'created'
CHECK(lifecycle_status IN ('created', 'indexing', 'ready', 'failed', 'superseded'));

ALTER TABLE ingestion_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0
CHECK(cancel_requested IN (0, 1));

CREATE TABLE idempotency_records (
    scope_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL CHECK(request_hash GLOB 'sha256:*'),
    result_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope_id, operation, idempotency_key)
);

CREATE TABLE ingestion_requests (
    job_id TEXT PRIMARY KEY REFERENCES ingestion_jobs(job_id),
    request_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'queued', 'running', 'succeeded', 'failed', 'cancelled'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE lifecycle_operations (
    operation_id TEXT PRIMARY KEY,
    operation_type TEXT NOT NULL CHECK(operation_type IN (
        'delete_document', 'delete_knowledge_base'
    )),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    document_id TEXT REFERENCES documents(document_id),
    state TEXT NOT NULL CHECK(state IN ('planned', 'running', 'completed', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(operation_type, knowledge_base_id, document_id)
);
