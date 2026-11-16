-- 015_intent_decisions.sql — decisiones de la CAPA DE COMPRENSIÓN (F15).
--
-- Por qué existe (revisión externa del cambio, 2026-11): la migración 014 solo
-- añadía columnas a `med_queries`, y esa tabla SOLO tiene filas cuando hubo
-- búsqueda. Es decir: las decisiones que evitan una búsqueda —justamente el
-- objetivo de F15— no dejaban rastro. Sin registrar las dos caras no se puede
-- medir cuánta basura se evitó, ni atribuir un fallo a la comprensión o al catálogo.
--
-- Una fila por DECISIÓN (haya búsqueda o no), con el término candidato que veía la
-- capa, su veredicto y el porqué. Aditivo: nada de lo existente cambia.

CREATE TABLE IF NOT EXISTS intent_decisions (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
    provider_id     TEXT NOT NULL DEFAULT '',
    -- nombre | capa | fail-safe | desactivada | desvio  (y 'llm' si el modelo buscó solo)
    fuente          TEXT NOT NULL,
    acto            TEXT,
    hay_consulta    BOOLEAN,
    termino         TEXT,
    candidato       TEXT,
    motivo          TEXT,
    confianza       DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_intent_decisions_created
    ON intent_decisions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intent_decisions_fuente
    ON intent_decisions (fuente, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intent_decisions_acto
    ON intent_decisions (acto, created_at DESC);
