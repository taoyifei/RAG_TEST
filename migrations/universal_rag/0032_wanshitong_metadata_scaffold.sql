-- 通用 Document 只保存默认空的外部 metadata；湾事通补齐类型化登记和版本快照。
ALTER TABLE documents ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'
CHECK(json_valid(metadata_json) AND json_type(metadata_json) = 'object');

ALTER TABLE wanshitong_document_metadata ADD COLUMN department_key TEXT;
ALTER TABLE wanshitong_document_metadata ADD COLUMN department_name TEXT;
ALTER TABLE wanshitong_document_metadata ADD COLUMN document_title TEXT;
ALTER TABLE wanshitong_document_metadata ADD COLUMN source_relative_path TEXT;
ALTER TABLE wanshitong_document_metadata ADD COLUMN topic_keys_json TEXT NOT NULL
DEFAULT '[]' CHECK(
    json_valid(topic_keys_json) AND json_type(topic_keys_json) = 'array'
);
ALTER TABLE wanshitong_document_metadata ADD COLUMN visibility_scope TEXT NOT NULL
DEFAULT 'all_internal' CHECK(visibility_scope = 'all_internal');
ALTER TABLE wanshitong_document_metadata ADD COLUMN allowed_roles_json TEXT NOT NULL
DEFAULT '[]' CHECK(
    json_valid(allowed_roles_json) AND json_type(allowed_roles_json) = 'array'
);
ALTER TABLE wanshitong_document_metadata ADD COLUMN allowed_groups_json TEXT NOT NULL
DEFAULT '[]' CHECK(
    json_valid(allowed_groups_json) AND json_type(allowed_groups_json) = 'array'
);
ALTER TABLE wanshitong_document_metadata ADD COLUMN metadata_revision TEXT NOT NULL
DEFAULT 'wanshitong-document-metadata-v1';

UPDATE wanshitong_document_metadata
SET department_name = department,
    source_relative_path = relative_path
WHERE department_name IS NULL OR source_relative_path IS NULL;

CREATE TABLE wanshitong_document_version_metadata (
    document_version_id TEXT PRIMARY KEY
        REFERENCES document_versions(document_version_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    job_id TEXT NOT NULL REFERENCES ingestion_jobs(job_id),
    metadata_json TEXT NOT NULL
        CHECK(json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    metadata_revision TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX wanshitong_document_version_metadata_document
ON wanshitong_document_version_metadata(document_id, created_at);
