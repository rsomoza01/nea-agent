# ADR-0008 — Autorización por actor: diseño fail-closed y BLOQUEO de Staff por falta de identidad verificable

> ## ✅ DESBLOQUEADO (2026-11-14) por [`ADR-0010`](ADR-0010-identidad-staff-sesion.md)
>
> El bloqueo operativo que fija este ADR se levantó cuando el propietario **aprobó un
> modelo de identidad verificable de empleado** (sesión con token emitido). Ese modelo
> está implementado en [`ADR-0010`](ADR-0010-identidad-staff-sesion.md) + migración
> `018_staff_identity.sql` + `app/staff/`. Las negativas N2/N3/N4 del §7 de
> [`../SECURITY-ACTOR-POLICIES.md`](../SECURITY-ACTOR-POLICIES.md), antes BLOCKED, son
> ahora **ejercitables y probadas** (`tests/test_staff_agent.py`). El diseño de este ADR
> (deny-by-default, no simular auth, no promover la API-key a rol) **sigue vigente**: es
> exactamente lo que ADR-0010 implementa.

- **Estado:** Aceptada (diseño) + bloqueo operativo **LEVANTADO por ADR-0010** (2026-11-14)
- **Fecha:** 2026-11-13
- **Paso del plan:** 9 (Identidad, permisos y frontera Customer/Staff)
- **Relacionada con:** [`../POLICY-MATRIX.md`](../POLICY-MATRIX.md), [`../SECURITY-ACTOR-POLICIES.md`](../SECURITY-ACTOR-POLICIES.md), [`../DOMAIN-MODEL.md`](../DOMAIN-MODEL.md) §1, [`ADR-0002`](ADR-0002-llm-sin-autoridad.md), [`ADR-0003`](ADR-0003-puerta-unica-autorizacion.md), PLAN §7 (aislamiento de tenant) y §8 q3 (identidad de empleado), §9

## Contexto

El Paso 9 pide diseñar autorización server-side por **actor × acción × recurso ×
farmacia**, aplicar *tenant scope* en cada repositorio/servicio, denegar por defecto
y registrar auditoría de cada mutación Staff. El modo del paso es explícito:
**"requiere modelo de identidad aprobado"** y **"si el producto no dispone de
identidad verificable para empleados, entrega diseño y bloqueo; no simules
autenticación"**.

### Auditoría de identidad (puntos 1-2 del paso) — evidencia verificada

| Actor | Identidad real hoy | ¿Autenticada? | Evidencia (ruta:línea) |
|---|---|---|---|
| **Cliente** | número de WhatsApp / BSUID, canonicalizado | **No** (identificador de canal, no credencial) | `webhook.extract_inbound:98-106`, `config.canonical_identity:12-22` |
| **"Staff" (analytics / lab `/chat`)** | `X-API-Key == CRM_BOT_API_KEY` | **No es una persona**: secreto de canal **compartido**, sin usuario/rol | `webhook.py:195-197,395-397,437-439`; `config.Settings.crm_bot_api_key:37` |
| **Empleado / farmacéutico / administrador** | — **no existe** — | — | sin tabla, sin sesión, sin token en `migrations/001..017` ni en `app/` |
| **Farmacia / tenant** | `provider_id` (env, una farmacia por instancia) + `providerId` del contexto CRM | **Sí, backend** | `config.Settings.provider_id:46`; `DOMAIN-MODEL.md:19` |

Hechos confirmados durante la auditoría:
- **No hay** mecanismo de sesión, login, contraseña ni JWT de empleado en el código ni
  en el esquema (grep sobre `app/` y `migrations/`: 0 resultados de
  `session|login|password|jwt|oauth|bearer`). Los únicos "tokens" existentes
  (`verify_token` del handshake de Meta, `crm_bot_api_key`) son **secretos de canal**,
  no sesiones de persona. El único `role` del esquema
  (`migrations/001_init.sql:21`, tabla `bot_message`) es el rol del mensaje de chat
  (`user`/`assistant`/`tool`), **no** un rol de empleado.
- La `X-API-Key` es **un único secreto global** (`CRM_BOT_API_KEY`), sin binding a un
  tenant ni a una persona: quien la posee es "la integración", no un empleado con
  permisos propios (ya documentado en `POLICY-MATRIX.md:66-71`).
