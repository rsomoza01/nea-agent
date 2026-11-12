# PR — Paso 7: Carrito y flujo transaccional de pedido (auditoría + porción no bloqueada)

> **Rama:** `feat/pharmacy-order-workflow` · **Base / HEAD inicial:** `cf2dde6`
> (`refactor/turn-orchestrator`) · **Fecha:** 2026-11-12 · **Modo:** implementación de
> dominio incremental; **parcialmente BLOCKED** por CRM sandbox no configurado y por
> decisiones del propietario ESTIMADAS (PLAN §8, preguntas 1/2/8).
>
> **Actualización 2026-11-12 (2ª iteración):** el propietario habilitó Docker y respondió
> 1/2/8 de forma estimada. Con ello se **desbloqueó la parte D de ADR-0007** (reserva
> atómica del intento), que se diseñó, implementó (puerto + referencia en memoria) y
> **validó contra Postgres desechable**. La migración queda como propuesta validada (no
> aplicada), por ser las decisiones no definitivas. Lo que sigue BLOCKED: el cableado al
> hot path y los casos que requieren **CRM sandbox** (timeout/revalidación/CRM caído).

## 0. Evaluación de las respuestas estimadas del propietario

| # | Respuesta | Evaluación del arquitecto | Resolución |
|---|---|---|---|
| 1 | CRM quizá no ofrece API de pedidos; NEA puede aportarla | Correcta; el CRM no la tiene (auditado §2) | Entidad de pedido **en NEA** |
| 2 | Si se construye en NEA, mantenerla en NEA | Correcta y consistente | Tabla `order_attempt` en NEA |
| 3 | Fuente de verdad = la DB disponible/real de producción | **Refinada:** la DB de NEA no tiene el catálogo; precio/stock = catálogo CRM (`get_products`), instantánea en `bot_cart`. Una tabla de precios en NEA sería 2ª fuente que deriva | Entidad **en NEA**; **precio/stock = catálogo**, revalidado al confirmar (ADR-0007 §Revisión P8) |
| 4 | Docker/DB desechable + CRM sandbox disponibles | Docker **confirmado UP**; CRM sandbox **no** configurado en esta sesión | DB validada; CRM-tests siguen BLOCKED |

## 1. Resumen ejecutivo

El Paso 7 pide un flujo **transaccional** de pedido (persistencia, idempotencia,
concurrencia, revalidación de precio/stock, recuperación/rollback) con pruebas de
integración obligatorias. La **auditoría previa** —que el propio prompt exige como primer
paso ("no supongas que existe una entidad de pedido persistida")— confirma dos bloqueos
que impiden ejecutar el núcleo del paso en esta sesión:

1. **No existe entidad de pedido persistida ni API de pedidos en el CRM.** La superficie
   del `CrmClient` (`app/crm.py`) no tiene ninguna operación de pedido:
   `get_context`, `get_profile`, `send_message`, `put_ficha`, `post_handoff`,
   `get_availability`/`create_booking`/`reschedule_booking` (**citas/agenda**, no
   pedidos), `get_products`, `get_providers`, `get_media`. "Finalizar pedido" es hoy una
   **nota de ficha best-effort** (`tools._finalizar_pedido:4723-4728`,
   `crm.put_ficha:179`). Diseñar la máquina de estados extendida del pedido
   (`confirmado/aceptado/preparado/entregado`) exige respuesta del propietario a las
   preguntas abiertas 1/2/8 del PLAN; hacerlo ahora sería **diseñar contra supuestos**,
   prohibido por ADR-0007 y por la regla no negociable 4.

2. **Entorno de pruebas no disponible.** Docker está caído y las credenciales CRM no
   están configuradas (`CRM_BASE_URL`/`CRM_BOT_API_KEY` sin definir), igual que en
   `BASELINE-TEST-MATRIX.md`. **Los 7 casos de integración obligatorios** (doble
   webhook, timeout antes/después del CRM, dos operaciones concurrentes, precio/stock
   cambió, CRM no disponible, recuperación/rollback) **requieren DB desechable + CRM
   sandbox** → **BLOCKED**. La regla no negociable 7 prohíbe migrar esquema sin prueba
   en DB desechable, que no puedo ejecutar.

