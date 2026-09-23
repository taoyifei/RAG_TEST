-- 管理员按固定范围和真实 History owner 精确筛选，先计数再分页。
CREATE INDEX IF NOT EXISTS query_history_scope_owner_time
ON query_history(
    project_id, knowledge_base_id, owner_id, created_at DESC, trace_id DESC
);
