-- Paso 8 — Derivación clínica persistida (UC-08, ADR-0006, PLAN §7/§8).
--
-- Registro DURABLE y auditable de cada derivación de consulta clínica a un humano.
-- Guarda el DATO CLÍNICO MÍNIMO: la CATEGORÍA (enum acotado) y la correlación
-- (clinical_handoff_id). NUNCA el texto clínico del cliente (PLAN §7 punto 5,
-- ADR-0006): no se vuelca información clínica innecesaria.
--
-- Anti-duplicado (PLAN §8 punto 4): a lo sumo UNA derivación ABIERTA por
-- conversación. El índice único PARCIAL sobre conversation_id (solo estados
-- abiertos) hace atómico el INSERT … ON CONFLICT DO NOTHING RETURNING: un segundo
-- mensaje clínico mientras se espera al humano no crea otro handoff. Mismo idioma
-- idempotente que processed_message/order_attempt.
--
-- Estados (UC-08): detectado → solicitado → asignado → atendido → retomado/cerrado.
-- NEA controla detectado/solicitado; asignado/atendido/retomado/cerrado dependen del
-- proceso humano real (PENDIENTE, PLAN §8 q6). No se añade el motivo `clinico` al
-- catálogo de handoff del CRM: la derivación se mapea al motivo seguro `cliente`
-- (decisión del propietario); la categoría vive solo aquí.
--
-- Rollback: DROP TABLE clinical_handoff;

CREATE TABLE IF NOT EXISTS clinical_handoff (
    clinical_handoff_id TEXT PRIMARY KEY,
    conversation_id     BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
    category            TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'solicitado',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_clinical_handoff_conv
    ON clinical_handoff (conversation_id);

-- Única derivación ABIERTA por conversación (anti-duplicado atómico).
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinical_handoff_abierto
    ON clinical_handoff (conversation_id)
    WHERE state IN ('detectado', 'solicitado', 'asignado', 'atendido');
