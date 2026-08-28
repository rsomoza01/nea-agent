-- 009_chronic_patients.sql — Fase 2: clasificación de pacientes crónicos.
--
-- condiciones_cronicas: tabla de referencia medicamento → condición crónica.
-- Se llena con un seed inicial (hipertensión, diabetes, colesterol, asma,
-- hipotiroidismo) y el dueño puede ampliarla.
--
-- patient_profiles: un paciente (wa_identity por farmacia) con condición
-- crónica detectada. La clasificación es AUTOMÁTICA: el mismo medicamento
-- crónico consultado >= 2 veces en 30 días (o >= 3 en 90) crea/actualiza el
-- perfil con nivel de confianza creciente.

CREATE TABLE IF NOT EXISTS condiciones_cronicas (
    id          BIGSERIAL PRIMARY KEY,
    pattern     TEXT NOT NULL UNIQUE,      -- término a matchear (lowercase, substring)
    condicion   TEXT NOT NULL,             -- 'hipertensión', 'diabetes', ...
    activo      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS patient_profiles (
    id              BIGSERIAL PRIMARY KEY,
    provider_id     TEXT NOT NULL DEFAULT '',
    wa_identity     TEXT NOT NULL,
    condicion       TEXT NOT NULL,
    confianza       INTEGER NOT NULL DEFAULT 1,      -- nº de consultas que sustentan la clasificación
    nivel           TEXT NOT NULL DEFAULT 'bajo',    -- bajo | medio | alto
    consent         BOOLEAN NOT NULL DEFAULT FALSE,
    consent_at      TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (provider_id, wa_identity, condicion)
);

CREATE INDEX IF NOT EXISTS idx_patient_profiles_provider ON patient_profiles (provider_id, consent);
CREATE INDEX IF NOT EXISTS idx_condiciones_pattern ON condiciones_cronicas (activo);

-- Seed inicial: medicamentos crónicos más comunes en Venezuela.
INSERT INTO condiciones_cronicas (pattern, condicion) VALUES
    ('losartan', 'hipertension'),
    ('enalapril', 'hipertension'),
    ('amlodipina', 'hipertension'),
    ('nifedipina', 'hipertension'),
    ('valsartan', 'hipertension'),
    ('candesartan', 'hipertension'),
    ('bisoprolol', 'hipertension'),
    ('carvedilol', 'hipertension'),
    ('atenolol', 'hipertension'),
    ('furosemida', 'hipertension'),
    ('hidroclorotiazida', 'hipertension'),
    ('metformina', 'diabetes'),
    ('glibenclamida', 'diabetes'),
    ('gliclazida', 'diabetes'),
    ('insulina', 'diabetes'),
    ('sitagliptina', 'diabetes'),
    ('empagliflozina', 'diabetes'),
    ('atorvastatina', 'colesterol'),
    ('simvastatina', 'colesterol'),
    ('rosuvastatina', 'colesterol'),
    ('lovastatina', 'colesterol'),
    ('salbutamol', 'asma'),
    ('budesonida', 'asma'),
    ('ipratropio', 'asma'),
    ('montelukast', 'asma'),
    ('levotiroxina', 'hipotiroidismo'),
    ('euthyrox', 'hipotiroidismo'),
    ('clopidogrel', 'cardiovascular'),
    ('warfarina', 'cardiovascular'),
    ('acido acetilsalicilico', 'cardiovascular'),
    ('aspirina', 'cardiovascular'),
    ('omeprazol', 'gastroparesia_reflujo'),
    ('pantoprazol', 'gastroparesia_reflujo'),
    ('esomeprazol', 'gastroparesia_reflujo')
ON CONFLICT (pattern) DO NOTHING;
