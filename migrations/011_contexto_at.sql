-- 011_contexto_at.sql — cuándo se refrescó el CONTEXTO de producto de la conversación
-- (`last_product`, `last_term`, `last_options`).
--
-- Esos tres campos alimentan el bloque de estado que se le pasa al LLM ("último producto
-- que el cliente vio", "última búsqueda"). Sin fecha se presentan como si fueran de ahora
-- aunque tengan semanas: en el caso del 2026-11-13 (provider 05) el agente reconstruyó un
-- pedido de hacía 37 días —"Tu pedido es 1 caja de ACETAMINOFEN … por $1.80, ya registrado
-- para que un asesor lo procese"— ante un cliente que solo dijo que se le borraron los
-- chats.
--
-- `updated_at` no sirve como fecha: cambia con CUALQUIER campo de la fila.
-- Idempotente: se re-ejecuta en cada arranque.

ALTER TABLE bot_conversation ADD COLUMN IF NOT EXISTS contexto_at TIMESTAMPTZ;
