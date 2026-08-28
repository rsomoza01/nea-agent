-- 006_last_term.sql — persiste el último término consultado (para re-consultar
-- el catálogo cuando el cliente refina con miligramo/marca sin repetir el nombre).
ALTER TABLE bot_conversation ADD COLUMN IF NOT EXISTS last_term TEXT;
