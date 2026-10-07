-- 012_delivery.sql — método de entrega del pedido (delivery o retiro en farmacia).
-- Idempotente: se re-ejecuta en cada arranque (Constitución III).
--
-- POR QUÉ EXISTE:
--   Antes del Resumen del Pedido se pregunta cómo quiere recibirlo:
--     1. Delivery        → se pide la DIRECCIÓN y se guarda en delivery_address
--     2. Retirar en Farmacia
--   El resumen final muestra el MÉTODO DE ENTREGA con esa información.
--
-- delivery_method : NULL (aún no elegido) | 'delivery' | 'pickup'
-- delivery_address: la dirección escrita por el cliente (solo si method='delivery')
-- delivery_pending: '' | 'method' | 'address'
--   ''        → no hay ninguna pregunta de entrega esperando respuesta
--   'method'  → el agente preguntó delivery/retiro y espera la elección
--   'address' → el cliente eligió delivery y el agente pidió la dirección
--
-- POR QUÉ delivery_pending ES NECESARIO:
--   Sin él no se puede distinguir un "1" o un "2" sueltos (respuesta a NUESTRA
--   pregunta) de un mensaje nuevo del cliente que empieza por número. Y al pedir
--   la dirección, hay que saber que el SIGUIENTE mensaje es la dirección y no una
--   consulta de medicamento.

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS delivery_method TEXT;

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS delivery_address TEXT;

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS delivery_pending TEXT NOT NULL DEFAULT '';
