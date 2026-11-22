-- Semantic layer: trail descriptions and hiker tips as embeddings (pgvector) + full-text (tsvector).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id        TEXT PRIMARY KEY,
    trail_id        TEXT REFERENCES trails (trail_id) ON DELETE SET NULL,
    trail_name_hint TEXT,                              -- used to link a tip to a trail after ingestion
    region          TEXT,
    lang            TEXT NOT NULL CHECK (lang IN ('he', 'en')),
    source          TEXT NOT NULL,
    text            TEXT NOT NULL,
    embedding       vector(1024) NOT NULL,
    -- 'simple' config: no Hebrew stemmer exists in Postgres, so we match normalized tokens.
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_embedding_hnsw
    ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS knowledge_tsv_gin ON knowledge_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS knowledge_trail_idx ON knowledge_chunks (trail_id);
