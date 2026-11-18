# NEA — Arquitectura objetivo (TO-BE)

> **Paso 3 del plan de modernización.** Documento de **diseño**: define la
> arquitectura objetivo a partir del AS-IS y el mapa de dependencias aprobados.
> **No modifica runtime.** Cada afirmación sobre el estado actual apunta a
> `ruta:símbolo`. Cada supuesto va marcado **⚠ SUPUESTO**; cada decisión de
> producto no resuelta va marcada **🔸 PENDIENTE**.
>
> Fecha: 2026-11-10. Rama: `docs/pharmacy-domain-design`.
> Base: [`AS-IS.md`](AS-IS.md), [`DEPENDENCY-MAP.md`](DEPENDENCY-MAP.md),
> [`TOOL-INVENTORY.md`](TOOL-INVENTORY.md), [`FLOW-CATALOG.md`](FLOW-CATALOG.md),
> [`FLOW-ORDERS.md`](FLOW-ORDERS.md).
> Hermanos de este paso: [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md),
> [`USE-CASES.md`](USE-CASES.md), [`POLICY-MATRIX.md`](POLICY-MATRIX.md),
> [`MIGRATION-ROADMAP.md`](MIGRATION-ROADMAP.md), [`ADR/`](ADR/).

---

## 0. Resumen ejecutivo

NEA evoluciona hacia un **monolito modular farmacéutico** ([ADR-0001](ADR/ADR-0001-monolito-modular.md))
en el mismo proceso FastAPI/Python/Postgres, conservando CRM y LLM actuales. El
invariante rector ([ADR-0002](ADR/ADR-0002-llm-sin-autoridad.md)): **el LLM
interpreta y redacta; no autoriza, no inventa datos, no afirma que una transacción
ocurrió, no ejecuta operaciones privilegiadas.**

Se **conservan sin negociación** las protecciones medidas: F14 (extracción/búsqueda),
F15 (comprensión antes de buscar), F15.1 (autorización por término en la puerta
única `_autorizar_busqueda`), G1–G13, concurrencia/locks/coalescencia, idempotencia
y aislamiento por farmacia. El diseño **documenta y separa** estas responsabilidades,
**no las duplica**.

---

## 1. Capas objetivo (destino conceptual, no carpetas vacías)

Se adopta la estructura del PLAN §3.2 como **destino**; las carpetas se crean solo
cuando un caso de uso real se extrae con pruebas (PLAN §3.2, [ADR-0005](ADR/ADR-0005-puertos-sin-migracion.md)).

| Capa | Responsabilidad | De dónde sale hoy |
|---|---|---|
| `api/` | Adaptadores HTTP/webhook (entrada/salida) | `webhook.py`, parte de `main.py` |
| `core/orchestration/` | Ciclo de turno y conversación (coordina, no contiene reglas de dominio) | parte de `turn.py` (`run_turn`, `_tool_loop`, locks) |
| `core/identity/` | Farmacia/tenant, cliente, empleado | `turn.py:430-431` (`providerId`), `profile.py` |
| `core/policies/` | Autorización determinista (actor × acción × recurso × farmacia) | hoy repartido: `_autorizar_busqueda`, allowlist, X-API-Key, guards-condición |
| `core/contracts/` | Resultados tipados, errores, DTO internos | hoy dicts `{ok,...}` de `tools.py` |
| `application/use_cases/` | Casos de uso de farmacia independientes del canal | handlers de `tools.py` + helpers de `turn.py` |
| `application/ports/` | Interfaces que los casos de uso requieren | `Store` (`state.py`), `CrmClient`, `OpenAiLlm` |
| `domain/pharmacy/` | catálogo, precios, inventario, carrito, pedidos, seguimiento | `relevancia.py`, `aliases.py`, lógica de carrito/pedido de `tools.py`/`turn.py` |
| `domain/clinical/` | derivación clínica | **nuevo** (fail-closed, [ADR-0006](ADR/ADR-0006-handoff-clinico-fail-closed.md)) |
| `infrastructure/` | Postgres, CRM, WhatsApp, LLM, media, relay, observabilidad | `db.py`, `crm.py`, `llm.py`, `media.py`, `relay.py`, `sender.py`, `observability.py` |
| `safety/intent/` | Comprensión F15 | `intencion.py` |
| `safety/response_guards/` | Vetos G1–G13 (última línea, no autorizador) | `guards.py` + condición en `turn.py` |
| `safety/audit/` | Auditoría por operación | `intent_decisions`, `med_queries`, `observability.py` |