- **Hallazgo de seguridad (cross-tenant):** los endpoints `/analytics/top-meds` y
  `/analytics/chronic-patients` aceptan el `provider_id` **del query string** sin
  validarlo contra ningún contexto ligado a la credencial (`webhook.py:398-399,
  440-441`). Como la API-key es global y compartida, cualquier poseedor de la key
  puede leer analytics de **cualquier** farmacia cambiando `provider_id`. Es IDOR /
  tenant-crossing de lectura. Se registra como control requerido en
  `SECURITY-ACTOR-POLICIES.md` §6 (no se corrige en este paso: docs-only).

**Conclusión:** el producto **no dispone de identidad verificable de empleados**. Se
cumple el supuesto del modo que obliga a **entregar diseño + bloqueo**.

## Decisión

1. **Diseño sí, implementación de Staff NO.** Se entrega el diseño completo de la
   autorización server-side (ver [`SECURITY-ACTOR-POLICIES.md`](../SECURITY-ACTOR-POLICIES.md)):
   modelo actor/acción/recurso/farmacia, deny-by-default, *tenant scope* en capa de
   repositorio, auditoría de mutaciones Staff y protecciones anti-IDOR / cambio de
   tenant / spoofing de rol / replay. **No se escribe código de runtime** que cree
   roles, sesiones o herramientas Staff.

2. **No se simula autenticación.** Queda **prohibido** introducir un "rol" derivado
   de la posesión de la API-key, un empleado "ficticio", o cualquier identidad de
   staff no respaldada por un canal de autenticación real y aprobado. Un secreto de
   canal compartido **no** es identidad de persona y **no** se promoverá a actor
   Staff.

3. **Deny-by-default como postura vigente.** Mientras no exista identidad de empleado
   aprobada, **toda** acción Staff/rol-dependiente se **deniega**: identidad
   desconocida, rol ausente, farmacia no coincidente y contexto ambiguo → denegar
   ([ADR-0002](ADR-0002-llm-sin-autoridad.md), fail-closed). Esto ya es, de hecho, el
   estado del sistema (no hay superficie Staff mutante expuesta al cliente); el
   diseño lo formaliza.

4. **No se cambian canales de autenticación ni se activan funciones Staff** (punto 10
   del paso) sin aprobación explícita del propietario.

5. **Bloqueo de los Pasos dependientes.** El Paso 11 (Staff Agent) **no puede
   empezar** hasta que (a) se apruebe un modelo de identidad verificable de empleado,
   (b) se implemente y (c) se prueben las negativas del §7 de
   `SECURITY-ACTOR-POLICIES.md`. Este ADR es la precondición de seguridad del Paso 11.

## Alternativas consideradas

| Alternativa | Por qué se descarta |
|---|---|
| Tratar la posesión de `CRM_BOT_API_KEY` como "rol Staff" | Es simular autenticación (prohibido). Un secreto compartido no identifica a una persona ni habilita permisos individuales. |
| Implementar ya un policy-engine con roles de empleado | No hay identidad que alimente al engine; sería andamiaje de una función bloqueada y daría falsa sensación de seguridad. |
| Derivar identidad de empleado del número de WhatsApp | Un número de WhatsApp no es credencial de empleado (punto 1 del paso lo prohíbe explícitamente). |
| Parar solo en la auditoría | El paso pide, para el caso sin identidad, **diseño + bloqueo**; el diseño queda listo para implementar en cuanto se apruebe el modelo. |

## Consecuencias

- **Positivas:** postura de seguridad explícita y fail-closed; el diseño está listo y
  revisado para cuando exista identidad; el Paso 11 tiene una puerta de entrada clara.
- **Negativas / deuda registrada:**
  - El hallazgo IDOR de analytics (cross-tenant por `provider_id` de query) **sigue
    presente** tras este paso (docs-only). Mitigación mínima disponible sin identidad
    nueva: ligar `provider_id` a `settings.provider_id` en instancias mono-tenant
    (ver `SECURITY-ACTOR-POLICIES.md` §6); requiere aprobación para tocar runtime.
  - Las pruebas negativas de cross-tenant / rol insuficiente / identidad expirada
    quedan **diseñadas pero BLOCKED** (no hay identidad que ejercitar). Solo la
    negativa "cliente no alcanza superficie Staff" es verificable hoy y es
    estructuralmente cierta (el cliente entra por WhatsApp; `/chat` y `/analytics/*`
    exigen la API-key que el cliente no posee).
