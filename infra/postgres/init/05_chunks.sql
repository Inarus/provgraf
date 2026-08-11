-- 05_chunks.sql — raw document chunks (fallback retrieval, a "searchable scratch space").
-- Kept apart from atomic facts (entity): this holds the split-up source text for semantic search
-- when no curated fact exists yet. Idempotent, immutable-after-deploy.
CREATE TABLE IF NOT EXISTS doc_chunk (
  id         BIGSERIAL PRIMARY KEY,
  doc_qname  TEXT NOT NULL,                 -- qname of the source document (entity kind='document')
  owner      TEXT,
  scope      prov_scope NOT NULL DEFAULT 'client',
  ord        INT  NOT NULL,                 -- position of the chunk within the document
  text       TEXT NOT NULL,
  embedding  vector(1024),                  -- mmlw (the same model as for facts)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (doc_qname, ord)
);
CREATE INDEX IF NOT EXISTS idx_doc_chunk_doc ON doc_chunk(doc_qname);
-- An HNSW index on embedding only once there are many chunks (a separate migration).
