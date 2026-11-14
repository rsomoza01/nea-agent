# ADR-0010 — Identidad verificable de empleado por sesión emitida: DESBLOQUEO del Staff Agent

- **Estado:** Aceptada e **implementada** (Paso 11)
- **Fecha:** 2026-11-14
- **Paso del plan:** 11 (Staff Agent — superficie interna para empleados)
- **Desbloquea:** [`ADR-0008`](ADR-0008-identidad-staff-bloqueada.md) (bloqueo operativo del Staff)
- **Relacionada con:** [`../SECURITY-ACTOR-POLICIES.md`](../SECURITY-ACTOR-POLICIES.md) §3-§7,
  [`../POLICY-MATRIX.md`](../POLICY-MATRIX.md), [`ADR-0002`](ADR-0002-llm-sin-autoridad.md),
  [`ADR-0003`](ADR-0003-puerta-unica-autorizacion.md), [`ADR-0006`](ADR-0006-handoff-clinico-fail-closed.md),
  PLAN §7 (tenant), §8 q3 (identidad de empleado), §9

## Contexto

[`ADR-0008`](ADR-0008-identidad-staff-bloqueada.md) bloqueó el Paso 11 porque NEA **no
tenía identidad verificable de empleados**: el cliente es un nº de WhatsApp (no
autenticado) y el "Staff" era la posesión de una `X-API-Key` **global compartida** (una
integración, no una persona). El §8 de `SECURITY-ACTOR-POLICIES.md` fijó la precondición
de desbloqueo: el propietario debe **aprobar un modelo de identidad verificable de
empleado** — canal de autenticación, emisión/expiración de sesión y mapa
empleado→farmacia→rol.

El propietario aprobó dicho modelo (sesión de empleado con token emitido). Este ADR lo
materializa y, con ello, **levanta el bloqueo de ADR-0008**.

## Decisión

Se implementa una **identidad de empleado de primera clase**, independiente de los
secretos de canal existentes:

1. **Empleado como entidad real** (`staff_user`, migración 018): `id`, `pharmacy_id`,
   `username`, `role` (`clerk` ⊂ `pharmacist` ⊂ `admin`), `secret_hash`, `active`. La
   credencial se guarda **hasheada** (pbkdf2-hmac-sha256 + sal por usuario, stdlib; sin
   dependencias nuevas). Unicidad `(pharmacy_id, username)`.

2. **Sesión emitida, con TTL y revocable** (`staff_session`): `POST /staff/login`
   verifica credencial y emite un **token Bearer opaco**; la BD guarda solo su
   `sha256` (`token_hash`), nunca el token. La sesión fija `pharmacy_id` y `role`,
   tiene `expires_at` (TTL 8 h) y `revoked` (logout/revocación). `verificar_sesion`
   es **fail-closed**: token ausente/desconocido, revocado o expirado → sin actor.

3. **`AuthorizationContext` resuelto en backend** desde la sesión (nunca desde el
   texto/LLM — [ADR-0002](ADR-0002-llm-sin-autoridad.md)): `actor=STAFF`, `actor_id`
   (empleado real), `pharmacy_id` y `role` de la sesión, `correlation_id` por request.

4. **Policy-engine deny-by-default** (`app/staff/policy.autorizar`): autoriza solo la
   tupla (Staff autenticado × operación del **catálogo cerrado** × rol suficiente ×
   farmacia coincidente × precondición). El `pharmacy_id` entrante se **compara**, no se
   adopta (anti tenant-crossing). Las mutaciones de riesgo exigen **confirmación
   explícita**. El LLM no construye SQL ni invoca operaciones arbitrarias: solo existen
   las claves de `OPERACIONES`.

5. **Auditoría append-only** (`staff_audit`): toda operación — **autorizada o
   denegada** — registra `correlation_id`, `actor_id`, `pharmacy_id`, `action`,
   `resource_ref` (por referencia) y `result`. **Sin PII ni texto clínico**
   (coherente con [ADR-0006](ADR-0006-handoff-clinico-fail-closed.md) y PLAN §7 p5).

6. **Separación Customer/Staff.** La persistencia de identidad/sesión/auditoría vive en
   un `StaffStore` aparte (`app/staff/`), montado en `AppContext.staff`. La **lógica de
   dominio no se duplica**: las operaciones Staff reutilizan los casos de uso/consultas
   existentes (analytics, pedidos, perfiles) con una capa de política delante.

7. **Piloto cerrado** (punto 10 del Paso 11): el esquema **no siembra empleados**. Sin
   filas en `staff_user`, `/staff/login` no autentica a nadie y **todo queda denegado
   por defecto** (fail-closed). El propietario provisiona empleados explícitamente
   (`construir_secret_hash` + `crear_staff_user`), fuera de runtime.

## Modelo de identidad (resumen)

| Concepto | Antes (ADR-0008) | Ahora (ADR-0010) |
|---|---|---|
| Empleado | no existe | fila `staff_user` (farmacia + rol + credencial hasheada) |
| Autenticación | — (API-key compartida ≠ persona) | `POST /staff/login` → token Bearer |
| Sesión | no existe | `staff_session` con TTL + revocación |
| Tenant del actor | — | fijado en la sesión; se compara, no se adopta |
| Auditoría Staff | diseñada, bloqueada | `staff_audit` append-only (autorizados y denegados) |

## Alternativas consideradas

| Alternativa | Por qué se descarta |
|---|---|
| API-key por empleado (sin sesión) | Satisface actor/rol/tenant, pero sin expiración temporal de sesión; N4 (identidad expirada) quedaría como rotación de key, no como caducidad. El propietario eligió el modelo con sesión. |
| Reutilizar `CRM_BOT_API_KEY` como rol | Prohibido por ADR-0008: simular autenticación. Un secreto compartido no identifica a una persona. |
| JWT firmado sin estado en BD | No permite revocación individual inmediata; la tabla de sesión da logout/revocación reales y es coherente con el idioma idempotente del repo. |
| Dependencia de hashing (bcrypt/passlib) | pbkdf2 de stdlib basta y evita dependencia nueva (regla del plan: cambios acotados). |

## Consecuencias

- **Positivas:** el Paso 11 queda **desbloqueado e implementado**; las negativas
  N2 (tenant-crossing), N3 (rol insuficiente) y N4 (identidad expirada) de
  `SECURITY-ACTOR-POLICIES.md` §7 son ahora **ejercitables y probadas**
  (`tests/test_staff_agent.py`). F-1 (IDOR de analytics) queda **mitigado** (binding
  `provider_id == settings.provider_id` en mono-tenant).
- **Negativas / deuda registrada:**
  - El piloto expone un conjunto **mínimo** de operaciones (analytics, consulta de
    pedido, consentimiento crónico). Ampliarlo exige clasificar cada operación nueva
    por riesgo y añadirla al catálogo cerrado con su política.
  - No hay rotación automática de credenciales ni UI de administración de empleados:
    la provisión es manual (deliberado para el piloto cerrado).
  - F-2 (exigir firma Meta en producción) **sigue abierto**; no lo toca este paso.
