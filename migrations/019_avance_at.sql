-- 019_avance_at.sql — Sello del último AVANCE REAL de la conversación.
--
-- POR QUÉ EXISTE
-- El candado de cierre (app/stall.py) decidía "esta conversación no va a ningún
-- lado" con `len(mensajes_del_lead) >= MAX_MENSAJES_SIN_AVANCE` sobre la ventana
-- de historial. Eso no mide estancamiento: mide el TAMAÑO DEL HILO. Cualquier
-- conversación que llegara a 14 mensajes del cliente se cerraba, aunque el
-- cliente estuviera comprando.
--
-- Caso real (provider 05 = FarmaHogar, 2026-11-17 19:00 UTC): el cliente pidió
-- "quiero 1 caja de la opcion 8", el agente respondió "✅ Agregado al carrito:
-- OMEGA 3 X 45 CAP NOW ×1" y en ESE MISMO turno marcó stalled_at. Los tres
-- mensajes siguientes ("listo", "dame el resumen", "hola") quedaron sin
-- respuesta 24 h (STALL_COOLDOWN).
--
-- QUÉ GUARDA
-- avance_at = instante del último turno que produjo algo concreto: lista de
-- opciones del catálogo, movimiento del carrito, elección de entrega, derivación
-- o agendamiento. NULL = nunca hubo avance (cuenta todo el hilo, que es lo
-- correcto: si nunca avanzó, el candado puede cerrar).
--
-- Idempotente: se re-ejecuta en cada arranque (app/db.py migrate()).

ALTER TABLE bot_conversation
    ADD COLUMN IF NOT EXISTS avance_at TIMESTAMPTZ;
