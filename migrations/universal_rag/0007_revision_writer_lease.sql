CREATE TABLE revision_build_leases (
    revision_id TEXT PRIMARY KEY,
    owner_job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id),
    fencing_token INTEGER NOT NULL CHECK(fencing_token > 0),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active', 'released', 'expired'))
);

CREATE INDEX revision_build_leases_owner
ON revision_build_leases(owner_job_id, state, expires_at);

CREATE TRIGGER revision_build_lease_owner_insert
BEFORE INSERT ON revision_build_leases
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM ingestion_jobs j
        WHERE j.job_id = NEW.owner_job_id
          AND j.revision_id = NEW.revision_id
    ) THEN RAISE(ABORT, 'lease_owner_revision_mismatch') END;
END;

CREATE TRIGGER revision_build_lease_owner_update
BEFORE UPDATE OF revision_id, owner_job_id ON revision_build_leases
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM ingestion_jobs j
        WHERE j.job_id = NEW.owner_job_id
          AND j.revision_id = NEW.revision_id
    ) THEN RAISE(ABORT, 'lease_owner_revision_mismatch') END;
END;
