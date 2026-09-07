-- 本机问答历史与安全事件共用主库；正文使用既有 AES-GCM 包装器。
CREATE TABLE query_history (
    trace_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    process_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    question_sha256 TEXT NOT NULL,
    body_saved INTEGER NOT NULL,
    ciphertext TEXT,
    nonce TEXT,
    duration_ms INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX query_history_scope_time ON query_history(
    project_id, knowledge_base_id, created_at DESC, trace_id DESC
);
CREATE INDEX query_history_expiry ON query_history(expires_at);
CREATE TABLE query_trace_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_name TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX query_trace_events_trace ON query_trace_events(trace_id, sequence);
