-- 问答与技术 Trace 支持包的持久导出 journal/lease。
CREATE TABLE history_export_journal (
    export_id TEXT PRIMARY KEY,
    requested_sha256 TEXT NOT NULL CHECK(length(requested_sha256) = 64),
    state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'COMPLETED', 'FAILED')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    finished_at TEXT,
    failure_code TEXT
);

CREATE INDEX history_export_journal_expiry
ON history_export_journal(expires_at);

CREATE TABLE history_export_leases (
    export_id TEXT NOT NULL REFERENCES history_export_journal(export_id)
        ON DELETE CASCADE,
    trace_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(export_id, trace_id)
);

CREATE INDEX history_export_leases_trace
ON history_export_leases(trace_id, expires_at);
