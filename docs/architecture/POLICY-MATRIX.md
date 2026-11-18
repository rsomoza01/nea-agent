# NEA — Matriz de políticas (TO-BE)

> **Paso 3.** Política determinista como **actor × acción × recurso × farmacia ×
> precondición × auditoría**. Formaliza, sin duplicar, lo que hoy decide
> `_autorizar_busqueda` (F15.1), la allowlist, la X-API-Key y la condición de las
> guardas. Acompaña a [`TO-BE.md`](TO-BE.md) y [`USE-CASES.md`](USE-CASES.md).
> **No modifica runtime.** **🔸 PENDIENTE** = decisión de producto.

---

## 1. Principios de la política

1. **Determinista y en backend.** La política decide; el LLM no concede permisos
   ([ADR-0002](ADR/ADR-0002-llm-sin-autoridad.md)).
2. **Dos planos de decisión, no uno.** Se distinguen:
   - **Política general de acceso** (actor × acción × recurso × farmacia × canal):
     quién puede intentar qué sobre qué recurso de qué farmacia. La decide el
     Policy Engine.
   - **Autorización específica de búsqueda de catálogo** (por **término**): la
     decide **exclusivamente** `tools._autorizar_busqueda:2768` (F15/F15.1).
   Son capas complementarias: la política general puede admitir "este actor puede
   buscar catálogo", pero **cada término** sigue pasando por la puerta única.
3. **Puerta única para búsqueda de catálogo.** El Policy Engine **delega** la
   autorización por término en `_autorizar_busqueda`; **nunca la duplica ni la
   sustituye** y **no crea una segunda puerta**
   ([ADR-0003](ADR/ADR-0003-puerta-unica-autorizacion.md)).
4. **Fail-closed.** Si la comprensión falla, se deniega (`_rechazo_comprension`).
5. **Tenant validado por backend.** El `providerId` proviene **siempre** del
   contexto CRM resuelto en backend, **nunca** del LLM ni del texto del mensaje
   (PLAN §7). Toda consulta y mutación aplica *tenant scope* por `providerId`
   (catálogo, analytics, perfiles) o por `conversation_id` (carrito, atado a un
   tenant); **no se cruza data entre farmacias**. El parámetro `providerId` de las
   rutas de analytics se valida contra el contexto de la integración, no se acepta
   a ciegas desde el cliente.
6. **Mínimo privilegio.** Un cliente no invoca acciones Staff. Los roles
   individuales (empleado, farmacéutico, administrador) **no existen todavía**
   (ver §2); cuando existan, operarán bajo mínimo privilegio por farmacia/rol
   (PLAN §9).
7. **Las guardas no autorizan** ([ADR-0004](ADR/ADR-0004-guards-ultima-linea.md)).

---

## 2. Matriz principal (estado actual mapeado)

Columnas: **Actor** · **Acción** · **Recurso** · **Alcance de farmacia** ·
**Precondición / autorización** · **Auditoría**.

| Actor | Acción | Recurso | Farmacia | Precondición / autorización (AS-IS) | Auditoría |
|---|---|---|---|---|---|
| Cliente | Buscar catálogo (UC-01) | Catálogo CRM | `providerId` del contexto | `aiEnabled`+`windowOpen`+allowlist; **F15.1** `_autorizar_busqueda` por término; fail-closed | `intent_decisions`, `med_queries` |
| Cliente | Ver detalle/precio (UC-02) | Catálogo (lectura) | `providerId` | producto ya mostrado; precio solo del catálogo | — (lectura) |
| Cliente | Ver disponibilidad (UC-03) | Catálogo (lectura) | `providerId` | producto ya mostrado | — |
| Cliente | Agregar/actualizar carrito (UC-04) | `bot_cart` | por `conversation_id` | opción/SKU resuelto contra `last_options`; precio del catálogo | `bot_cart`, `bot_conversation` |
| Cliente | Preparar pedido / resumen (UC-05) | `bot_cart`+entrega | `conversation_id` | carrito no vacío; **no sale sin método de entrega** | `bot_conversation` (`delivery_*`) |
| Cliente | Confirmar pedido (UC-06) | ficha CRM + `bot_cart` | `providerId`+`conversation_id` | carrito no vacío + **confirmación explícita** | nota de ficha CRM |
| Cliente | Pedir humano (UC-08) | handoff CRM | `providerId` | determinista (G9 / `_pide_hablar_con_humano`) | `handoff_reason` |
| Cliente | Consulta clínica (UC-08) | handoff CRM | `providerId` | **fail-closed**: derivar, no aconsejar | motivo mínimo (🔸) |
| Sistema | Seguimiento (UC-07) | conversación | `conversation_id` | pedido abierto + franja/días + IA activa | `bot_conversation` (`followup_*`) |
| Sistema | Relay / Sender | CRM | — | best-effort, nunca descarta | `relay_queue`, `pending_send` |
| Integración (API key) | Leer analytics top-meds | `med_queries` | `providerId` (query) | `X-API-Key == CRM_BOT_API_KEY` | — |
| Integración (API key) | Leer pacientes crónicos | `patient_profiles` | `providerId` | `X-API-Key` | — |
| Integración (API key) | Marcar consent crónico | `patient_profiles` | `providerId` | **🔸 sin endpoint hoy** (`marcar_consent:831`) | PENDIENTE |
| Empleado/Farmacéutico/Admin (individual) | Operaciones internas (`/staff/*`, piloto cerrado) | analytics, pedido, perfiles crónicos | `pharmacy_id` de la **sesión** | ✅ **IMPLEMENTADO** (Paso 11/ADR-0010): sesión Bearer + rol (clerk⊂pharmacist⊂admin); deny-by-default; mutación exige confirmación | `staff_audit` (append-only) |
| LLM | — | — | — | **ninguna**: no autoriza, no ejecuta, no afirma hechos | — |

