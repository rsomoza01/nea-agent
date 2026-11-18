-- Sello del resumen pendiente de confirmación.
--
-- Cuando el agente manda el resumen con
--   "¿Confirmas el pedido con un *SI*, o quieres agregar otro medicamento?"
-- el pedido queda esperando UNA de dos cosas: un SI (cerrar) o un medicamento
-- (agregar). Ese estado no estaba guardado en ninguna parte: la lista de opciones
-- ya no aplica (el resumen no es una lista) y la pregunta de entrega ya se había
-- contestado, así que lo que el cliente respondiera caía al azar del modelo.
--
-- Medido en el provider 04 (18-nov, AFLAMAX + ATAMEL, retiro en farmacia):
--   "No" → el agente repitió el resumen completo (o el menú de entrega)
--   "2"  → "⚠️ Ojo: esas opciones no coinciden con la última lista que te envié"
-- El cliente quedó sin forma de cerrar el pedido.
--
-- Se enciende al enviar el resumen y se apaga cuando el carrito cambia, cuando el
-- pedido se cierra, cuando el cliente elige entrega en ese momento o cuando el
-- mensaje trata de otra cosa.
ALTER TABLE bot_conversation
    ADD COLUMN IF NOT EXISTS confirmacion_pendiente BOOLEAN NOT NULL DEFAULT FALSE;
