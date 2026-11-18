-- =============================================================================
-- MIGRACIONES PENDIENTES PARA `desarrollo` — bundle listo para aplicar
-- =============================================================================
-- Generado por QA (Paso 12 / consolidación en prototipo), 2026-11-15.
--
-- NO es una migración del runtime: vive en docs/ a propósito, para que
-- app.db.PgStore.migrate() (que solo recorre migrations/*.sql) NO lo aplique
-- dos veces. Es una copia de conveniencia, VERBATIM, de los archivos canónicos:
--   migrations/016_order_attempt.sql
--   migrations/017_clinical_handoff.sql
--   migrations/018_staff_identity.sql
-- La fuente de verdad son esos archivos. Este bundle es para aplicar a mano,
-- DESPUÉS del backup, si se prefiere no depender del migrate() de arranque.
--
-- Precondición OBLIGATORIA: backup de la base de datos ANTES de ejecutar esto
-- (ver docs/architecture/MIGRATIONS-RUNBOOK-desarrollo.md §Backup). A la fecha
-- de este commit el backup NO se ha hecho: NO EJECUTAR todavía.
--
-- Todo es aditivo e idempotente (CREATE TABLE/INDEX IF NOT EXISTS). Re-ejecutar
-- es seguro. desarrollo (6559228) ya tiene 001–015; esto solo añade 016/017/018.
-- =============================================================================

BEGIN;

-- --------------------------------------------------------------------------
-- 016_order_attempt.sql — intento de pedido persistido (Paso 7, ADR-0007)
-- Rollback: DROP TABLE order_attempt;
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_attempt (
    order_attempt_id TEXT PRIMARY KEY,
    conversation_id  BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
    cart_fingerprint TEXT NOT NULL,
    phase            TEXT NOT NULL DEFAULT 'confirmado_por_cliente',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_order_attempt_conv ON order_attempt (conversation_id);

-- --------------------------------------------------------------------------
-- 017_clinical_handoff.sql — derivación clínica persistida (Paso 8, UC-08)
-- Rollback: DROP TABLE clinical_handoff;
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clinical_handoff (
    clinical_handoff_id TEXT PRIMARY KEY,
    conversation_id     BIGINT NOT NULL REFERENCES bot_conversation(id) ON DELETE CASCADE,
    category            TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'solicitado',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_clinical_handoff_conv
    ON clinical_handoff (conversation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_clinical_handoff_abierto
    ON clinical_handoff (conversation_id)
    WHERE state IN ('detectado', 'solicitado', 'asignado', 'atendido');

-- --------------------------------------------------------------------------
-- 018_staff_identity.sql — identidad/sesión/auditoría Staff (Paso 11, ADR-0010)
-- Piloto CERRADO: NO siembra empleados. Sin filas en staff_user, /staff/* queda
-- fail-closed. Rollback: DROP TABLE staff_audit; DROP TABLE staff_session; DROP TABLE staff_user;
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff_user (
    id           TEXT PRIMARY KEY,
    pharmacy_id  TEXT NOT NULL,
    username     TEXT NOT NULL,
    role         TEXT NOT NULL,
    secret_hash  TEXT NOT NULL,
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_staff_user_pharmacy_username
    ON staff_user (pharmacy_id, username);

CREATE TABLE IF NOT EXISTS staff_session (
    token_hash     TEXT PRIMARY KEY,
    staff_user_id  TEXT NOT NULL REFERENCES staff_user(id) ON DELETE CASCADE,
    pharmacy_id    TEXT NOT NULL,
    role           TEXT NOT NULL,
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,
    revoked        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_staff_session_user
    ON staff_session (staff_user_id);

CREATE TABLE IF NOT EXISTS staff_audit (
    id             BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    actor_id       TEXT,
    pharmacy_id    TEXT,
    action         TEXT NOT NULL,
    resource_ref   TEXT,
    result         TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_staff_audit_pharmacy_created
    ON staff_audit (pharmacy_id, created_at DESC);

COMMIT;

-- =============================================================================
-- VERIFICACIÓN (post-aplicación)
-- =============================================================================
--   SELECT tablename FROM pg_tables WHERE schemaname='public'
--     AND tablename IN ('order_attempt','clinical_handoff','staff_user','staff_session','staff_audit');
--   -- esperado: 5 filas
--   SELECT count(*) FROM staff_user;   -- esperado: 0 (piloto cerrado)
-- =============================================================================