Ante esto, se ejecuta **solo la porción explícitamente marcada como "diseñable y probable
sin tocar el CRM"** por [ADR-0007](ADR/ADR-0007-fuente-verdad-pedido.md) (decisiones
1-4, escenarios A/B/C): contratos tipados que separan **preparar** de **confirmar**,
el identificador de **intento lógico** (`order_attempt_id`) con su semántica de
reintento/unicidad, y el **fingerprint de carrito** como dato de contexto (no clave).
Es **aditivo y paralelo** (mismo patrón que UC-01 en el Paso 4): **no está cableado** en
`turn.py`/`tools.py` y **no cambia el comportamiento observable** del bot. El resto del
Paso 7 queda BLOCKED con una solicitud de decisión al propietario (§8).

## 2. Auditoría del modelo real de carrito/pedido (paso 1 del prompt)

| Aspecto | Realidad AS-IS | Evidencia |
|---|---|---|
| Carrito | Tabla `bot_cart` en Postgres; upsert atómico que **suma** cantidad | `migrations/004_bot_cart.sql`, `db.cart_add:371-397`, `FLOW-ORDERS §6` |
| Entidad de pedido | **No existe**; finalizar = nota de ficha best-effort | `tools._finalizar_pedido:4705-4728`, `crm.put_ficha:179` |
| API de pedido en CRM | **No existe** (solo ficha/catálogo/handoff/agenda) | `app/crm.py` (inventario de métodos, §1) |
| Confirmación | Existe implícita: `_es_confirmacion_resumen`, backstop a `finalizar_pedido` | `FLOW-ORDERS §5`, `turn.py:1967` |
| Idempotencia de pedido | **No existe** clave de pedido; sí dedup de webhook por mensaje | `db.mark_processed:99` (`INSERT … ON CONFLICT DO NOTHING RETURNING`) |
| Concurrencia | Solo `conversation_lock` serializa turnos; sin `SELECT … FOR UPDATE` en `bot_cart` | `turn.py:210-242`, `FLOW-ORDERS §6` |
| Revalidación precio/stock al confirmar | **No existe**; precio del catálogo al agregar, no se revalida al cerrar | `FLOW-ORDERS §2/§8` |
| Estado comunicado | Sostenido por G13 (no afirma acción no ocurrida) | `tests/test_g13_accion.py`, `ADR-0007` decisión 3 |

Conclusión de la auditoría: el "pedido" hoy es **carrito local + nota**. La parte
transaccional del Paso 7 depende de información que es **decisión del propietario**.

## 3. Alcance y archivos (porción no bloqueada)

| Archivo | Cambio | Rol |
|---|---|---|
| `app/application/order_workflow.py` | nuevo (+~290) | Contratos: `OrderPhase` + grafo de transiciones, `OrderConfirmStatus`/`OrderConfirmResult`, `order_attempt_id` (`nuevo_order_attempt_id`, `OrderAttempt`), `cart_fingerprint`; **puerto `OrderAttemptStore`** (reserva atómica, parte D) + referencia en memoria `EnMemoriaOrderAttemptStore` + `ConfirmarPedidoUseCase`. **Puro/puerto, sin I/O real, sin LLM, sin autorización.** |
| `tests/test_order_workflow_contracts.py` | nuevo (+~290) | 23 pruebas: unicidad/reintento de id, fingerprint ≠ clave, estabilidad del fingerprint, transiciones permitidas/prohibidas, confirmación obligatoria, limitación G13, **reserva atómica idempotente (A/B) incl. concurrente, ids distintos (C), caso de uso completo**. |
| `016_order_attempt.sql` (propuesta) | **NO aplicada a `migrations/`** | DDL validada contra Postgres desechable (§DB); se difiere a decisiones definitivas. |

**No** se tocó runtime (`turn.py`/`tools.py`/`db.py`/`crm.py`), **no** hay migración
aplicada, **no** hay dependencia nueva (solo `uuid`/`asyncio`/`dataclasses`/`enum` de la
stdlib), **no** se cableó el caso de uso. Diff de runtime observable: **0** (módulo
aditivo no importado por la ruta caliente).

## 3b. Validación contra Postgres desechable (DB) — regla no negociable 7

Con Docker UP se levantó `postgres:16-alpine` desechable y se aplicaron **las 15
migraciones existentes en orden** (todas OK) + la propuesta `016_order_attempt.sql`:

