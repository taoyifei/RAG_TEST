ALTER TABLE retrieval_profile_revisions ADD COLUMN primary_resolved_json TEXT;
ALTER TABLE retrieval_profile_revisions ADD COLUMN standby_resolved_json TEXT;
ALTER TABLE retrieval_profile_revisions ADD COLUMN activation_job_id TEXT;

CREATE TABLE profile_publications (
    profile_revision_id TEXT PRIMARY KEY REFERENCES retrieval_profile_revisions(profile_revision_id),
    job_id TEXT NOT NULL UNIQUE REFERENCES ingestion_jobs(job_id),
    revision_id TEXT NOT NULL,
    expected_profile_revision_id TEXT,
    expected_index_revision_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE quality_validation_records (
    record_id TEXT PRIMARY KEY,
    profile_revision_id TEXT NOT NULL REFERENCES retrieval_profile_revisions(profile_revision_id),
    kind TEXT NOT NULL,
    validation_mode TEXT NOT NULL CHECK(validation_mode IN ('offline', 'mock', 'live')),
    accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
    binding_identity TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

ALTER TABLE provider_validation_runs ADD COLUMN endpoint_identity TEXT;
ALTER TABLE provider_validation_runs ADD COLUMN validation_mode TEXT NOT NULL DEFAULT 'unknown';
