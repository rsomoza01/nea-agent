-- PROPUESTA (NO aplicada a migrations/): entidad de intento de pedido en NEA.
--
-- Estado: validada contra Postgres desechable el 2026-11-12 (ver
-- docs/architecture/PR-feat-pharmacy-order-workflow.md §3b). NO mover a migrations/
-- hasta que el propietario confirme como DEFINITIVAS las decisiones estimadas de
-- ADR-0007 §Revisión 2026-11-12 (en especial el refinamiento de la fuente de verdad
-- de precio/disponibilidad = catálogo del CRM, no una tabla propia en NEA).
--
-- Rollback: DROP TABLE order_attempt;

CREATE TABLE IF NOT EXISTS order_attempt (
    order_attempt_id TEXT PRIMARY KEY,
    conversation_id  BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
    cart_fingerprint TEXT NOT NULL,
    phase            TEXT NOT NULL DEFAULT 'confirmado_por_cliente',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_attempt_conv ON order_attempt (conversation_id);

-- Reserva atómica e idempotente por intento lógico (ADR-0007 decisión 4, escenarios
-- A/B; mismo idioma que Store.mark_processed, db.py:99):
--   INSERT INTO order_attempt (order_attempt_id, conversation_id, cart_fingerprint)
--   VALUES ($1, $2, $3) ON CONFLICT (order_attempt_id) DO NOTHING
--   RETURNING *;
-- Si RETURNING viene vacío, el intento ya existía: SELECT y se devuelve ese (no se
-- crea otro ni se sobrescribe la huella de la primera reserva).
