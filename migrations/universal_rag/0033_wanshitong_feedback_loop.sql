-- 湾事通反馈详情只扩展 canonical product_feedback，不重复保存 owner/scope。
CREATE TABLE wanshitong_feedback_details (
    trace_id TEXT PRIMARY KEY REFERENCES product_feedback(trace_id)
        ON DELETE CASCADE,
    reason_detail TEXT CHECK(reason_detail IS NULL OR reason_detail IN (
        'INCORRECT', 'INCOMPLETE', 'WRONG_SOURCE', 'FALSE_REFUSAL',
        'UNSAFE_ANSWER', 'TOO_SLOW', 'OTHER'
    )),
    comment_ciphertext TEXT,
    comment_nonce TEXT,
    encryption_key_id TEXT,
    feedback_revision INTEGER NOT NULL DEFAULT 1
        CHECK(feedback_revision >= 1),
    updated_at TEXT NOT NULL,
    CHECK(
        (comment_ciphertext IS NULL AND comment_nonce IS NULL
            AND encryption_key_id IS NULL)
        OR
        (comment_ciphertext IS NOT NULL AND comment_nonce IS NOT NULL
            AND encryption_key_id IS NOT NULL)
    )
);

CREATE INDEX wanshitong_feedback_details_reason
ON wanshitong_feedback_details(reason_detail, updated_at DESC);

-- 管理员复核与用户反馈独立版本化，用户更新后保留旧复核结论。
CREATE TABLE wanshitong_feedback_reviews (
    trace_id TEXT PRIMARY KEY REFERENCES product_feedback(trace_id)
        ON DELETE CASCADE,
    review_status TEXT NOT NULL CHECK(review_status IN (
        'NEW', 'REVIEWED', 'FIX_PLANNED', 'RESOLVED',
        'EXPECTED_BEHAVIOR'
    )),
    root_cause TEXT CHECK(root_cause IS NULL OR root_cause IN (
        'CONTEXT_RESOLUTION_WRONG', 'RETRIEVAL_NO_CANDIDATE',
        'CORRECT_SOURCE_NOT_IN_EVIDENCE_PACK',
        'EVIDENCE_HARD_REJECTED_WRONGLY', 'WRONG_EVIDENCE_SELECTED',
        'GENERATION_EMPTY', 'GENERATION_INCOMPLETE', 'CLAIM_ALL_REJECTED',
        'CLAIM_UNSUPPORTED_ACCEPTED', 'CLAIM_NUMBER_OR_NEGATION_ERROR',
        'STRUCTURAL_SIBLING_CONTAMINATION', 'TEMPLATE_BOUNDARY_ERROR',
        'FALSE_REFUSAL', 'WRONG_SOURCE', 'DEPARTMENT_ROUTE_WRONG',
        'TOO_SLOW', 'EXPECTED_REFUSAL', 'OTHER'
    )),
    note_ciphertext TEXT,
    note_nonce TEXT,
    encryption_key_id TEXT,
    selected_source_document_id TEXT,
    selected_source_version_id TEXT,
    reviewed_feedback_revision INTEGER NOT NULL
        CHECK(reviewed_feedback_revision >= 1),
    evaluation_candidate INTEGER NOT NULL DEFAULT 0
        CHECK(evaluation_candidate IN (0, 1)),
    fix_reference TEXT,
    verification_reference_json TEXT NOT NULL DEFAULT '[]',
    review_version INTEGER NOT NULL DEFAULT 1 CHECK(review_version >= 1),
    reviewed_by_admin TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (note_ciphertext IS NULL AND note_nonce IS NULL
            AND encryption_key_id IS NULL)
        OR
        (note_ciphertext IS NOT NULL AND note_nonce IS NOT NULL
            AND encryption_key_id IS NOT NULL)
    ),
    CHECK(
        (selected_source_document_id IS NULL
            AND selected_source_version_id IS NULL)
        OR
        (selected_source_document_id GLOB 'doc_*'
            AND selected_source_version_id GLOB 'dver_*')
    )
);

CREATE INDEX wanshitong_feedback_reviews_status
ON wanshitong_feedback_reviews(review_status, updated_at DESC);

CREATE INDEX wanshitong_feedback_reviews_root_cause
ON wanshitong_feedback_reviews(root_cause, updated_at DESC);
