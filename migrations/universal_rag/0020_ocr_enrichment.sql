CREATE TABLE ocr_enrichment_cache (
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    media_sha256 TEXT NOT NULL CHECK(length(media_sha256) = 64),
    model TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    recognition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (knowledge_base_id, media_sha256, model, policy_version)
);
