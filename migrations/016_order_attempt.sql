-- Paso 7 — Entidad de intento de pedido persistida en NEA (ADR-0007 §Revisión
-- 2026-11-12, decisiones 1-3 confirmadas definitivas por el propietario).
--
-- Registro DURABLE y auditable de cada confirmación de pedido, creado ANTES de la
-- nota best-effort del CRM (`tools._finalizar_pedido`), para que el pedido no se
-- pierda si la nota del CRM falla. La reserva es atómica e idempotente por intento
-- lógico — mismo idioma que `processed_message`/`Store.mark_processed` (db.py:99):
--   INSERT INTO order_attempt (order_attempt_id, conversation_id, cart_fingerprint)
--   VALUES ($1, $2, $3) ON CONFLICT (order_attempt_id) DO NOTHING RETURNING *;
-- Si RETURNING viene vacío, el intento ya existía: SELECT y se devuelve ese (no se
-- crea otro ni se sobrescribe la huella de la primera reserva).
--
-- La fuente de verdad de precio/disponibilidad NO es esta tabla: sigue siendo el
-- catálogo del CRM (ADR-0007 §Revisión P8). Aquí solo se guarda la HUELLA del
-- carrito como evidencia para revalidar/auditar, nunca como precio autoritativo.
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
