-- Product 多轮会话正文继续使用应用层 AES-GCM；表中只保留范围与来源身份。
CREATE TABLE product_conversations (
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    conversation_id TEXT NOT NULL,
    next_ordinal INTEGER NOT NULL DEFAULT 0 CHECK(next_ordinal >= 0),
    content_chars INTEGER NOT NULL DEFAULT 0 CHECK(content_chars >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(owner_id, project_id, knowledge_base_id, conversation_id)
);

CREATE INDEX product_conversations_expiry
ON product_conversations(expires_at);

CREATE TABLE product_conversation_turns (
    turn_id TEXT PRIMARY KEY CHECK(turn_id GLOB 'trace_*'),
    owner_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    active_revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    terminal_status TEXT NOT NULL CHECK(terminal_status IN ('ANSWERED', 'REFUSED')),
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

CREATE INDEX product_conversation_turn_scope
ON product_conversation_turns(
    owner_id, project_id, knowledge_base_id, conversation_id, ordinal
);

CREATE TABLE product_conversation_supports (
    turn_id TEXT NOT NULL REFERENCES product_conversation_turns(turn_id)
        ON DELETE CASCADE,
    claim_ordinal INTEGER NOT NULL CHECK(claim_ordinal >= 0),
    support_ordinal INTEGER NOT NULL CHECK(support_ordinal >= 0),
    revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
    chunk_id TEXT NOT NULL,
    PRIMARY KEY(turn_id, claim_ordinal, support_ordinal)
);

CREATE INDEX product_conversation_support_source
ON product_conversation_supports(
    revision_id, document_id, document_version_id, chunk_id
);

-- canonical feedback 与 Operational Trace 投影通过可恢复状态显式对账。
CREATE TABLE product_feedback (
    trace_id TEXT PRIMARY KEY CHECK(trace_id GLOB 'trace_*'),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    owner_id TEXT NOT NULL,
    useful INTEGER NOT NULL CHECK(useful IN (0, 1)),
    reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN (
        'INCORRECT', 'INCOMPLETE', 'WRONG_SOURCE', 'OUTDATED',
        'TOO_SLOW', 'OTHER'
    )),
    projection_state TEXT NOT NULL CHECK(projection_state IN (
        'PENDING', 'APPLIED', 'NOT_APPLICABLE'
    )),
    projection_version INTEGER NOT NULL DEFAULT 1
        CHECK(projection_version >= 1),
    projection_attempts INTEGER NOT NULL DEFAULT 0 CHECK(projection_attempts >= 0),
    projection_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX product_feedback_scope
ON product_feedback(project_id, knowledge_base_id, owner_id, updated_at DESC);

CREATE INDEX product_feedback_projection
ON product_feedback(projection_state, updated_at);
