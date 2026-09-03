CREATE TABLE embedding_cache (
    cache_key TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL CHECK(
        scope_kind IN ('knowledge_base', 'project', 'global')
    ),
    scope_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    normalization TEXT NOT NULL,
    role_policy_identity TEXT NOT NULL,
    adapter_revision TEXT NOT NULL,
    text_sha256 TEXT NOT NULL CHECK(length(text_sha256) = 64),
    vector_encoding_version TEXT NOT NULL,
    vector_bytes BLOB NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);

CREATE INDEX embedding_cache_scope
ON embedding_cache(scope_kind, scope_id, slot_id, text_sha256);

CREATE TABLE job_provider_usage (
    job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id),
    slot_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    requests INTEGER NOT NULL CHECK(requests >= 0),
    estimated_tokens INTEGER NOT NULL CHECK(estimated_tokens >= 0),
    observed_tokens INTEGER,
    chunks INTEGER NOT NULL CHECK(chunks >= 0),
    retries INTEGER NOT NULL CHECK(retries >= 0),
    elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms >= 0),
    status_category TEXT NOT NULL,
    PRIMARY KEY(job_id, slot_id, provider_id)
);

CREATE TABLE gc_plans (
    plan_id TEXT PRIMARY KEY CHECK(plan_id GLOB 'gcplan_*'),
    database_identity TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL CHECK(plan_hash GLOB 'sha256:*'),
    state TEXT NOT NULL CHECK(state IN ('planned', 'applied', 'rejected')),
    created_at TEXT NOT NULL,
    applied_at TEXT
);
