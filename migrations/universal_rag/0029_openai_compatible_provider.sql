-- migration: foreign_keys=off

CREATE TABLE provider_credentials_next (
    credential_id TEXT PRIMARY KEY CHECK(credential_id GLOB 'cred_*'),
    provider_type TEXT NOT NULL CHECK(provider_type IN (
        'jina', 'aliyun-model-studio', 'openai-compatible'
    )),
    encrypted_payload TEXT NOT NULL,
    nonce TEXT,
    aad_version TEXT NOT NULL,
    key_id TEXT,
    key_version INTEGER NOT NULL CHECK(key_version > 0),
    masked_hint TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN (
        'environment_managed', 'database_encrypted'
    )),
    status TEXT NOT NULL CHECK(status IN ('configured', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    rotated_at TEXT,
    disabled_at TEXT
);

INSERT INTO provider_credentials_next SELECT * FROM provider_credentials;

CREATE TABLE provider_connections_next (
    connection_id TEXT PRIMARY KEY CHECK(connection_id GLOB 'conn_*'),
    display_name TEXT NOT NULL,
    provider_type TEXT NOT NULL CHECK(provider_type IN (
        'jina', 'aliyun-model-studio', 'openai-compatible'
    )),
    credential_id TEXT NOT NULL
        REFERENCES provider_credentials_next(credential_id),
    endpoint_profile TEXT NOT NULL DEFAULT 'default'
        CHECK(endpoint_profile = 'default'),
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN (
        'configured', 'validated', 'degraded', 'disabled'
    )),
    last_validation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    configuration_version INTEGER NOT NULL DEFAULT 1
        CHECK(configuration_version > 0)
);

INSERT INTO provider_connections_next SELECT * FROM provider_connections;

DROP TABLE provider_connections;
DROP TABLE provider_credentials;
ALTER TABLE provider_credentials_next RENAME TO provider_credentials;
ALTER TABLE provider_connections_next RENAME TO provider_connections;

CREATE INDEX provider_credentials_provider
ON provider_credentials(provider_type, status);

CREATE INDEX provider_connections_provider
ON provider_connections(provider_type, enabled);
