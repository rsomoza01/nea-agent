-- 004_bot_cart.sql — carrito de compra del rol farmacéutico (spec 001, FR-8).
-- Una tabla por conversación: acumula medicamentos con cantidad y precios.
-- Se limpia al finalizar el pedido (LISTO). Idempotente.

CREATE TABLE IF NOT EXISTS bot_cart (
  id              BIGSERIAL PRIMARY KEY,
  conversation_id BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
  product_id      TEXT NOT NULL,
  producto        TEXT NOT NULL,
  presentacion    TEXT DEFAULT '',
  laboratorio     TEXT DEFAULT '',
  cantidad        INT NOT NULL DEFAULT 1 CHECK (cantidad >= 1),
  precio_usd      NUMERIC,
  precio_bs       NUMERIC,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bot_cart_conv ON bot_cart (conversation_id);

-- Para el upsert (agregar el mismo producto incrementa cantidad).
CREATE UNIQUE INDEX IF NOT EXISTS uq_bot_cart_conv_product ON bot_cart (conversation_id, product_id);
