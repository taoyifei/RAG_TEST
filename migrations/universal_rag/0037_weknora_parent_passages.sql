-- 只在候选 Revision 中保存父级阅读材料，子块仍单独进入 FTS/向量。
CREATE TABLE parent_passages (
    revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    parent_passage_id TEXT NOT NULL CHECK(parent_passage_id GLOB 'ppsg_*'),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    passage_json TEXT NOT NULL,
    PRIMARY KEY(revision_id, parent_passage_id)
);

CREATE INDEX parent_passages_scope
ON parent_passages(revision_id, document_id, document_version_id);