> **La `X-API-Key` identifica una *integración* (el CRM/dashboard que llama), no a
> un empleado individual.** `CRM_BOT_API_KEY` es un secreto compartido por canal,
> sin usuario, rol ni actor por persona. Por eso las filas de analytics se
> atribuyen al actor **"Integración (API key)"** y **no** a un "Staff" con
> permisos individuales: ese actor **no existe todavía** (ver [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §1).
> No se deben inferir permisos individuales a partir de la posesión de la API key.

---

## 3. Controles de acceso / canal (hoy)

| Control | Símbolo | Aplica a |
|---|---|---|
| Firma Meta `x-hub-signature-256` | `webhook.verify_signature:35-42` | `POST /webhook` (acepta sin secret en dev) |
| `X-API-Key == CRM_BOT_API_KEY` | `webhook.py:195-197,395-397,436-439` | `/chat`, `/analytics/*` |
| Allowlist `ALLOWED_WA_IDS` | `turn.py:288-293` | `run_turn` (se salta en lab) |
| Tester-only `/reset` | `turn.py:300-304` | `TESTER_WA_IDS` |

**⚠ Riesgo registrado (AS-IS §7.4):** dos rutas de ingesta con contratos distintos
(`/webhook` firmado vs. `/chat` por API-key) para el mismo `handle_flush`. El
Policy Engine debe tratar ambas con la misma política de actor/recurso.

---

## 4. Decisión de autorización como resultado tipado

El Policy Engine devuelve (sin reemplazar la puerta de catálogo):

- `Autorizado(accion, actor, recurso, farmacia)`
- `NoAutorizado(motivo)` — p. ej. fuera de allowlist, acción Staff pedida por Cliente.
- `RequiereAclaracion` — intención no concluyente (`hay_consulta=None`): **preguntar,
  no actuar**.
- Para búsqueda de catálogo: **delega** y propaga el veredicto de
  `_autorizar_busqueda` (`autorizado` / `rechazo_comprension`).

---

## 5. Pruebas de política (criterio de salida — para Pasos 5/9)

- **Positivas:** cada fila "Cliente" autoriza su acción con precondición cumplida.
- **Negativas (cross-tenant / cross-rol):**
  - Cliente **no** puede invocar acciones Staff/analytics.
  - Petición con `providerId` A no ve datos de `providerId` B.
  - Término no autorizado por F15.1 **no** llega al catálogo (paridad con
    `test_f15_intencion.py` A9).
  - `/chat` sin `X-API-Key` válida se rechaza.
- **Fail-closed:** comprensión caída → denegar, no buscar.

**✅ IMPLEMENTADO (Paso 11, 2026-11-14):** el propietario aprobó el modelo de identidad
de empleado y se implementó ([`ADR-0010`](ADR/ADR-0010-identidad-staff-sesion.md)): las
negativas de acceso cruzado Staff/empleado (tenant A→B, rol insuficiente, identidad
expirada/revocada) son ahora **ejercitables y probadas** (`tests/test_staff_agent.py`),
además de "cliente no alcanza superficie Staff". El hallazgo **F-1** quedó **mitigado**
(binding `provider_id == settings.provider_id` en mono-tenant + tenant desde la sesión en
`/staff/*`). F-2 (firma Meta en producción) sigue abierto. Ver
[`SECURITY-ACTOR-POLICIES.md`](SECURITY-ACTOR-POLICIES.md) §6-§7.
