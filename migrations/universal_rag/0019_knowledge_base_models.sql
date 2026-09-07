CREATE TABLE knowledge_base_model_settings (
    knowledge_base_id TEXT PRIMARY KEY REFERENCES knowledge_bases(knowledge_base_id),
    configuration TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
