CREATE TABLE exact_identifiers (
    revision_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    identifier TEXT NOT NULL,
    normalized_identifier TEXT NOT NULL,
    PRIMARY KEY(revision_id, chunk_id, normalized_identifier),
    FOREIGN KEY(revision_id, chunk_id) REFERENCES chunks(revision_id, chunk_id)
);

CREATE INDEX exact_identifiers_lookup
ON exact_identifiers(revision_id, normalized_identifier);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED,
    revision_id UNINDEXED,
    knowledge_base_id UNINDEXED,
    document_id UNINDEXED,
    title,
    heading,
    identifiers,
    lexical_text,
    tokenize = 'unicode61 remove_diacritics 2'
);
