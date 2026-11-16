-- 014_med_queries_intent.sql — auditoría de la CAPA DE COMPRENSIÓN (F15).
-- Por qué: medir la capa y poder culpar a quien corresponda en un caso real
-- (¿entendió mal el acto de conversación, o falló el catálogo?). Sin esto, un
-- «No tengo SER INDU» en producción no deja rastro de qué decidió la capa.
--
-- Aditivo y nullable: las filas viejas quedan con NULL y el dashboard de
-- Analítica existente (008) no cambia.

ALTER TABLE med_queries ADD COLUMN IF NOT EXISTS intent_fuente TEXT;
ALTER TABLE med_queries ADD COLUMN IF NOT EXISTS intent_acto TEXT;
ALTER TABLE med_queries ADD COLUMN IF NOT EXISTS intent_motivo TEXT;
ALTER TABLE med_queries ADD COLUMN IF NOT EXISTS intent_confianza DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_med_queries_intent_fuente
    ON med_queries (intent_fuente, created_at DESC);