> **Nota de altitud:** el orquestador **coordina**; las reglas de catálogo/pedido
> viven en `domain/`, las de autorización en `core/policies/`. No se admite un nuevo
> "god orchestrator" (PLAN §5 paso 6).

---

## 2. Flujo de un turno (ruta canónica)

La ruta canónica del PLAN §1, mapeada a las separaciones del prompt (punto 9):

```
1. Recibir y normalizar            →  api/ (webhook, extract_inbound, dedup, coalesce)
2. Resolver identidad/farmacia/ctx →  core/identity/ (providerId, aiEnabled, windowOpen)
3. Comprender intención (F15)      →  safety/intent/ (intencion.entender)   ── COMPRENSIÓN
4. Evaluar política/autorización   →  core/policies/ (+ _autorizar_busqueda) ── AUTORIZACIÓN
5. Ejecutar caso de uso permitido  →  application/use_cases/                 ── EJECUCIÓN
6. Validar resultado de dominio    →  domain/ (evidencia) + safety/guards     ── VALIDACIÓN
7. Registrar (auditoría)           →  safety/audit/
8. Redactar fiel a la evidencia    →  compositor de respuesta                 ── REDACCIÓN
9. Enviar y registrar entrega      →  infrastructure/ (_send, pending_send)
```

Las **cinco separaciones** que exige el prompt (punto 9) son estas fronteras:
**comprensión (3)**, **autorización (4)**, **ejecución de caso de uso (5)**,
**validación (6)** y **redacción (8)**. Hoy están entrelazadas dentro de
`run_turn`/`_tool_loop`/`tools.execute`; el TO-BE las hace explícitas sin cambiar
el orden ni el comportamiento observable.

Diagrama completo del turno: PLAN §3.1 (se conserva como referencia). Secuencia
AS-IS: [`AS-IS.md`](AS-IS.md) §1.2.

---

## 3. Actores, límites y derivación clínica

