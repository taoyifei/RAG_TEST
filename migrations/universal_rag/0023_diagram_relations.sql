CREATE TABLE diagram_relation_candidates (
    candidate_id TEXT PRIMARY KEY CHECK(candidate_id GLOB 'drel_*'),
    knowledge_base_id TEXT NOT NULL
        REFERENCES knowledge_bases(knowledge_base_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    document_version_id TEXT NOT NULL
        REFERENCES document_versions(document_version_id),
    figure_occurrence_id TEXT NOT NULL CHECK(figure_occurrence_id GLOB 'node_*'),
    media_sha256 TEXT NOT NULL CHECK(length(media_sha256) = 64),
    review_state TEXT NOT NULL CHECK(
        review_state IN ('pending', 'accepted', 'rejected', 'ambiguous')
    ),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX diagram_relation_scope
ON diagram_relation_candidates(
    knowledge_base_id,
    document_id,
    document_version_id,
    review_state
);
