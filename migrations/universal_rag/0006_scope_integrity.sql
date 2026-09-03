CREATE UNIQUE INDEX knowledge_bases_scope_identity
ON knowledge_bases(knowledge_base_id, project_id);

CREATE UNIQUE INDEX documents_scope_identity
ON documents(document_id, project_id, knowledge_base_id);

CREATE UNIQUE INDEX document_versions_document_identity
ON document_versions(document_version_id, document_id);

CREATE UNIQUE INDEX index_revisions_scope_identity
ON index_revisions(index_revision_id, project_id, knowledge_base_id);

CREATE TRIGGER documents_scope_insert
BEFORE INSERT ON documents
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_bases kb
        WHERE kb.knowledge_base_id = NEW.knowledge_base_id
          AND kb.project_id = NEW.project_id
    ) THEN RAISE(ABORT, 'document_scope_mismatch') END;
END;

CREATE TRIGGER documents_scope_update
BEFORE UPDATE OF project_id, knowledge_base_id, current_version_id ON documents
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_bases kb
        WHERE kb.knowledge_base_id = NEW.knowledge_base_id
          AND kb.project_id = NEW.project_id
    ) THEN RAISE(ABORT, 'document_scope_mismatch') END;
    SELECT CASE WHEN NEW.current_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM document_versions dv
        WHERE dv.document_version_id = NEW.current_version_id
          AND dv.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'document_current_version_mismatch') END;
END;

CREATE TRIGGER index_revisions_scope_insert
BEFORE INSERT ON index_revisions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_bases kb
        WHERE kb.knowledge_base_id = NEW.knowledge_base_id
          AND kb.project_id = NEW.project_id
    ) THEN RAISE(ABORT, 'revision_scope_mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM ingestion_jobs j
        WHERE j.revision_id = NEW.index_revision_id
          AND (j.project_id <> NEW.project_id
               OR j.knowledge_base_id <> NEW.knowledge_base_id)
    ) THEN RAISE(ABORT, 'revision_job_scope_mismatch') END;
END;

CREATE TRIGGER index_revisions_scope_update
BEFORE UPDATE OF project_id, knowledge_base_id, state ON index_revisions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_bases kb
        WHERE kb.knowledge_base_id = NEW.knowledge_base_id
          AND kb.project_id = NEW.project_id
    ) THEN RAISE(ABORT, 'revision_scope_mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM ingestion_jobs j
        WHERE j.revision_id = NEW.index_revision_id
          AND (j.project_id <> NEW.project_id
               OR j.knowledge_base_id <> NEW.knowledge_base_id)
    ) THEN RAISE(ABORT, 'revision_job_scope_mismatch') END;
    SELECT CASE WHEN NEW.state <> 'active' AND EXISTS (
        SELECT 1 FROM knowledge_bases kb
        WHERE kb.active_revision_id = NEW.index_revision_id
    ) THEN RAISE(ABORT, 'active_revision_must_remain_active') END;
END;

CREATE TRIGGER revision_documents_scope_insert
BEFORE INSERT ON revision_documents
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM document_versions dv
        WHERE dv.document_version_id = NEW.document_version_id
          AND dv.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'revision_document_version_mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        JOIN documents d ON d.document_id = NEW.document_id
        WHERE r.index_revision_id = NEW.revision_id
          AND d.project_id = r.project_id
          AND d.knowledge_base_id = r.knowledge_base_id
    ) THEN RAISE(ABORT, 'revision_document_scope_mismatch') END;
END;

CREATE TRIGGER revision_documents_scope_update
BEFORE UPDATE OF revision_id, document_id, document_version_id
ON revision_documents
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM document_versions dv
        WHERE dv.document_version_id = NEW.document_version_id
          AND dv.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'revision_document_version_mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        JOIN documents d ON d.document_id = NEW.document_id
        WHERE r.index_revision_id = NEW.revision_id
          AND d.project_id = r.project_id
          AND d.knowledge_base_id = r.knowledge_base_id
    ) THEN RAISE(ABORT, 'revision_document_scope_mismatch') END;
END;

