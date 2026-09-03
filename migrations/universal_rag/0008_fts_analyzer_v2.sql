CREATE VIRTUAL TABLE chunks_fts_v2 USING fts5(
    chunk_id UNINDEXED,
    revision_id UNINDEXED,
    knowledge_base_id UNINDEXED,
    document_id UNINDEXED,
    analyzer_id UNINDEXED,
    analyzed_title,
    analyzed_heading,
    analyzed_identifiers,
    analyzed_text,
    tokenize = 'unicode61 remove_diacritics 2'
);
