ALTER TABLE blob_references RENAME TO blob_references_before_derived_document;

CREATE TABLE blob_references (
    reference_id TEXT PRIMARY KEY CHECK(reference_id GLOB 'bref_*'),
    artifact_id TEXT NOT NULL REFERENCES blob_objects(artifact_id),
    owner_type TEXT NOT NULL CHECK(
        owner_type IN ('document_version', 'parsed_media', 'other')
    ),
    owner_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(
        role IN (
            'source_document',
            'derived_document',
            'embedded_media',
            'other'
        )
    ),
    revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, owner_type, owner_id, role, revision_id)
);

INSERT INTO blob_references(
    reference_id,
    artifact_id,
    owner_type,
    owner_id,
    role,
    revision_id,
    created_at
)
SELECT
    reference_id,
    artifact_id,
    owner_type,
    owner_id,
    role,
    revision_id,
    created_at
FROM blob_references_before_derived_document;

DROP TABLE blob_references_before_derived_document;

CREATE INDEX blob_references_artifact
ON blob_references(artifact_id);