Detalle y modelo en [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §1–§2. Resumen:

- **Cliente (Customer)** — identidad WhatsApp. Es el actor **real y activo** hoy
  (todas las capacidades de farmacia). Evidencia: `tools.FARMACIA_TOOLS`,
  `turn._resolver_paso_entrega`.
- **Staff** — hoy existe **solo** como consumidor de analytics vía `X-API-Key`
  (`webhook.analytics_top_meds:384`, `analytics_chronic_patients:426`). **No** hay
  identidad de empleado individual ni rol por persona.
- **Farmacéutico / Administrador / Empleado** — **⚠ BRECHA:** no existen como roles
  diferenciados en el código. El prompt pide no inventarlos; se registran como
  **🔸 PENDIENTE** (PLAN §8 pregunta 3: ¿qué identificador/autenticación representa
  a un empleado?). El diseño los deja como actores **futuros** del Staff Agent
  (Paso 9/11), no activos.
- **Farmacia / tenant** — `providerId` del contexto CRM (`turn.py:430-431`);
  aislamiento por `provider_id` en `med_queries`/`patient_profiles` y por
  `conversation_id` en el carrito. **No se confía en `pharmacy_id` del LLM** (PLAN §7).
- **Sistema** — workers deterministas (relay/followup/sender), backstops y guardas.

**Derivación clínica:** fail-closed sobre la puerta de handoff existente
([ADR-0006](ADR/ADR-0006-handoff-clinico-fail-closed.md)). Taxonomía clínica,
responsable y estados de derivación = **🔸 PENDIENTE** (decisión de producto, PLAN §8 q6).

---

## 4. Contratos tipados

Detalle en [`USE-CASES.md`](USE-CASES.md) §2 y [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §5.
Hoy cada handler devuelve `dict {ok, ...}` con `instrucciones` para el LLM
(`TOOL-INVENTORY.md` §6). El TO-BE define **resultados tipados** comunes:

| Resultado tipado | Semántica | Origen AS-IS |
|---|---|---|
| `Encontrado` | ≥1 producto relevante | lista de `_buscar_medicamento` |
| `NoEncontrado` | 0 resultados tras typo/principio activo | `med_not_found` (`turn.py:708-712`) |
| `Ambiguo` | varias opciones / forma no resuelta | `last_options`, `aviso_forma` |
| `NoAutorizado` | la puerta F15.1 denegó el término | `_rechazo_comprension:2894` |
| `Parcial` | datos incompletos (p. ej. falta Bs) | omisión de Bs (`ver_carrito`, `tools.py:4593-4620`) |
| `ProveedorNoDisponible` | CRM/LLM caído o timeout | `LlmExhausted`, `CrmError`, fail-safe de intención |
| `RequiereConfirmacion` | falta confirmación explícita del cliente | resumen antes de `finalizar_pedido` |
| `Derivado` | handoff a humano | `handoff_reason` + `_safe_handoff` |

El **contrato de intención** (F15) se formaliza desde `Entendimiento`
(`intencion.py:166-179`): `acto`, `hay_consulta`, `termino`, `confianza`, `motivo`,
`fuente`. Se **amplía** conceptualmente con `entidades` y `evidencia` (hoy
implícitas) — ver [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §5.1. **⚠ SUPUESTO:** la
ampliación es aditiva; no cambia `ACTOS` ni el umbral `CONFIANZA_MINIMA=0.35`.

---

## 5. Carrito / pedido e invariantes transaccionales

Máquina de estados y reglas en [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §3–§4, basadas
en el CRM/persistencia reales ([`FLOW-ORDERS.md`](FLOW-ORDERS.md)). Puntos clave:

- **Separar `preparar pedido` de `confirmar pedido`** ([ADR-0007](ADR/ADR-0007-fuente-verdad-pedido.md)).
- **Idempotencia:** a nivel de **carrito** ya existe (upsert `ON CONFLICT` que suma
  en `db.cart_add`, dedupe intra-turno `backstop_added_skus` e inter-turno por
  cantidad) y de **webhook** (`processed_message`). A nivel de **pedido** se define
  un `order_attempt_id` por **intento lógico de confirmación**: los reintentos del
  mismo intento (timeout, doble webhook, reenvío) **reutilizan** ese identificador,
  y una confirmación intencional nueva puede generar uno nuevo aunque el carrito sea
  idéntico. La **coordinación atómica ante concurrencia** (reconocer/reutilizar el
  intento lógico con solicitudes concurrentes) **sigue PENDIENTE del Paso 7**; no se
  afirma idempotencia de pedido completamente resuelta
  ([ADR-0007](ADR/ADR-0007-fuente-verdad-pedido.md), [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §7).
- **Concurrencia:** `conversation_lock` por identidad es el único mecanismo; no hay
  `SELECT … FOR UPDATE` sobre `bot_cart`.
- **Expiración:** `CART_SESSION_HOURS=12h` (carrito), `FOLLOWUP_MAX_AGE_HOURS=48h`
  (seguimiento).
- **Recuperación:** `pending_send` (nunca se descarta respuesta), reintentos del
  `SenderWorker`, re-enganche del `FollowupWorker`.
- **🔸 PENDIENTE:** estados de pedido del CRM y fuente de verdad de precio/stock
  (PLAN §8 q1/q2/q8).

---

## 6. Separación de responsabilidades (comprensión / autorización / ejecución / validación / redacción)

| Separación | Componente TO-BE | Hoy | Invariante |
|---|---|---|---|
| **Comprensión** | `safety/intent/` | `intencion.entender` (F15) | decide si hay consulta y el término, **antes** de buscar; fail-safe `hay_consulta=None` |
| **Autorización** | `core/policies/` (acceso general) **+** `_autorizar_busqueda` (búsqueda por término) | repartido | determinista; fail-closed. Ver desglose de los dos planos abajo |
| **Ejecución** | `application/use_cases/` | handlers `tools.py` | solo casos de uso permitidos; nunca efectos a partir de texto del LLM sin autorización |
| **Validación** | `domain/` + `safety/response_guards/` | backstops + G1–G13 | evidencia por afirmación; G13 = post-condición de acción |
| **Redacción** | compositor de respuesta | prompt + `wtext` + backstops | fiel a la evidencia; el LLM redacta, no decide |

La **autorización** tiene **dos planos que no se confunden**:

1. **Política general de acceso** (`core/policies/`, futuro Policy Engine): actor ×
   acción × recurso × farmacia × canal. Decide si un actor puede *intentar* una
   acción sobre un recurso de una farmacia. Consolida allowlist, X-API-Key y
   *tenant scope*.
2. **Autorización específica de búsqueda de catálogo** (`_autorizar_busqueda`,
   F15/F15.1): decide, **por término**, si una búsqueda puede tocar el catálogo.

El Policy Engine **delega** el plano 2 en `_autorizar_busqueda`; **nunca lo duplica
ni lo sustituye** ([ADR-0003](ADR/ADR-0003-puerta-unica-autorizacion.md)). "Puedo
buscar catálogo" (plano 1) no implica "este término está autorizado" (plano 2):
cada término sigue pasando por la puerta única. Las guardas **no autorizan** en
ninguno de los dos planos ([ADR-0004](ADR/ADR-0004-guards-ultima-linea.md)).

---

## 7. Mapeo F14 / F15 / F15.1 ↔ guardas ↔ componentes futuros

**No se crea una segunda puerta** (prompt punto 11, PLAN §2.3,
[ADR-0003](ADR/ADR-0003-puerta-unica-autorizacion.md)).

| Protección actual | Símbolo AS-IS | Componente TO-BE | Relación |
|---|---|---|---|
| **F14** extracción/búsqueda, alias, typos, opción N, multi-med | `tools._buscar_medicamento:2957`, `relevancia.*`, `aliases.reescribir` | `domain/pharmacy/catalog/` | **mover** algoritmos sin cambiarlos (Paso 5), paridad por fixtures |
| **F15** comprensión | `intencion.entender:394` | `safety/intent/` | **mantener** como clasificador; formalizar contrato (aditivo) |
| **F15.1** autorización por término | `tools._autorizar_busqueda:2768` (`candidato_obligatorio=True`) | `core/policies/` **delega** aquí | **puerta única** (plano 2); el Policy Engine la invoca/delega, **nunca la duplica ni la sustituye** |
| **G1–G12** vetos de salida | `guards.py` + `turn.py:950-1098` | `safety/response_guards/` | **mantener** como fail-safe de redacción |
| **G13** acción no ocurrida | `guards.familia_accion_afirmada:622` + `turn._g13_accion_ocurrio:254` | `safety/response_guards/` + señales de caso de uso | **validación de post-condición** |
| Hostilidad / stall | `hostility.py`, `stall.py` | `safety/` transversal | conteo determinista entre turnos, fuera del LLM |
| Allowlist / X-API-Key / tester | `turn.py:288`, `webhook.py:195`, `turn.py:300` | `core/policies/` + `api/` | autorización de **acceso general** (plano 1); se consolida como política, distinta de la autorización de búsqueda por término |

---

## 8. Decisiones pendientes (no diseñar contra supuestos)

Traídas del PLAN §8; ninguna se resuelve en este paso (prompt: marcar cada decisión
pendiente):

1. 🔸 ¿El CRM ofrece APIs separadas de catálogo/precio/inventario/carrito/pedidos?
2. 🔸 ¿Existe pedido persistido en el CRM o solo carrito en NEA? (hoy: nota de ficha)
3. 🔸 Identificador/autenticación de empleado (bloquea Staff real, Paso 9/11).
4. 🔸 ¿Una farmacia por instancia o multi-tenant real? (hoy: por `providerId`).
5. 🔸 Calidad/disponibilidad de datos de composición/principio activo.
6. 🔸 Proceso humano real de derivación clínica (bloquea Paso 8).
7. 🔸 Qué flujos de agenda heredados siguen siendo necesarios.
8. 🔸 Sistema fuente de verdad de precios/disponibilidad.
9. 🔸 Qué acciones pueden reintentarse con seguridad (idempotencia de efectos).
10. 🔸 Retención de datos según jurisdicción/acuerdos.

Decisiones de arquitectura tomadas: [ADR-0001](ADR/ADR-0001-monolito-modular.md) …
[ADR-0007](ADR/ADR-0007-fuente-verdad-pedido.md).

---

## 9. Plan de transición (resumen)

Conserva el monolito; extracción incremental con paridad medida. Detalle y criterios
de salida por paso en [`MIGRATION-ROADMAP.md`](MIGRATION-ROADMAP.md). El siguiente
paso (Paso 4) **requiere aprobación explícita** antes de empezar (prompt y PLAN §9).
