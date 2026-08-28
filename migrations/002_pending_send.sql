-- 002_pending_send.sql — cola durable de envíos al CRM que agotaron los
-- reintentos del turno (incidente 2026-08-03: la respuesta generada se
-- descartaba y el lead no recibía nada). Idempotente (Constitución III).

CREATE TABLE IF NOT EXISTS pending_send (
  id                  BIGSERIAL PRIMARY KEY,
  conversation_id     BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
  crm_conversation_id TEXT NOT NULL,
  content             TEXT NOT NULL,
  attempts            INT NOT NULL DEFAULT 0,
  next_retry_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at        TIMESTAMPTZ,
  abandoned_at        TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pending_send_due
  ON pending_send (next_retry_at)
  WHERE delivered_at IS NULL AND abandoned_at IS NULL;
