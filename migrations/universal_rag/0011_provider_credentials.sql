CREATE TABLE provider_credentials (
    credential_id TEXT PRIMARY KEY CHECK(credential_id GLOB 'cred_*'),
    provider_type TEXT NOT NULL CHECK(provider_type IN (
        'jina', 'aliyun-model-studio'
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

CREATE INDEX provider_credentials_provider
ON provider_credentials(provider_type, status);
