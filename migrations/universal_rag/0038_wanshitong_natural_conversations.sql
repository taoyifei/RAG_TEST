-- 自然回答与旧逐 Claim 轮次分别保存，复用同一会话 scope、TTL 和 AES-GCM 密文。
CREATE TABLE product_natural_turns (
    turn_id TEXT PRIMARY KEY CHECK(turn_id GLOB 'trace_*'),
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    active_revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    terminal_status TEXT NOT NULL CHECK(terminal_status IN ('ANSWERED', 'NO_MATERIAL')),
    question_sha256 TEXT NOT NULL CHECK(length(question_sha256) = 64),
    ciphertext TEXT NOT NULL,
    nonce TEXT NOT NULL,
    content_chars INTEGER NOT NULL CHECK(content_chars >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(owner_id, project_id, knowledge_base_id, conversation_id, ordinal),
    FOREIGN KEY(owner_id, project_id, knowledge_base_id, conversation_id)
        REFERENCES product_conversations(
            owner_id, project_id, knowledge_base_id, conversation_id
        ) ON DELETE CASCADE
);

CREATE INDEX product_natural_turn_scope
ON product_natural_turns(
    owner_id, project_id, knowledge_base_id, conversation_id, ordinal
);
