-- 010_last_options_at.sql — cuándo se guardó `last_options`.
--
-- `last_options` es la lista de opciones (productos con precio) que el cliente YA vio, y
-- sirve para resolver "quiero 2 cajas de la opción 3" en un mensaje posterior. No tenía
-- fecha: `updated_at` de la fila cambia con CUALQUIER campo, así que no dice cuándo se
-- guardó la lista. Sin fecha no se puede caducar, y una lista de hace dos semanas seguiría
-- resolviendo selecciones contra precios que ya no existen.
--
-- Caso medido (provider 05, 2026-11-12): el agente reenvió una lista de ACETAMINOFEN de
-- trece días antes porque venía en el historial y `last_options` también. Las listas del
-- prompt ya caducan (LISTA_PRECIOS_TTL_HORAS); esto caduca también la lista persistida.
--
-- Idempotente: se re-ejecuta en cada arranque.

ALTER TABLE bot_conversation ADD COLUMN IF NOT EXISTS last_options_at TIMESTAMPTZ;
