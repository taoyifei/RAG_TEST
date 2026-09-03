CREATE TABLE index_revisions (
    index_revision_id TEXT PRIMARY KEY CHECK(index_revision_id GLOB 'irev_*'),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    state TEXT NOT NULL CHECK(state IN (
        'created', 'building', 'parsing', 'chunking', 'embedding_primary',
        'embedding_standby', 'lexical_indexing', 'vector_indexing',
        'validating', 'ready', 'active', 'failed_retryable',
        'failed_terminal', 'retired'
    )),
    index_fingerprint TEXT NOT NULL CHECK(index_fingerprint GLOB 'sha256:*'),
    serving_compatibility_version TEXT NOT NULL,
    parser_identity_json TEXT NOT NULL,
    parsing_policy_json TEXT NOT NULL,
    chunker_identity_json TEXT NOT NULL,
    chunking_policy_json TEXT NOT NULL,
    embedding_topology_json TEXT NOT NULL,
    lexical_schema_json TEXT NOT NULL,
    vector_schema_json TEXT NOT NULL,
    chunk_payload_schema_json TEXT NOT NULL,
    physical_vector_namespace TEXT NOT NULL UNIQUE,
    expected_document_count INTEGER NOT NULL CHECK(expected_document_count >= 0),
    expected_chunk_count INTEGER NOT NULL CHECK(expected_chunk_count >= 0),
    validation_evidence_json TEXT,
    validation_evidence_hash TEXT,
    created_at TEXT NOT NULL,
    validated_at TEXT,
    activated_at TEXT,
    retired_at TEXT,
    failure_code TEXT,
    safe_message TEXT
);

CREATE TABLE revision_documents (
    revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
    parser_id TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    parsing_policy_fingerprint TEXT NOT NULL,
    ir_schema_version TEXT NOT NULL,
    document_ir_json TEXT NOT NULL,
    parse_report_json TEXT NOT NULL,
    chunking_report_json TEXT NOT NULL,
    part_catalog_identity TEXT NOT NULL,
    chunk_count INTEGER NOT NULL CHECK(chunk_count >= 0),
    PRIMARY KEY(revision_id, document_id)
);

CREATE TABLE chunks (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    chunk_id TEXT NOT NULL CHECK(chunk_id GLOB 'chunk_*'),
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    document_version_id TEXT NOT NULL REFERENCES document_versions(document_version_id),
    role TEXT NOT NULL,
    parent_node_id TEXT,
    section_id TEXT NOT NULL,
    neighbor_group_id TEXT NOT NULL,
    previous_chunk_id TEXT,
    next_chunk_id TEXT,
    citation_text TEXT NOT NULL,
    embedding_text TEXT NOT NULL,
    lexical_text TEXT NOT NULL,
    heading_path_json TEXT NOT NULL,
    source_spans_json TEXT NOT NULL,
    identifiers_json TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK(token_count > 0),
    token_count_is_estimate INTEGER NOT NULL CHECK(token_count_is_estimate IN (0, 1)),
    tokenizer_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64),
    metadata_json TEXT NOT NULL,
    chunk_json TEXT NOT NULL,
    UNIQUE(revision_id, chunk_id)
);

CREATE INDEX chunks_scope
ON chunks(revision_id, document_id, chunk_id);

CREATE TABLE embedding_slots (
    revision_id TEXT NOT NULL REFERENCES index_revisions(index_revision_id),
    slot_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('primary', 'standby')),
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    vector_name TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK(dimension > 0),
    normalization TEXT NOT NULL,
    document_request_policy_json TEXT NOT NULL,
    query_request_policy_json TEXT NOT NULL,
    adapter_revision TEXT NOT NULL,
    max_input_tokens INTEGER NOT NULL CHECK(max_input_tokens > 0),
    required_for_activation INTEGER NOT NULL CHECK(required_for_activation IN (0, 1)),
    document_fingerprint TEXT NOT NULL,
    query_fingerprint TEXT NOT NULL,
    PRIMARY KEY(revision_id, slot_id),
    UNIQUE(revision_id, vector_name)
);

CREATE TABLE revision_chunk_embeddings (
    revision_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    cache_scope TEXT NOT NULL,
    cache_key TEXT,
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'cached', 'embedded', 'vector_written', 'failed'
    )),
    attempt INTEGER NOT NULL CHECK(attempt >= 0),
    error_code TEXT,
    retryable INTEGER NOT NULL CHECK(retryable IN (0, 1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(revision_id, chunk_id, slot_id),
    FOREIGN KEY(revision_id, chunk_id) REFERENCES chunks(revision_id, chunk_id),
    FOREIGN KEY(revision_id, slot_id) REFERENCES embedding_slots(revision_id, slot_id)
);

CREATE TABLE revision_embedding_coverage (
    revision_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    expected_chunk_count INTEGER NOT NULL CHECK(expected_chunk_count >= 0),
    cached_count INTEGER NOT NULL CHECK(cached_count >= 0),
    embedded_count INTEGER NOT NULL CHECK(embedded_count >= 0),
    vector_written_count INTEGER NOT NULL CHECK(vector_written_count >= 0),
    valid_vector_count INTEGER NOT NULL CHECK(valid_vector_count >= 0),
    failed_count INTEGER NOT NULL CHECK(failed_count >= 0),
    coverage_ratio REAL NOT NULL CHECK(coverage_ratio >= 0 AND coverage_ratio <= 1),
    state TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    PRIMARY KEY(revision_id, slot_id),
    FOREIGN KEY(revision_id, slot_id) REFERENCES embedding_slots(revision_id, slot_id)
);

CREATE TABLE active_revision_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_base_id TEXT NOT NULL REFERENCES knowledge_bases(knowledge_base_id),
    old_revision_id TEXT,
    new_revision_id TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    trace_id TEXT NOT NULL
);
