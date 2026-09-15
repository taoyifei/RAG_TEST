-- 湾事通产品壳保存浏览器提供的相对路径；Universal Document 保持不变。
CREATE TABLE wanshitong_document_metadata (
    document_id TEXT PRIMARY KEY REFERENCES documents(document_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    relative_path TEXT NOT NULL,
    department TEXT,
    category_path_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX wanshitong_document_metadata_scope
ON wanshitong_document_metadata(
    project_id,
    knowledge_base_id,
    relative_path
);
