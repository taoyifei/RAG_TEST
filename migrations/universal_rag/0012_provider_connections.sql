CREATE TABLE provider_connections (
    connection_id TEXT PRIMARY KEY CHECK(connection_id GLOB 'conn_*'),
    display_name TEXT NOT NULL,
    provider_type TEXT NOT NULL CHECK(provider_type IN (
        'jina', 'aliyun-model-studio'
    )),
    credential_id TEXT NOT NULL REFERENCES provider_credentials(credential_id),
    endpoint_profile TEXT NOT NULL DEFAULT 'default'
        CHECK(endpoint_profile = 'default'),
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN (
        'configured', 'validated', 'degraded', 'disabled'
    )),
    last_validation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX provider_connections_provider
ON provider_connections(provider_type, enabled);
