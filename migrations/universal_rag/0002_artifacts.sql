CREATE TABLE blob_objects (
    artifact_id TEXT PRIMARY KEY CHECK(artifact_id GLOB 'sha256:*'),
    content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    media_type TEXT NOT NULL,
    physical_state TEXT NOT NULL CHECK(
        physical_state IN ('staged', 'available', 'quarantine')
    ),
    physical_locator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    verified_at TEXT,
    created_by_job_id TEXT REFERENCES ingestion_jobs(job_id)
);

CREATE TABLE blob_references (
    reference_id TEXT PRIMARY KEY CHECK(reference_id GLOB 'bref_*'),
    artifact_id TEXT NOT NULL REFERENCES blob_objects(artifact_id),
    owner_type TEXT NOT NULL CHECK(
        owner_type IN ('document_version', 'parsed_media', 'other')
    ),
    owner_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(
        role IN ('source_document', 'embedded_media', 'other')
    ),
    revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, owner_type, owner_id, role, revision_id)
);

CREATE INDEX blob_references_artifact
ON blob_references(artifact_id);
