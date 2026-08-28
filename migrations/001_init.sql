-- 001_init.sql — tablas del estado propio de Nea.
-- Idempotente: se re-ejecuta completo en cada arranque (Constitución III).

CREATE TABLE IF NOT EXISTS bot_conversation (
  id                  BIGSERIAL PRIMARY KEY,
  wa_identity         TEXT NOT NULL UNIQUE,
  crm_conversation_id TEXT,
  phase               TEXT NOT NULL DEFAULT 'descubrimiento',
  greeted             BOOLEAN NOT NULL DEFAULT FALSE,
  media_notice_sent   BOOLEAN NOT NULL DEFAULT FALSE,
  followup_due_at     TIMESTAMPTZ,
  followup_sent       BOOLEAN NOT NULL DEFAULT FALSE,
  last_inbound_at     TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_message (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  wa_message_id   TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bot_message_conv ON bot_message (conversation_id, id);

-- Dedup por wa_message_id: el INSERT ... ON CONFLICT DO NOTHING es el gate atómico.
CREATE TABLE IF NOT EXISTS processed_message (
  wa_message_id TEXT PRIMARY KEY,
  processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Solo lo aquí presente es reservable; se limpia al reservar.
CREATE TABLE IF NOT EXISTS offered_slots (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
  start_utc       TIMESTAMPTZ NOT NULL,
  end_utc         TIMESTAMPTZ,
  label           TEXT NOT NULL,
  offered_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_offered_slots_conv ON offered_slots (conversation_id);

-- Cola persistente del relay al CRM (backoff exponencial, se agota a las 24 h).
CREATE TABLE IF NOT EXISTS relay_queue (
  id            BIGSERIAL PRIMARY KEY,
  body          BYTEA NOT NULL,
  signature     TEXT,
  attempts      INT NOT NULL DEFAULT 0,
  next_retry_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered_at  TIMESTAMPTZ,
  abandoned_at  TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_relay_queue_due
  ON relay_queue (next_retry_at)
  WHERE delivered_at IS NULL AND abandoned_at IS NULL;
