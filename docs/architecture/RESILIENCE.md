# NEA — Fronteras de infraestructura, resiliencia y observabilidad (Paso 10)

> **Modo:** robustez incremental. Formaliza los contratos de los adaptadores
> (PostgreSQL, CRM, WhatsApp vía CRM, LLM, media, workers) **sin introducir
> complejidad ni dependencias injustificadas**. Acompaña a
> [`ADR-0009`](ADR/ADR-0009-fronteras-infra-resiliencia.md) y
> [`DEPENDENCY-MAP.md`](DEPENDENCY-MAP.md). Cada afirmación apunta a `ruta:línea`.
>
> **Alcance ejecutado:** docs (este archivo + ADR) + **seam no-op** de código
> (`app/metrics.py`, `CrmUnauthorized`). El cableado de métricas/logs en cada
> adaptador es **incremental y opcional** (ver R-10.2).

---

## 1. Contrato de timeout por dependencia (puntos 1, 8)

| Dependencia | Adaptador | Timeout | Evidencia | Al expirar |
|---|---|---|---|---|
| **CRM** (genérico) | `CrmClient` | **15 s** por defecto | `crm.py:114-121` | `CrmError` (fallo de proveedor) |
| CRM typing/paused/final | `CrmClient.post_typing*` | **8 s** | `crm.py:275,285,291` | `CrmError`; best-effort, sin reintento |
| CRM media (descarga binaria) | `CrmClient.get_media` | **60 s** (adjuntos pesan) | `crm.py:353-355` | `CrmError` → media degrada honesto |
| **PostgreSQL** | `PgStore` (asyncpg) | pool **min 1 / max 5**; sin statement_timeout explícito | `db.py:94` | excepción asyncpg → sube al turno |
| **LLM** chat/visión/transcripción | `OpenAiLlm` | del cliente OpenAI (SDK) | `llm.py:75-98` | reintenta; agota → `LlmExhausted` |
| **Relay worker** | `RelayWorker` | **20 s** por POST | `relay.py:36` | red → reintento con backoff |
| **Sender worker** | `SenderWorker` | hereda el del `CrmClient` | `sender.py:60` | `CrmError` → reprograma |

**🔸 Pendiente de medición (no se cambia aquí):** PostgreSQL no fija
`command_timeout` en el pool; una query colgada podría retener una conexión del pool
(max 5). No hay caso medido; se registra para vigilar con la métrica de latencia PG.

---

## 2. Taxonomía de error por dependencia (punto 7)

**Se distingue fallo de proveedor de "no encontrado" y de "no autorizado".** Vocabulario
compartido con las etiquetas de métricas (`app/metrics.py`): `provider_failure` ·
`not_found` · `not_authorized` · `conflict` · `timeout` · `malformed`.

### CRM (`crm.py`)
| Situación | Tipo | Código | Nota |
|---|---|---|---|
| 5xx / red / status inesperado | `CrmError` | `provider_failure` | `_request:123-131`, no-200 por método |
| 404 | **no lanza** → `None` | `not_found` | `get_context:144`, `get_profile:166` |
| **401 / 403** | **`CrmUnauthorized`** (nuevo) | `not_authorized` | `_request:132-135` — centralizado; subclase de `CrmError` (retrocompatible) |
| 409 | `CrmConflict` / `SlotTaken` | `conflict` | códigos `ai_paused`/`window_closed`/`slot_taken` (`crm.py:56-77`) |

### LLM (`llm.py`)
| Situación | Resultado | Nota |
|---|---|---|
| content vacío sin tool_calls | reintento | `complete:416` |
| excepción (red/API/parseo) | reintento; proveedor rechaza extras → se desactivan y reintenta | `complete:418-421`, `_quiza_por_los_extras:373` |
| agota reintentos | `LlmExhausted` → turno degrada en silencio + handoff `error` | `complete:424` (Constitución IV) |
| `arguments` de tool malformados | `{}` con aviso (no silencioso) | `_parse_tool_arguments:489-525` → `malformed` |
| OCR devuelve el prompt parafraseado | descarta + reintenta; vacío → `LlmExhausted` | `ocr_image:347-360` |