- Tabla creada con PK `order_attempt_id`, FK `conversation_id → bot_conversation(id)
  ON DELETE CASCADE`, índice `idx_order_attempt_conv`, `phase`/`created_at` con default.
- **Reserva atómica probada en SQL real:** 1er `INSERT … ON CONFLICT DO NOTHING
  RETURNING` → 1 fila; 2º con el **mismo** id y otra huella → **0 filas** (no duplica, no
  sobrescribe); `order_attempt` queda con **1 fila** y la huella de la **primera** reserva
  (`A:1`). Coincide exactamente con la semántica de `EnMemoriaOrderAttemptStore`, lo que
  valida la referencia en memoria como espec del futuro método `PgStore`.
- **Rollback:** `DROP TABLE order_attempt;` (sin datos productivos). Contenedor `--rm`.

## 4. Decisiones y justificación

1. **Ejecutar solo la parte "diseñable ya" de ADR-0007.** La propia ADR separa lo que se
   puede especificar/probar sin CRM (decisiones 1-4, A/B/C) de lo PENDIENTE (parte D:
   reserva atómica/concurrencia, y máquina de estados extendida). Se implementa lo
   primero; lo segundo se reporta BLOCKED, sin inventar.
2. **`order_attempt_id` por intento lógico, no por transporte** (escenario A). El helper
   genera UUID4 nuevo por intento; el reintento **reutiliza** el id (`es_reintento_de`).
   La persistencia-antes-de-llamar y la reserva atómica ante concurrencia (escenario
   B/parte D) quedan BLOCKED.
3. **Fingerprint de carrito = contexto, no identidad** (escenario C). Mismo carrito puede
   ser otro pedido; el test lo fija explícitamente. El fingerprint sirve para detectar
   cambios entre preparar y confirmar (futura revalidación).
4. **`PENDIENTE_FUENTE` + `LIMITACION_SIN_ENTIDAD_PEDIDO`** (decisión 3). El contrato
   expone que NEA no puede afirmar aceptación por la farmacia mientras no haya entidad de
   pedido en la fuente — alineado con G13, sin debilitarlo.
5. **Aditivo y no cableado** (precedente Paso 4). Cablear = cambiar comportamiento y
   exige la arquitectura transaccional completa (bloqueada) + sus pruebas. No se cablea.

## 5. Pruebas (comandos y resultados)

