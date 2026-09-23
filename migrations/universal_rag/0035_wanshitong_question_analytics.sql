-- F05 只保存有界聚合与 Trace 样本引用；正文仍由 canonical History 管理。
CREATE TABLE question_stats_runs (
    run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    knowledge_base_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    business_timezone TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    normalizer_revision TEXT NOT NULL,
    alias_revision TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'BUILDING', 'COMPLETE', 'FAILED', 'LIMITED'
    )),
    source_total INTEGER NOT NULL DEFAULT 0,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    unknown_identity_count INTEGER NOT NULL DEFAULT 0,
    body_missing_count INTEGER NOT NULL DEFAULT 0,
    unreadable_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    coverage_start TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    expires_at TEXT NOT NULL,
    failure_code TEXT
);

CREATE UNIQUE INDEX question_stats_one_building
ON question_stats_runs(deployment_id, project_id, knowledge_base_id)
WHERE state='BUILDING';

CREATE INDEX question_stats_runs_latest
ON question_stats_runs(
    deployment_id, project_id, knowledge_base_id, state, created_at DESC
);

CREATE TABLE question_stats_items (
    run_id TEXT NOT NULL REFERENCES question_stats_runs(run_id)
        ON DELETE CASCADE,
    group_key TEXT NOT NULL,
    group_kind TEXT NOT NULL CHECK(group_kind IN (
        'EXACT', 'APPROVED_ALIAS', 'CONTEXT'
    )),
    request_count INTEGER NOT NULL,
    distinct_users INTEGER NOT NULL,
    user_day_heat INTEGER NOT NULL,
    manual_request_count INTEGER NOT NULL,
    manual_distinct_users INTEGER NOT NULL,
    manual_user_day_heat INTEGER NOT NULL,
    suggestion_count INTEGER NOT NULL,
    popular_count INTEGER NOT NULL,
    retry_count INTEGER NOT NULL,
    unknown_entry_count INTEGER NOT NULL,
    answered_count INTEGER NOT NULL,
    refused_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    feedback_count INTEGER NOT NULL,
    helpful_count INTEGER NOT NULL,
    negative_feedback_count INTEGER NOT NULL,
    false_refusal_count INTEGER NOT NULL,
    confirmed_open_issue_count INTEGER NOT NULL,
    last_seen_at TEXT NOT NULL,
    sample_trace_ids_json TEXT NOT NULL,
    PRIMARY KEY(run_id, group_key)
);

CREATE INDEX question_stats_items_frequent
ON question_stats_items(
    run_id, distinct_users DESC, user_day_heat DESC,
    request_count DESC, last_seen_at DESC, group_key
);

CREATE INDEX question_stats_items_unresolved
ON question_stats_items(
    run_id, confirmed_open_issue_count DESC, distinct_users DESC,
    request_count DESC, last_seen_at DESC, group_key
);