---

## 3. Reintentos, backoff, idempotencia (puntos 2, 3)

| Componente | Reintento | Backoff | Idempotencia | ¿Mutación ciega? |
|---|---|---|---|---|
| **LLM** | 2 (+ inicial) | `2**attempt` → 1 s, 2 s (`llm.py:423`) | lecturas puras | No (idempotente) |
| **Relay** | hasta 24 h | `2**attempts` cap **900 s** (`relay.py:63`) | **consumidor CRM idempotente** (dedup firma Meta; `relay.py:3-6`) | No (seguro) |
| **Sender** | hasta 24 h | `30·2**attempts` cap **900 s** (`sender.py:72`) | — (`send_message` **no** idempotente) | ⚠ **R-10.1**: reintenta en `CrmError` de red → *at-least-once* |
| **CRM 409** | **no** reintenta | — | — | Correcto: `ai_paused`/`window_closed` → abandona (`sender.py:61-69`) |
| **DB** | — | — | gates `ON CONFLICT DO NOTHING` (`processed_message`, `order_attempt`, `clinical_handoff`) | No |

**Circuit breaker:** **ausente a propósito** (punto 3). No hay caso medido; los
reintentos acotados + backoff + degradación ya cubren el fallo observado. Se
reconsiderará con la métrica `dep.errors`/`dep.calls` (error-rate) real.

**R-10.1 (deuda):** el sender re-envía ante error de red aunque el mensaje quizá sí se
entregó (ACK perdido). Es el trade-off clásico *at-least-once* para no perder la
respuesta (incidente 2026-08-03: antes se descartaba en silencio). Mitigación futura:
clave de idempotencia por envío si el CRM la soporta.

---

## 4. Correlación en logs y trazas (punto 4)

- **Hoy:** cada turno abre una traza Langfuse con la identidad; el LLM registra su
  generación en la traza activa (`llm._record_generation:426`, contextvar
  `observability._current_trace`). Es **no-op** sin Langfuse.
- **Gap R-10.2:** los logs **planos** de los adaptadores (`nea.crm`, `nea.llm`,
  `nea.sender`, `nea.relay`) **no** incluyen `correlation_id`/`turn_id`/`pharmacy_id`
  de forma consistente.
- **Diseño del patrón (cuando se cablee):** un `contextvars.ContextVar` por turno con
  `{turn_id, correlation_id, pharmacy_id}` + un `logging.Filter` que lo inyecte en cada
  record. `turn_id`/`correlation_id` ya existen conceptualmente (p. ej.
  `wa_message_id`, `clinical_handoff_id`); `pharmacy_id` = `provider_id` del contexto.
  Cablearlo es **hardening ampliado** (toca runtime): requiere aprobación.

---

## 5. Métricas útiles (punto 6) — catálogo y seam

Implementadas como **fachada no-op** en `app/metrics.py` (patrón de `observability.py`):
sin `METRICS_ENABLED` no cuestan nada; con el flag, emiten `METRIC nombre=valor
etiqueta=...` a `nea.metrics`. Un backend real requiere ADR aparte (ADR-0009 §5).

| Métrica (constante) | Qué mide | Etiquetas |
|---|---|---|
| `DEP_LATENCY_MS` | latencia por dependencia | `dependency`, `ok` |
| `DEP_CALLS` | llamadas (base del **error rate** = errores/llamadas) | `dependency`, `ok` |
| `DEP_ERRORS` | errores por taxonomía | `dependency`, `kind` |
| `TURN_NO_REPLY` | **turnos sin respuesta** al cliente | — |
| `SEARCH_AUTHZ` | **autorización de búsqueda** (puerta F15.1) | `decision=authorized\|rejected` |
| `RESULT_PARTIAL` | **resultado parcial**/degradado | `dependency` |
| `HANDOFF` | handoffs | `reason` (catálogo canónico) |
| `ORDER` | pedidos | `outcome` |

Uso previsto (cuando se cablee, incremental):

