CREATE TABLE gc_plan_items (
    plan_id TEXT NOT NULL REFERENCES gc_plans(plan_id),
    item_type TEXT NOT NULL CHECK(item_type IN ('revision', 'blob')),
    item_id TEXT NOT NULL,
    expected_snapshot_hash TEXT NOT NULL CHECK(
        expected_snapshot_hash GLOB 'sha256:*'
    ),
    state TEXT NOT NULL CHECK(state IN (
        'planned', 'claimed', 'vector_deleted', 'sqlite_deleted',
        'blob_reconciled', 'completed', 'failed_retryable'
    )),
    attempt INTEGER NOT NULL CHECK(attempt >= 0),
    safe_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, item_type, item_id)
);

CREATE INDEX gc_plan_items_state
ON gc_plan_items(plan_id, state, item_type, item_id);

CREATE TABLE blob_reconciliation (
    artifact_id TEXT PRIMARY KEY CHECK(artifact_id GLOB 'sha256:*'),
    observed_state TEXT NOT NULL CHECK(
        observed_state IN ('physical_only', 'catalog_only', 'consistent')
    ),
    content_sha256 TEXT CHECK(
        content_sha256 IS NULL OR length(content_sha256) = 64
    ),
    action_state TEXT NOT NULL CHECK(
        action_state IN ('quarantined', 'corrupt', 'verified')
    ),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    safe_error TEXT
);
