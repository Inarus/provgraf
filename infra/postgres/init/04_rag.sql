-- 04_rag.sql — RAG layer: a gloss column to embed (contextual retrieval).
-- The `embedding vector(1024)` column already exists (01_schema.sql); here we only add `gloss`.
-- Idempotent + immutable-after-deploy (do not edit — put any changes in 05_*.sql).
ALTER TABLE entity ADD COLUMN IF NOT EXISTS gloss TEXT;

-- An HNSW index on embedding only once there are >~1000 vectors (a separate 05_ migration);
-- with the current corpus a brute-force `embedding <=> $1` (cosine) is instant.
