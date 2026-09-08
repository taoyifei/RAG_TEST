ALTER TABLE ocr_enrichment_cache RENAME TO ocr_enrichment_cache_legacy;

CREATE TABLE ocr_enrichment_cache (
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    media_sha256 TEXT NOT NULL CHECK(length(media_sha256) = 64),
    adapter TEXT NOT NULL,
    provider TEXT NOT NULL,
    ocr_revision TEXT NOT NULL,
    model TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    recognition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        knowledge_base_id,
        media_sha256,
        adapter,
        provider,
        ocr_revision,
        model,
        policy_version
    )
);

INSERT INTO ocr_enrichment_cache(
    knowledge_base_id,
    media_sha256,
    adapter,
    provider,
    ocr_revision,
    model,
    policy_version,
    recognition_json,
    created_at
)
SELECT
    knowledge_base_id,
    media_sha256,
    'aliyun-multimodal-ocr',
    'legacy-unscoped',
    'legacy-unversioned',
    model,
    policy_version,
    recognition_json,
    created_at
FROM ocr_enrichment_cache_legacy;

DROP TABLE ocr_enrichment_cache_legacy;