Intérprete: `.venv-test/Scripts/python.exe` (Python 3.11.16). `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.

| Prueba | Comando | Resultado |
|---|---|---|
| Compilación | `python -m py_compile app/application/order_workflow.py` | **PASS** |
| **Contrato pedido** (23) | `pytest tests/test_order_workflow_contracts.py` | **23 passed** |
| DB desechable (migraciones + reserva atómica) | `docker run postgres:16-alpine` + psql (§3b) | **PASS** |
| Suite completa | `pytest tests/ -q` | **327 passed** (304 baseline + 23 nuevos) |
| Sonda P1 | `PYTHONPATH=. python tests/harness_agente_p1.py` | **exit 0** (paso) |
| Sonda P2 | `PYTHONPATH=. python tests/harness_agente_p2.py` | **exit 0** (paso) |
| Sonda P3 | `PYTHONPATH=. python tests/harness_agente_p3.py` | **exit 0** (paso) |
| F15.1 por término | `python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` | **10 pruebas, 0 huecos, exit 0** |
| Guards G5 / G13 | `pytest tests/test_g5_evidencia.py tests/test_g13_accion.py` | **44 passed** |
| Guard G6 (script) | `python tests/test_g6_lista_mostrada.py` | **15/15 OK, exit 0** |

### Cobertura nueva
El flujo de pedido no tenía pruebas unitarias directas (hueco mayor registrado en
`FLOW-ORDERS §9`). Estas 15 pruebas cubren, de forma determinista y sin infraestructura,
la semántica de idempotencia de intento (A/B/C), el fingerprint y las transiciones de
fase — la base para la parte transaccional cuando se desbloquee.

## 6. Pruebas obligatorias NO ejecutadas y motivo (BLOCKED)

De los 7 casos obligatorios, la **lógica** de confirmación/idempotencia/concurrencia ya
está cubierta por los tests de contrato + la validación en DB; lo que sigue BLOCKED es su
ejercicio **end-to-end por el hot path** (requiere cableado, no hecho por ser decisiones
estimadas) y lo que depende de **CRM sandbox** (no configurado en esta sesión):

| Caso obligatorio | Estado | Detalle |
|---|---|---|
| Confirmación explícita y rechazo | **PARCIAL** | lógica cubierta (`ConfirmarPedidoUseCase`, tests); e2e por hot path pendiente de cableado |
| Doble mensaje/webhook | **PARCIAL** | idempotencia por `order_attempt_id` probada en memoria **y en Postgres**; falta e2e |
| Dos operaciones concurrentes | **PARCIAL** | reserva atómica probada (memoria + `ON CONFLICT` en Postgres); falta e2e |
| Recuperación de estado y rollback | **PARCIAL** | DDL + rollback validados en DB desechable; falta flujo completo |
| Timeout antes/después de respuesta del CRM | **BLOCKED** | necesita CRM sandbox |
| Precio o stock cambió | **BLOCKED** | necesita revalidación contra catálogo/CRM sandbox |
| CRM no disponible | **BLOCKED** | necesita CRM sandbox |

F14 (`simular_conversaciones`), integración DB y `sonda_catalogo`: **BLOCKED** (igual que
baseline). F15.1 contra modelo real (proxy): **NOT RUN** (F15.1 no se modificó; cubierto
determinista).

## 7. Reglas no negociables — cumplimiento

1. No se debilitó ni duplicó F14/F15/F15.1/G5/G6/G13 (verificado por sondas/guards). ✓
2. No se creó bypass ni segunda puerta de autorización (módulo no autoriza nada). ✓
3. El LLM no autoriza, no decide permisos, no escribe SQL (módulo puro, sin LLM). ✓
4. No se cambió contrato público ni comportamiento observable (aditivo, no cableado). ✓
5. No se reescribió `turn.py`/`tools.py` (no se tocaron). ✓
6. No se alteraron sondas/fixtures/tests para forzar PASS. ✓
7. **Sin migración, sin dependencia nueva, sin cambio de esquema.** ✓
8. Sin push/merge/rebase/deploy. ✓
9. Estados reportados como PASS/BLOCKED/NOT RUN sin inventar. ✓
10. La parte que exige tocar barreras/arquitectura transaccional se **detiene y solicita
    autorización** (§8), no se fuerza. ✓

## 8. 🔸 Decisiones pendientes para cerrar el Paso 7

Preguntas 1/2/8 **respondidas (estimadas)** y registradas en ADR-0007 §Revisión (ver §0).
Para pasar de la porción actual al modelo transaccional **cableado** falta:

1. **Confirmar que las decisiones estimadas son definitivas** (en especial el refinamiento
   de P8: fuente de verdad de precio = catálogo, no una tabla propia en NEA). Al
   confirmarse, se aplica `016_order_attempt.sql` a `migrations/` con su ADR/rollback.
2. **CRM sandbox:** configurar `CRM_BASE_URL`/`CRM_BOT_API_KEY` de sandbox para ejercer
   timeout, revalidación de precio/stock y CRM caído (los 3 casos aún BLOCKED).
3. **Autorización para cablear al hot path:** el cableado (sustituir/compañar
   `tools._finalizar_pedido` con `ConfirmarPedidoUseCase`) cambia comportamiento
   observable y debe ir con su spec de pruebas e2e — fuera del alcance aditivo de esta
   iteración.

## 9. Riesgos y rollback

- **Riesgo:** muy bajo. Código aditivo, puro, **no importado** por la ruta caliente; no
  altera DB, CRM, guards ni comportamiento. No hay migración que revertir.
- **Rollback:** borrar `app/application/order_workflow.py` y
  `tests/test_order_workflow_contracts.py`. Sin efectos colaterales.

## 10. Recomendación

**Detenerse tras esta iteración** y obtener del propietario (§8): (a) confirmación de que
las decisiones estimadas son definitivas y (b) credenciales de CRM sandbox. La porción
entregada es behavior-preserving, cubre un hueco de pruebas real, **resuelve y valida en
DB la reserva atómica (parte D)** y deja la DDL y el caso de uso listos para cablear. El
siguiente PR (con autorización) aplicaría la migración, implementaría el método `PgStore`
espejado de la referencia en memoria y cablearía `ConfirmarPedidoUseCase` con pruebas
e2e. **No cablear/migrar/hacer push por iniciativa propia.**
