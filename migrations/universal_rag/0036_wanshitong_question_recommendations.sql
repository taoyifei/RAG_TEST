-- F06 只存管理员审核后的公开题面和映射；不复制 History 答案或身份。
CREATE TABLE question_recommendation_catalog (
    recommendation_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    question_text TEXT NOT NULL,
    question_style TEXT NOT NULL CHECK(question_style IN (
        'SHORT', 'STANDARD', 'COMPOUND'
    )),
    topic_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'DRAFT', 'APPROVED', 'DISABLED', 'NEEDS_REVIEW'
    )),
    version INTEGER NOT NULL CHECK(version >= 1),
    alias_keys_json TEXT NOT NULL,
    validated_source_versions_json TEXT NOT NULL,
    checked_at TEXT,
    approved_at TEXT,
    approved_by TEXT,
    disabled_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX question_recommendation_scope
ON question_recommendation_catalog(
    deployment_id, project_id, knowledge_base_id, state, updated_at DESC
);
