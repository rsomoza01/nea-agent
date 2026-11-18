# NEA — Identidad, permisos y frontera Customer/Staff (Paso 9)

> ## ✅ ESTADO: DISEÑO (Paso 9) → IMPLEMENTADO (Paso 11, 2026-11-14)
>
> Este documento fue el **diseño + bloqueo** del Paso 9. El propietario **aprobó el
> modelo de identidad de empleado** y el Paso 11 lo **implementó**: identidad por sesión
> emitida ([`ADR-0010`](ADR/ADR-0010-identidad-staff-sesion.md)), migración
> `018_staff_identity.sql`, policy-engine deny-by-default y auditoría append-only en
> `app/staff/`, superficie `/staff/*` como **piloto cerrado**. El diseño §3-§6 de abajo
> es ahora el **contrato implementado**; las negativas §7 N2/N3/N4 — antes BLOCKED — son
> **ejercitables y probadas** (`tests/test_staff_agent.py`). El hallazgo **F-1** quedó
> **mitigado** (binding `provider_id == settings.provider_id` en mono-tenant). El bloqueo
> de [`ADR-0008`](ADR/ADR-0008-identidad-staff-bloqueada.md) está **levantado**.

> **Insumos:** [`POLICY-MATRIX.md`](POLICY-MATRIX.md), [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §1-§2,
> [`USE-CASES.md`](USE-CASES.md), [`ADR-0002`](ADR/ADR-0002-llm-sin-autoridad.md),
> [`ADR-0003`](ADR/ADR-0003-puerta-unica-autorizacion.md),
> [`ADR-0008`](ADR/ADR-0008-identidad-staff-bloqueada.md), PLAN §7/§8/§9.

---

## 1. Objetivo y alcance

Diseñar la autorización **determinista y server-side** de NEA por **actor × acción ×
recurso × farmacia × precondición × auditoría**, de forma que:

- el cliente nunca invoque acciones Staff;
- ninguna petición cruce datos entre farmacias (tenant scope en repositorio/servicio,
  no en prompts ni filtros de UI);
- la identidad desconocida, el rol ausente, la farmacia no coincidente y el contexto
  ambiguo **se denieguen por defecto**;
- toda mutación Staff quede auditada (actor, farmacia, acción, resultado,
  `correlation_id`).

**Fuera de alcance (bloqueado):** crear identidad/sesión de empleado, activar
herramientas Staff, cambiar canales de autenticación. Eso es Paso 11 y exige
aprobación previa del modelo de identidad.

---

## 2. Auditoría del estado actual (puntos 1-2)

### 2.1 ¿Cómo se identifica cada actor hoy?

| Actor | Identidad | ¿Credencial? | Evidencia |
|---|---|---|---|
| **Cliente** | nº WhatsApp / BSUID canónico | No — identificador de canal | `webhook.extract_inbound:98-106`, `config.canonical_identity:12-22` |
| **Integración** (dashboard/CRM que llama `/chat`, `/analytics/*`) | `X-API-Key == CRM_BOT_API_KEY` | Secreto **global compartido**, sin persona ni rol | `webhook.py:195-197,395-397,437-439` |
| **Empleado / farmacéutico / admin** | — no existe — | — | sin tabla/sesión/token en `migrations/001..017` ni `app/` |
| **Farmacia / tenant** | `provider_id` (env, 1 farmacia/instancia) + `providerId` del contexto CRM | Backend | `config.Settings.provider_id:46`, `turn._fetch_context` |
| **Sistema** | workers relay/followup/sender | — determinista, sin LLM | `followup.py`, `sender.py` |

> **Advertencia clave (no confundir identidad con canal):** el número de WhatsApp
> **no** autentica a nadie, y la `X-API-Key` autentica **a una integración, no a un
> empleado**. No se deben inferir permisos individuales de la posesión de la key.

### 2.2 Mecanismos reales de autenticación / sesión / tenant

| Mecanismo | Qué protege | Símbolo | Nota |
|---|---|---|---|
| Firma Meta `x-hub-signature-256` | `POST /webhook` | `webhook.verify_signature:35-42` | **acepta sin secret en dev** (si `META_APP_SECRET` vacío → pasa) |
| `X-API-Key` global | `/chat`, `/analytics/*` | `webhook.py:196,396,438` | secreto único; sin binding a tenant/persona |
| Verify token | `GET /webhook` (suscripción) | `webhook.verify:174` | handshake de Meta, no autoriza acciones |
| Allowlist `ALLOWED_WA_IDS` | a quién responde `run_turn` | `turn.py` (gate) | control de canal, no de rol; se salta en lab |
| Tester `/reset` | `TESTER_WA_IDS` | `config.tester_identities:166` | comando de pruebas; no es rol |
| **Sesión de empleado** | — | — | **NO EXISTE** |

**No hay** expiración de sesión, rotación de credencial por persona, ni revocación
individual: no existe el sujeto al que aplicarlas.

### 2.3 Hallazgos de seguridad registrados

- **F-1 (alta, cross-tenant / IDOR de lectura):** `/analytics/top-meds` y
  `/analytics/chronic-patients` toman `provider_id` del **query string** sin ligarlo a
  la credencial (`webhook.py:398-399,440-441`). Con la API-key global, cualquiera que
  la posea lee analytics de **cualquier** farmacia. → control §6.
- **F-2 (media):** `/webhook` **acepta sin firma** cuando `META_APP_SECRET` está vacío
  (`webhook.verify_signature:37-38`). Aceptable en dev; en producción debe exigir
  firma. → control §6.
- **F-3 (informativa):** dos rutas de ingesta (`/webhook` firmado vs `/chat` por
  API-key) alimentan el mismo `handle_flush` con contratos distintos
  (`POLICY-MATRIX.md:84-86`). El policy-engine debe tratarlas con la misma política.

---

## 3. Diseño: autorización server-side por actor × acción × recurso × farmacia (puntos 3-6)

### 3.1 El `AuthorizationContext` (quién pide qué, resuelto en backend)

Estructura tipada que **el backend construye** (nunca el LLM, nunca el texto del
mensaje — [ADR-0002](ADR/ADR-0002-llm-sin-autoridad.md)):

```
AuthorizationContext:
    actor:          ActorType      # Customer | Integration | System | Staff*(bloqueado)
    actor_id:       str            # wa_identity | nombre de integración | worker
    pharmacy_id:    str            # providerId resuelto por backend (obligatorio)
    role:           Role | None    # SIEMPRE None hoy (no hay roles de persona)
    authenticated:  bool           # Customer=False, Integration=por API-key, Staff=N/A
    correlation_id: str            # id de turno / request para auditoría y replay
    issued_at:      datetime       # para control de expiración (cuando haya sesión)
```

> `role` y las ramas `Staff` del enum existen **solo en el diseño**. No se materializan
> en runtime hasta que haya identidad aprobada (ADR-0008).

### 3.2 La decisión como resultado tipado (deny-by-default)

```
PolicyDecision = Autorizado(action, actor, resource, pharmacy_id)
               | Denegado(reason)         # fail-closed
               | RequiereAclaracion()     # contexto ambiguo → preguntar, no actuar
```

Regla madre (**punto 5**): la función de política devuelve `Denegado` **salvo** que
exista una regla **explícita** que autorice la tupla `(actor, action, resource,
pharmacy_id)` con sus precondiciones. En particular se deniega siempre que:

1. la identidad es desconocida / no autenticada para la acción pedida;
2. la acción requiere un rol y `role is None` (hoy: **todas** las acciones Staff);
3. `resource.pharmacy_id != context.pharmacy_id` (farmacia no coincidente);
4. el contexto es ambiguo (intención no concluyente) → `RequiereAclaracion`, no actuar.

### 3.3 Relación con la puerta única de catálogo (no duplicar — punto 6)

El policy-engine **no** sustituye ni duplica `tools._autorizar_busqueda` (F15/F15.1):
decide el plano general ("este actor puede intentar buscar catálogo de esta
farmacia"); **cada término** sigue pasando por la puerta única
([ADR-0003](ADR/ADR-0003-puerta-unica-autorizacion.md)). Son capas complementarias,
no una segunda puerta.

### 3.4 Customer vs Staff: políticas distintas, dominio compartido (punto 6)

- Los **casos de uso de dominio** (UC-01..UC-08: catálogo, carrito, pedido, handoff)
  **no se duplican**. Un Staff que en el futuro prepare un pedido ejecuta el **mismo**
  `OrderWorkflow`/`DerivarConsultaClinicaUseCase` que el Customer.
- Lo que cambia es **la política de entrada** (qué actor puede invocar qué caso de uso,
  sobre qué recurso y farmacia) y la **auditoría** (§5). La lógica de negocio vive una
  sola vez; la autorización es una capa delante.

---

## 4. Tenant scope en cada repositorio/servicio (punto 4)

**Invariante:** el `pharmacy_id`/`provider_id` **nunca** se acepta a ciegas del
cliente ni del LLM; proviene del `AuthorizationContext` resuelto en backend y se aplica
**en la capa de datos**, no en prompts ni en filtros de UI.

| Recurso | Clave de scope | Dónde se aplica (diseño) | Estado AS-IS |
|---|---|---|---|
| Catálogo CRM | `provider_id` del contexto | adapter de catálogo | ✅ ya por contexto |
| `bot_cart` | `conversation_id` (atado a tenant) | `db` cart repo | ✅ por conversación |
| `bot_conversation` | `wa_identity` + instancia mono-tenant | `db` | ✅ |
| `med_queries` / analytics | `provider_id` | repo de analytics | ⚠ **F-1**: hoy del query, sin binding |
| `patient_profiles` | `provider_id` | repo crónicos | ⚠ **F-1** |

**Patrón de diseño:** cada método de repositorio que lea/mute datos de tenant debe
**recibir `pharmacy_id` como argumento obligatorio** y filtrarlo en el `WHERE`/llamada
CRM, de modo que sea **imposible** construir una consulta sin scope. El control no
puede depender de que el llamador "se acuerde" de filtrar.

---

## 5. Auditoría de mutaciones Staff (punto 7)

Toda **mutación** ejecutada por un actor Staff (cuando exista) debe emitir un registro
de auditoría **inmutable y append-only** con, como mínimo:

```
staff_audit:
    correlation_id   TEXT        -- liga la operación con el request/turno
    actor_id         TEXT        -- empleado autenticado (NO la API-key compartida)
    pharmacy_id      TEXT        -- farmacia sobre la que operó
    action           TEXT        -- caso de uso / operación de dominio
    resource_ref     TEXT        -- recurso afectado (id de pedido, perfil, etc.)
    result           TEXT        -- autorizado/denegado/error + código
    created_at       TIMESTAMPTZ
```

- **No se registra PII ni contenido clínico** innecesario (coherente con
  [ADR-0006](ADR/ADR-0006-handoff-clinico-fail-closed.md) y PLAN §7 p5): se audita la
  **acción y el recurso por referencia**, no el texto del cliente.
- El registro es **obligatorio incluso para denegaciones** (intentos fallidos son
  señal de abuso).
- ✅ **IMPLEMENTADO (Paso 11):** la tabla `staff_audit` se crea en la migración
  `018_staff_identity.sql` y `actor_id` es el **empleado autenticado** (no la API-key).
  El código solo INSERTA (append-only); se auditan autorizados y denegados. Ver
  `app/staff/pg.PgStaffStore.registrar_auditoria` y `EnMemoriaStaffStore`.

---

## 6. Protecciones anti-IDOR / cambio de tenant / spoofing de rol / replay (punto 8)

| Amenaza | Diseño de control | Estado |
|---|---|---|
| **IDOR / tenant-crossing** (F-1) | `pharmacy_id` obligatorio en cada repo + validación del `provider_id` recibido contra el contexto de la credencial. Mitigación mínima sin identidad nueva: en instancia mono-tenant, exigir `provider_id == settings.provider_id` en `/analytics/*`. | ✅ **MITIGADO** (`webhook._provider_mismatch` → 403; `/staff/*` fija tenant desde la sesión) |
| **Cambio de tenant** | `pharmacy_id` solo del backend; nunca del LLM/texto/cliente. Cualquier `pharmacy_id` entrante se **compara**, no se **adopta**. | ✅ invariante vigente para catálogo/carrito; ⚠ pendiente en analytics (F-1) |
| **Spoofing de rol** | `role` nunca se deriva de la posesión de un secreto compartido ni del canal; solo de una sesión de empleado autenticada. | ✅ **IMPLEMENTADO** (`role` sale de `staff_session`; ADR-0010) |
| **Replay** | `correlation_id` único por operación + idempotencia. Patrón ya probado: dedup por `wa_message_id` (`processed_message`, gate `INSERT ... ON CONFLICT`) y reserva atómica de `order_attempt`/`clinical_handoff`. Las mutaciones Staff reusarían el mismo idioma idempotente. | **Patrón disponible; aplica al implementar Staff** |
| **Firma ausente** (F-2) | Exigir `META_APP_SECRET` en producción (rechazar `/webhook` sin firma). | **Diseñado; cambio de config requiere aprobación** |

---

## 7. Pruebas negativas (punto 9) — diseño y estado

El Paso 9 exige cuatro negativas. Sin identidad de empleado, **solo una es
ejercitable hoy**; las demás quedan **diseñadas y BLOCKED** (no hay sujeto que
expirar/cruzar/degradar). Ninguna debe implementarse simulando identidad.

| # | Prueba negativa | Resultado esperado | Estado | Por qué |
|---|---|---|---|---|
| N1 | **Cliente / no autenticado invoca operación Staff** | denegado (401) | ✅ **probado** | El cliente entra por WhatsApp; `/staff/*` exige sesión Bearer que no posee → 401 (`test_N1_no_autenticado_401_auditado`). Auditado como `denegado:no_autenticado`. |
| N2 | **Empleado de farmacia A opera en B** | denegado (tenant mismatch) | ✅ **probado** | `autorizar` compara `resource_pharmacy_id` con la farmacia de la SESIÓN → `denegado:tenant_mismatch` (`test_N2_tenant_crossing_denegado`). |
| N3 | **Rol insuficiente** | denegado | ✅ **probado** | `clerk` no alcanza `analytics.chronic_patients` (rol mínimo farmacéutico) → 403 `denegado:rol_insuficiente` (`test_N3_clerk_no_puede_cronicos_403`). |
| N4 | **Identidad expirada / revocada** | denegado (401) | ✅ **probado** | Sesión con `expires_at` vencido o `revoked` → `verificar_sesion` devuelve None → 401 (`test_N4_sesion_expirada_401`, `test_logout_revoca_sesion`). |

**Nota de honestidad de la prueba N1:** se documenta como estructuralmente cierta por
auditoría de código (no hay superficie Staff tras la puerta del cliente), sin
introducir un actor Staff falso para "probarla". Si el propietario lo pide, puede
añadirse un test que afirme el 401 de `/chat`/`/analytics/*` sin API-key — control de
canal real y no-simulado (fuera de este entregable docs-only).

---

## 8. Criterios de salida y desbloqueo

Este paso se considera **completo como diseño + bloqueo** cuando:

- [x] Auditoría de identidad/roles/tenant/sesiones documentada con evidencia (§2).
- [x] Diseño server-side actor×acción×recurso×farmacia, deny-by-default (§3).
- [x] Tenant scope especificado en capa de repositorio (§4) + hallazgo F-1 registrado.
- [x] Esquema de auditoría de mutaciones Staff (§5).
- [x] Controles anti-IDOR/tenant/spoof/replay (§6).
- [x] Pruebas negativas diseñadas; N1 verificable, N2-N4 BLOCKED con justificación (§7).
- [x] Bloqueo formal en [`ADR-0008`](ADR/ADR-0008-identidad-staff-bloqueada.md).

**Para DESBLOQUEAR (habilita el Paso 11):** el propietario debe aprobar un **modelo de
identidad verificable de empleado** (canal de autenticación, emisión/expiración de
sesión, mapa empleado→farmacia→rol). Recién entonces se implementan §3-§5 y se
ejercitan N2-N4. Hasta eso: **fail-closed, todo Staff denegado.**
