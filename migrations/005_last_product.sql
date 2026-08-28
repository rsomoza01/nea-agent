-- 005_last_product.sql — persiste el último producto consultado en la conversación.
-- Lo usa el backstop de carrito: cuando el cliente responde una cantidad en un
-- mensaje NUEVO, el agente sabe qué producto añadir aunque el ToolRuntime de ese
-- turno no haya consultado el catálogo.
-- Idempotente: se re-ejecuta en cada arranque.

ALTER TABLE bot_conversation ADD COLUMN IF NOT EXISTS last_product JSONB;
