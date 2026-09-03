CREATE TABLE projects (
    project_id TEXT PRIMARY KEY CHECK(project_id GLOB 'prj_*'),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE knowledge_bases (
    knowledge_base_id TEXT PRIMARY KEY CHECK(knowledge_base_id GLOB 'kb_*'),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    profile_id TEXT NOT NULL,
    active_revision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE UNIQUE INDEX knowledge_bases_live_name
ON knowledge_bases(project_id, normalized_name)
WHERE deleted_at IS NULL;

CREATE TABLE documents (
    document_id TEXT PRIMARY KEY CHECK(document_id GLOB 'doc_*'),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    display_name TEXT NOT NULL,
    current_version_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE document_versions (
    document_version_id TEXT PRIMARY KEY CHECK(document_version_id GLOB 'dver_*'),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    source_artifact_id TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, content_sha256)
);

CREATE TABLE ingestion_jobs (
    job_id TEXT PRIMARY KEY CHECK(job_id GLOB 'job_*'),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    document_id TEXT REFERENCES documents(document_id),
    document_version_id TEXT REFERENCES document_versions(document_version_id),
    revision_id TEXT,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'running', 'completed', 'failed_retryable',
        'failed_terminal', 'interrupted'
    )),
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK(attempt >= 0),
    heartbeat_at TEXT,
    error_code TEXT,
    safe_message TEXT,
    retryable INTEGER NOT NULL CHECK(retryable IN (0, 1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(knowledge_base_id, idempotency_key)
);

CREATE TABLE metadata (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(namespace, key)
);
