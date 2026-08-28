-- 003_stalled.sql — candado de cierre: marca de cuándo el agente cerró la
-- conversación por no ir a ningún lado. Mientras esté puesta, el agente no
-- responde (se reabre sola si el lead vuelve tras el periodo de enfriamiento
-- o si el dueño reactiva la IA desde el CRM). Idempotente.

ALTER TABLE bot_conversation
  ADD COLUMN IF NOT EXISTS stalled_at TIMESTAMPTZ;
