-- 010_cart_closed.sql — marca el carrito/pedido como CERRADO tras finalizar.
-- Idempotente: se re-ejecuta en cada arranque (Constitución III).
--
-- Diferencia con cart_summary_shown (009): ese flag se activa CADA VEZ que se
-- muestra el Resumen del Pedido (ver_carrito), incluido el flujo normal de
-- "¿Deseas buscar otro medicamento?" → "no" → resumen. En ese momento el
-- cliente está AMPLIANDO el pedido, no cerrándolo, así que NO debe reiniciar
-- el carrito.
--
-- cart_closed se activa ÚNICAMENTE al finalizar el pedido (finalizar_pedido /
-- LISTO). Solo entonces la siguiente consulta de medicamento arranca un
-- carrito nuevo en vez de acumular sobre el pedido ya procesado.

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS cart_closed BOOLEAN NOT NULL DEFAULT FALSE;
