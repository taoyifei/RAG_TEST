"""SQLite schema。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    pipeline_fingerprint TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    current_path TEXT NOT NULL UNIQUE,
    current_content_sha256 TEXT,
    active_doc_version TEXT,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_versions (
    source_id TEXT NOT NULL,
    doc_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    source_path TEXT NOT NULL,
    pipeline_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    job_id TEXT NOT NULL,
    chunk_count INTEGER,
    error_code TEXT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    PRIMARY KEY (source_id, doc_version),
    FOREIGN KEY (source_id) REFERENCES sources(source_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);

CREATE INDEX IF NOT EXISTS idx_versions_state
ON source_versions(state);

CREATE TABLE IF NOT EXISTS ocr_results (
    media_sha256 TEXT NOT NULL,
    ocr_revision TEXT NOT NULL,
    state TEXT NOT NULL,
    text TEXT,
    confidence REAL,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (media_sha256, ocr_revision)
);

CREATE TABLE IF NOT EXISTS media_references (
    source_id TEXT NOT NULL,
    doc_version TEXT NOT NULL,
    element_id TEXT NOT NULL,
    media_sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    media_name TEXT,
    locator TEXT NOT NULL,
    ocr_revision TEXT NOT NULL,
    state TEXT NOT NULL,
    error_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, doc_version, element_id, ocr_revision)
);

CREATE INDEX IF NOT EXISTS idx_media_references_ocr_state
ON media_references(ocr_revision, state);
"""