CREATE TRIGGER chunks_scope_insert
BEFORE INSERT ON chunks
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM document_versions dv
        WHERE dv.document_version_id = NEW.document_version_id
          AND dv.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'chunk_document_version_mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        JOIN documents d ON d.document_id = NEW.document_id
        WHERE r.index_revision_id = NEW.revision_id
          AND d.project_id = r.project_id
          AND d.knowledge_base_id = r.knowledge_base_id
    ) THEN RAISE(ABORT, 'chunk_scope_mismatch') END;
END;

CREATE TRIGGER chunks_scope_update
BEFORE UPDATE OF revision_id, document_id, document_version_id ON chunks
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM document_versions dv
        WHERE dv.document_version_id = NEW.document_version_id
          AND dv.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'chunk_document_version_mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        JOIN documents d ON d.document_id = NEW.document_id
        WHERE r.index_revision_id = NEW.revision_id
          AND d.project_id = r.project_id
          AND d.knowledge_base_id = r.knowledge_base_id
    ) THEN RAISE(ABORT, 'chunk_scope_mismatch') END;
END;

CREATE TRIGGER ingestion_jobs_scope_insert
BEFORE INSERT ON ingestion_jobs
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_bases kb
        WHERE kb.knowledge_base_id = NEW.knowledge_base_id
          AND kb.project_id = NEW.project_id
    ) THEN RAISE(ABORT, 'job_scope_mismatch') END;
    SELECT CASE WHEN NEW.document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = NEW.document_id
          AND d.project_id = NEW.project_id
          AND d.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'job_document_scope_mismatch') END;
    SELECT CASE WHEN NEW.document_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM document_versions dv
        WHERE dv.document_version_id = NEW.document_version_id
          AND dv.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'job_document_version_mismatch') END;
    SELECT CASE WHEN NEW.revision_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.revision_id
    ) AND NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.revision_id
          AND r.project_id = NEW.project_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'job_revision_scope_mismatch') END;
END;

CREATE TRIGGER ingestion_jobs_scope_update
BEFORE UPDATE OF project_id, knowledge_base_id, document_id,
    document_version_id, revision_id ON ingestion_jobs
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_bases kb
        WHERE kb.knowledge_base_id = NEW.knowledge_base_id
          AND kb.project_id = NEW.project_id
    ) THEN RAISE(ABORT, 'job_scope_mismatch') END;
    SELECT CASE WHEN NEW.document_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM documents d
        WHERE d.document_id = NEW.document_id
          AND d.project_id = NEW.project_id
          AND d.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'job_document_scope_mismatch') END;
    SELECT CASE WHEN NEW.document_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM document_versions dv
        WHERE dv.document_version_id = NEW.document_version_id
          AND dv.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'job_document_version_mismatch') END;
    SELECT CASE WHEN NEW.revision_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.revision_id
    ) AND NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.revision_id
          AND r.project_id = NEW.project_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'job_revision_scope_mismatch') END;
END;

CREATE TRIGGER knowledge_bases_active_revision_insert
BEFORE INSERT ON knowledge_bases
WHEN NEW.active_revision_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.active_revision_id
          AND r.project_id = NEW.project_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
          AND r.state = 'active'
    ) THEN RAISE(ABORT, 'active_revision_scope_or_state_mismatch') END;
END;

CREATE TRIGGER knowledge_bases_active_revision_update
BEFORE UPDATE OF active_revision_id, project_id ON knowledge_bases
WHEN NEW.active_revision_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.active_revision_id
          AND r.project_id = NEW.project_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
          AND r.state = 'active'
    ) THEN RAISE(ABORT, 'active_revision_scope_or_state_mismatch') END;
END;

CREATE TRIGGER active_revision_history_scope_insert
BEFORE INSERT ON active_revision_history
BEGIN
    SELECT CASE WHEN NEW.old_revision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.old_revision_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'history_old_revision_scope_mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.new_revision_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'history_new_revision_scope_mismatch') END;
END;

CREATE TRIGGER active_revision_history_scope_update
BEFORE UPDATE OF knowledge_base_id, old_revision_id, new_revision_id
ON active_revision_history
BEGIN
    SELECT CASE WHEN NEW.old_revision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.old_revision_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'history_old_revision_scope_mismatch') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM index_revisions r
        WHERE r.index_revision_id = NEW.new_revision_id
          AND r.knowledge_base_id = NEW.knowledge_base_id
    ) THEN RAISE(ABORT, 'history_new_revision_scope_mismatch') END;
END;