```python
from app import metrics
async with metrics.measure_async(metrics.DEP_CRM):      # latencia + llamada
    await crm.get_context(...)
metrics.error(metrics.DEP_CRM, metrics.ERR_NOT_AUTHORIZED)  # en except CrmUnauthorized
metrics.record(metrics.SEARCH_AUTHZ, decision="rejected")   # en _rechazo_comprension
```

---

## 6. Redacción de logs — no registrar lo innecesario (punto 5)

| Dato sensible | ¿Se loguea hoy? | Regla |
|---|---|---|
| `X-API-Key` / `CRM_BOT_API_KEY` | No | **Nunca** loguear la key ni el header `X-API-Key` |
| Tokens (`verify_token`, Meta) | No | Nunca |
| Payload crudo completo de Meta | **Solo** con `CAPTURE_PAYLOADS=true` (dev; `webhook.py:322-325`) | **R-10.3**: default `false`; apagar en producción |
| Texto clínico del cliente | No (solo categoría) | Coherente con [ADR-0006](ADR/ADR-0006-handoff-clinico-fail-closed.md) / PLAN §7 p5 |
| PII (teléfono, nombre) | Identidad en algunos logs/trazas | Minimizar; no añadir PII nueva a logs |
| Métricas | — | El sink emite **nombres+valores+etiquetas acotadas**, nunca texto libre del cliente |

La fachada de métricas **no** acepta texto libre del cliente como etiqueta (solo
enums/dimensiones acotadas): evita fuga de PII por la vía de telemetría.

---

## 7. Modo degradado y recuperación (punto 8)

| Fallo | Degradación | Recuperación |
|---|---|---|
| **LLM agotado** (`LlmExhausted`) | turno calla + handoff `error` (humano ve la conversación) | siguiente turno reintenta limpio |
| **CRM caído en el turno** | el envío va a `pending_send`; el turno no se cae | `SenderWorker` reintuza con backoff hasta 24 h; luego abandona + handoff `error` |
| **Relay (webhook→CRM) falla** | cola persistente en PG | `RelayWorker` reintenta hasta 24 h; luego abandona (relay ≠ respuesta al cliente) |
| **Perfil del negocio no disponible** | `ProfileProvider` degrada CRM→brief→mínimo (TTL 300 s) | reintenta al expirar TTL (`profile.py`) |
| **OCR/transcripción falla** | media degrada con honestidad (no inventa) | el cliente puede reenviar |
| **Observabilidad/métricas caídas** | no-op; **jamás** rompen el turno | — |

---

## 8. Pruebas (punto 9) — estado

| Caso | Dónde | Estado |
|---|---|---|
| **No autorizado vs no encontrado vs fallo** (CRM) | `tests/test_crm_errors.py` (MockTransport) | ✅ nuevo (5) |
| **Respuesta malformada** (tool args) | `tests/test_tool_loop_respaldo.py`, `llm._parse_tool_arguments` | ✅ existente |
| **Reintento LLM / provider hints** | `tests/test_llm_provider_hints.py` | ✅ existente |
| **Observabilidad/métricas no-op** | `tests/test_metrics_noop.py` | ✅ nuevo (7) |
| **Idempotencia** (dedup, order_attempt, clinical_handoff) | `tests/test_order_*`, `tests/test_clinical_handoff*` | ✅ existente |
| **Timeout real / conexión caída (socket)** | — | 🔸 diseñado; requiere simular red (fuera del seam no-op) |
| **Reintento del sender → doble envío (R-10.1)** | — | 🔸 diseñado; exige harness de ACK perdido |

**Suite:** 386 passed (374 previas + 12 nuevas), 0 regresiones.

---

## 9. Deuda registrada (para próximos pasos)

- **R-10.1** — Sender *at-least-once* en error de red (§3).
- **R-10.2** — Propagar `correlation_id/turn_id/pharmacy_id` en logs planos (§4). Cablear = hardening ampliado.
- **R-10.3** — `CAPTURE_PAYLOADS` vuelca payload completo; dev-only, apagar en prod (§6).
- **🔸 PG** — sin `command_timeout` en el pool (§1); vigilar con `dep.latency_ms`.
- **Circuit breaker** — aplazado hasta tener error-rate medido (§3, ADR-0009 §4).
