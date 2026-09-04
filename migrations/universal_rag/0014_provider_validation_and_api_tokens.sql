CREATE TABLE provider_validation_runs (
    validation_id TEXT PRIMARY KEY CHECK(validation_id GLOB 'val_*'),
    connection_id TEXT NOT NULL REFERENCES provider_connections(connection_id),
    catalog_version TEXT NOT NULL,
    operation TEXT NOT NULL,
    provider_model TEXT NOT NULL,
    credential_key_version INTEGER NOT NULL CHECK(credential_key_version > 0),
    request_policy_identity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
    http_category TEXT NOT NULL,
    dimension INTEGER,
    estimated_tokens INTEGER NOT NULL CHECK(estimated_tokens >= 0),
    observed_tokens INTEGER CHECK(observed_tokens >= 0),
    latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
    safe_error_code TEXT,
    synthetic_payload_hash TEXT NOT NULL CHECK(
        synthetic_payload_hash GLOB 'sha256:*'
    )
);

CREATE INDEX provider_validations_latest
ON provider_validation_runs(connection_id, finished_at DESC);

CREATE TABLE console_sessions (
    session_id TEXT PRIMARY KEY CHECK(session_id GLOB 'sess_*'),
    token_hash TEXT NOT NULL UNIQUE,
    csrf_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    rotated_at TEXT,
    revoked_at TEXT
);

CREATE TABLE api_access_tokens (
    token_id TEXT PRIMARY KEY CHECK(token_id GLOB 'tok_*'),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL,
    project_id TEXT REFERENCES projects(project_id),
    knowledge_base_id TEXT REFERENCES knowledge_bases(knowledge_base_id),
    expires_at TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX api_access_tokens_active
ON api_access_tokens(revoked_at, expires_at);
