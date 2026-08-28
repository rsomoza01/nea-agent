-- 007_last_options.sql — persiste la lista de opciones (productos) consultada
-- en el último buscar_medicamento, ORDENADA por precio (menor a mayor), igual
-- que la muestra el formateador.
-- Lo usa el backstop de carrito: cuando el cliente elige por número
-- ("quiero 2 cajas de la opción 3"), el agente resuelve el índice contra esta
-- lista real en un mensaje NUEVO (el ToolRuntime de ese turno no re-consultó).
-- Idempotente: se re-ejecuta en cada arranque.

ALTER TABLE bot_conversation ADD COLUMN IF NOT EXISTS last_options JSONB;
