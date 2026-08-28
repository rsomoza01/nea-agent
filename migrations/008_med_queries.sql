-- 008_med_queries.sql — Analytics: registro de consultas de medicamentos.
-- Cada búsqueda de medicamento en el catálogo inserta una fila (Fase 1:
-- dashboard de medicamentos más buscados por periodo).

CREATE TABLE IF NOT EXISTS med_queries (
    id            BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
    provider_id   TEXT NOT NULL DEFAULT '',
    term          TEXT NOT NULL,
    product_id    TEXT,
    product_name  TEXT,
    result_count  INTEGER NOT NULL DEFAULT 0,
    added_to_cart BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_med_queries_created ON med_queries (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_med_queries_provider ON med_queries (provider_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_med_queries_term ON med_queries (term);
