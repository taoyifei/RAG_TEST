CREATE TABLE provider_operation_events (
    event_id TEXT PRIMARY KEY CHECK(event_id GLOB 'opevt_*'),
    connection_id TEXT NOT NULL REFERENCES provider_connections(connection_id),
    occurred_at TEXT NOT NULL,
    operation TEXT NOT NULL,
    status_category TEXT NOT NULL,
    latency_ms INTEGER NOT NULL CHECK(latency_ms >= 0),
    estimated_tokens INTEGER NOT NULL CHECK(estimated_tokens >= 0),
    observed_tokens INTEGER CHECK(observed_tokens >= 0),
    retry_count INTEGER NOT NULL CHECK(retry_count >= 0),
    rate_limited INTEGER NOT NULL CHECK(rate_limited IN (0, 1)),
    selected_slot TEXT,
    failover INTEGER NOT NULL CHECK(failover IN (0, 1)),
    reranker_mode TEXT,
    cache_hit INTEGER NOT NULL CHECK(cache_hit IN (0, 1)),
    safe_error_code TEXT
);

CREATE INDEX provider_operation_events_daily
ON provider_operation_events(occurred_at, connection_id, operation);

CREATE TABLE provider_daily_budgets (
    usage_date TEXT NOT NULL,
    connection_id TEXT NOT NULL REFERENCES provider_connections(connection_id),
    operation TEXT NOT NULL,
    requests INTEGER NOT NULL CHECK(requests >= 0),
    estimated_tokens INTEGER NOT NULL CHECK(estimated_tokens >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(usage_date, connection_id, operation)
);
