-- Paso 11 — Identidad verificable de empleado + sesión + auditoría Staff.
--
-- DESBLOQUEA el Staff Agent bloqueado por ADR-0008: materializa lo que el §8 de
-- SECURITY-ACTOR-POLICIES exigía para empezar el Paso 11 — un modelo de identidad
-- de empleado real (NO la X-API-Key compartida, NO el nº de WhatsApp): mapa
-- empleado→farmacia→rol, emisión/expiración/revocación de sesión y auditoría
-- append-only de toda consulta sensible y mutación.
--
-- Decisiones de seguridad (ADR-0010):
--   * secret_hash: credencial hasheada (pbkdf2-hmac-sha256 con sal por usuario);
--     NUNCA se guarda el secreto en claro. Ver app/staff/identity.py.
--   * staff_session.token_hash: se guarda el SHA-256 del token Bearer, no el token;
--     si se filtra la tabla no se pueden reconstruir sesiones vivas.
--   * expires_at + revoked: la sesión caduca (TTL) y es revocable por persona — las
--     negativas N4 (identidad expirada) y "sesión revocada" se vuelven ejercitables.
--   * pharmacy_id en staff_user Y en staff_session: el tenant del empleado se fija al
--     emitir la sesión y se compara, NUNCA se adopta del query/body (anti tenant-crossing).
--   * staff_audit: append-only (el código solo INSERTA; no hay UPDATE/DELETE). Registra
--     autorizados Y denegados (un intento denegado es señal de abuso). No guarda PII ni
--     texto clínico: acción + recurso por REFERENCIA (coherente con ADR-0006/PLAN §7 p5).
--
-- Piloto CERRADO (punto 10 del Paso 11): este esquema NO siembra ningún empleado.
-- Sin filas en staff_user, /staff/login no autentica a nadie y toda operación Staff
-- queda denegada por defecto (fail-closed). El propietario provisiona empleados
-- explícitamente (app/staff/identity.py: construir_secret_hash) fuera de este archivo.
--
-- Rollback: DROP TABLE staff_audit; DROP TABLE staff_session; DROP TABLE staff_user;

CREATE TABLE IF NOT EXISTS staff_user (
    id           TEXT PRIMARY KEY,                 -- uuid del empleado
    pharmacy_id  TEXT NOT NULL,                    -- tenant (provider_id) al que pertenece
    username     TEXT NOT NULL,                    -- identificador de login (por farmacia)
    role         TEXT NOT NULL,                    -- clerk | pharmacist | admin
    secret_hash  TEXT NOT NULL,                    -- pbkdf2$iter$sal$hash (nunca en claro)
    active       BOOLEAN NOT NULL DEFAULT TRUE,    -- desactivar = revocación individual
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Un username es único DENTRO de una farmacia (dos farmacias pueden reusar nombres).
CREATE UNIQUE INDEX IF NOT EXISTS uq_staff_user_pharmacy_username
    ON staff_user (pharmacy_id, username);

CREATE TABLE IF NOT EXISTS staff_session (
    token_hash     TEXT PRIMARY KEY,               -- sha256(token); el token vive solo en el cliente
    staff_user_id  TEXT NOT NULL REFERENCES staff_user(id) ON DELETE CASCADE,
    pharmacy_id    TEXT NOT NULL,                  -- copiado del empleado al emitir (se compara, no se adopta)
    role           TEXT NOT NULL,                  -- copiado del empleado al emitir
    issued_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL,           -- TTL: caducidad obligatoria
    revoked        BOOLEAN NOT NULL DEFAULT FALSE  -- logout / revocación explícita
);

CREATE INDEX IF NOT EXISTS idx_staff_session_user
    ON staff_session (staff_user_id);

CREATE TABLE IF NOT EXISTS staff_audit (
    id             BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL,                  -- liga la operación con el request
    actor_id       TEXT,                           -- empleado autenticado (NULL si login falló)
    pharmacy_id    TEXT,                           -- farmacia sobre la que operó / intentó
    action         TEXT NOT NULL,                  -- operación de dominio (registro cerrado)
    resource_ref   TEXT,                           -- recurso afectado por REFERENCIA (id, no PII)
    result         TEXT NOT NULL,                  -- autorizado | denegado:<motivo> | error:<cod>
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_staff_audit_pharmacy_created
    ON staff_audit (pharmacy_id, created_at DESC);
