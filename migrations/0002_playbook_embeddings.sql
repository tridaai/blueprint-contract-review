-- 0002: playbook embeddings for semantic / hybrid retrieval (pgvector).
-- The vector column is deliberately un-dimensioned: embedding models
-- differ (OpenAI text-embedding-3-small is 1536, Ollama nomic-embed-text
-- is 768), and one corpus table must serve whichever provider the
-- deployment configures. The `model` column records which model wrote
-- each row so a model switch re-embeds instead of mixing spaces.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS playbook_embeddings (
    policy_id TEXT PRIMARY KEY,
    model     TEXT NOT NULL,
    embedding vector NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
