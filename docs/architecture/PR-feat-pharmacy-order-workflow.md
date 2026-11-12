# PR — Paso 7: Carrito y flujo transaccional de pedido (auditoría + porción no bloqueada)

> **Rama:** `feat/pharmacy-order-workflow` · **Base / HEAD inicial:** `cf2dde6`
> (`refactor/turn-orchestrator`) · **Fecha:** 2026-11-12 · **Modo:** implementación de
> dominio incremental; **parcialmente BLOCKED** por CRM sandbox no configurado y por
> decisiones del propietario ESTIMADAS (PLAN §8, preguntas 1/2/8).
>
> **Actualización 2026-11-12 (2ª iteración):** el propietario habilitó Docker y respondió
> 1/2/8 de forma estimada. Con ello se **desbloqueó la parte D de ADR-0007** (reserva
> atómica del intento), que se diseñó, implementó (puerto + referencia en memoria) y
> **validó contra Postgres desechable**.
>
> **Actualización 2026-11-12 (3ª iteración — decisiones 1-3 CONFIRMADAS definitivas):** el
> propietario confirmó las decisiones 1-3. Se ejecutó la recomendación sin romper diseño:
> (a) **migración `016_order_attempt.sql` APLICADA** a `migrations/` (validada contra
> Postgres desechable con las 16 migraciones + idempotencia `ON CONFLICT` real); (b)
> **`PgStore` + `MemoryStore` implementan `reservar_intento`/`obtener_intento`**
> (satisfacen el puerto `OrderAttemptStore`); (c) **`ConfirmarPedidoUseCase` CABLEADO** en
> `tools._finalizar_pedido` como **registro durable y auditable** del pedido, creado ANTES
> de la nota best-effort del CRM — preservando todo el comportamiento observable. Sigue
> BLOCKED solo lo que exige **CRM sandbox** (timeout, revalidación precio/stock, CRM
> caído): credenciales no configuradas en esta sesión.

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
| `app/application/order_workflow.py` | nuevo | Contratos: `OrderPhase` + grafo de transiciones, `OrderConfirmStatus`/`OrderConfirmResult`, `order_attempt_id`, `cart_fingerprint`; **puerto `OrderAttemptStore`** + referencia en memoria + `ConfirmarPedidoUseCase`. Puro, sin LLM, sin autorización. |
| `migrations/016_order_attempt.sql` | **nuevo (APLICADO)** | Tabla `order_attempt` (registro durable del pedido en NEA). Rollback: `DROP TABLE order_attempt;` |
| `app/state.py` | +~25 | `Store` protocol + `MemoryStore`: `reservar_intento`/`obtener_intento` (semántica `ON CONFLICT`). |
| `app/db.py` | +~45 | `PgStore`: `reservar_intento` (`INSERT … ON CONFLICT DO NOTHING RETURNING` + fallback SELECT) / `obtener_intento`. |
| `app/tools.py` | +~15 | `_finalizar_pedido` **cablea** `ConfirmarPedidoUseCase` como registro durable ANTES de `put_ficha` (best-effort, aditivo). |
| `tests/test_order_workflow_contracts.py` | nuevo | 23 pruebas de contrato (unicidad/reintento, fingerprint ≠ clave, transiciones, confirmación, reserva atómica A/B/C incl. concurrente, caso de uso). |
| `tests/test_order_finalize_wiring.py` | nuevo | 3 pruebas e2e: finalizar preserva comportamiento + registra intento; **el intento sobrevive aunque falle el CRM**; carrito vacío no registra ni cierra. |

**Comportamiento observable preservado:** el cableado es **aditivo y best-effort** — mismos
mensajes, misma nota de ficha, mismas señales G13 (`pedido_finalizado`), mismo
`cart_clear`/`cart_closed` y limpieza de entrega. La única diferencia observable es un
**registro durable adicional** del pedido en NEA (mejora de auditoría/recuperación). No se
reescribió `turn.py`/`tools.py`, no hay dependencia nueva (solo stdlib), no se tocaron
guards ni autorización.

## 3b. Validación contra Postgres desechable (DB) — regla no negociable 7

Con Docker UP se levantó `postgres:16-alpine` desechable y se aplicaron **las 16
migraciones en orden** (todas OK), incluida `016_order_attempt.sql`:

- Tabla creada con PK `order_attempt_id`, FK `conversation_id → bot_conversation(id)
  ON DELETE CASCADE`, índice `idx_order_attempt_conv`, `phase`/`created_at` con default.
- **Reserva atómica probada en SQL real:** 1er `INSERT … ON CONFLICT DO NOTHING
  RETURNING` → 1 fila; 2º con el **mismo** id y otra huella → **0 filas** (no duplica, no
  sobrescribe); `order_attempt` queda con **1 fila** y la huella de la **primera** reserva
  (`A:1`). Coincide exactamente con `PgStore.reservar_intento` (RETURNING vacío → SELECT
  del existente) y con `MemoryStore`.
- **Rollback probado:** `DROP TABLE order_attempt;` (sin datos productivos). `--rm`.

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
5. **Cableado como registro durable, no como sustituto del flujo.** `ConfirmarPedidoUseCase`
   se invoca en `_finalizar_pedido` **antes** de la nota del CRM y es **best-effort**: su
   valor es dejar evidencia del pedido en NEA aunque el CRM falle (mejora de
   auditoría/recuperación, Paso 7 pt 9/10). NO reemplaza `put_ficha`, NO cambia mensajes,
   NO afirma aceptación por la farmacia (G13). La confirmación explícita se detecta aguas
   arriba (backstops), por eso se invoca con `confirmacion_explicita=True`.
6. **Fuente de verdad de precio = catálogo (no `order_attempt`).** La tabla solo guarda la
   **huella** del carrito como evidencia; el precio/stock autoritativo sigue viniendo del
   catálogo del CRM (ADR-0007 §Revisión P8). No se creó tabla de precios en NEA.

## 5. Pruebas (comandos y resultados)

Intérprete: `.venv-test/Scripts/python.exe` (Python 3.11.16). `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.

| Prueba | Comando | Resultado |
|---|---|---|
| Compilación | `py_compile order_workflow/state/db/tools.py` + import sin ciclos | **PASS** |
| **Contrato pedido** (23) | `pytest tests/test_order_workflow_contracts.py` | **23 passed** |
| **E2E cableado finalizar** (3) | `pytest tests/test_order_finalize_wiring.py` | **3 passed** |
| DB desechable (16 migraciones + reserva atómica + rollback) | `docker run postgres:16-alpine` + psql (§3b) | **PASS** |
| Suite completa | `pytest tests/ -q` | **330 passed** (304 baseline + 26 nuevos) |
| Sonda P1 | `PYTHONPATH=. python tests/harness_agente_p1.py` | **exit 0** (paso) |
| Sonda P2 | `PYTHONPATH=. python tests/harness_agente_p2.py` | **exit 0** (paso) |
| Sonda P3 | `PYTHONPATH=. python tests/harness_agente_p3.py` | **exit 0** (paso) |
| F15.1 por término | `python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` | **10 pruebas, 0 huecos, exit 0** |
| Guards G5 / G13 | `pytest tests/test_g5_evidencia.py tests/test_g13_accion.py` | **44 passed** |
| Guard G6 (script) | `python tests/test_g6_lista_mostrada.py` | **15/15 OK, exit 0** |

### Cobertura nueva
El flujo de pedido no tenía pruebas unitarias directas (hueco mayor registrado en
`FLOW-ORDERS §9`). Las 26 pruebas nuevas cubren, de forma determinista, la semántica de
idempotencia de intento (A/B/C), el fingerprint, las transiciones de fase, la reserva
atómica y **el e2e de `_finalizar_pedido`** (preserva comportamiento, registra el intento
y sobrevive a un fallo del CRM). Es la primera cobertura automatizada directa del cierre
de pedido.

## 6. Pruebas obligatorias NO ejecutadas y motivo (BLOCKED)

De los 7 casos obligatorios, la **lógica** de confirmación/idempotencia/concurrencia está
cubierta por tests de contrato + e2e + validación en Postgres; lo que sigue BLOCKED es lo
que depende de **CRM sandbox** (credenciales no configuradas en esta sesión):

| Caso obligatorio | Estado | Detalle |
|---|---|---|
| Confirmación explícita y rechazo | **PASS** | `ConfirmarPedidoUseCase` + e2e `_finalizar_pedido` (confirmación/carrito vacío) |
| Doble mensaje/webhook | **PASS** | idempotencia por `order_attempt_id` probada en memoria **y en Postgres** (`ON CONFLICT`) |
| Dos operaciones concurrentes | **PASS** | reserva atómica probada (concurrencia en memoria + `ON CONFLICT` en Postgres) |
| Recuperación de estado y rollback | **PARCIAL** | registro durable del pedido (sobrevive fallo CRM) + rollback DDL validado; recuperación completa con el CRM pendiente de sandbox |
| Timeout antes/después de respuesta del CRM | **BLOCKED** | necesita CRM sandbox |
| Precio o stock cambió | **BLOCKED** | necesita revalidación contra catálogo/CRM sandbox |
| CRM no disponible | **PARCIAL** | e2e cubre que el pedido queda registrado en NEA aunque `put_ficha` falle; timeout/variantes requieren CRM sandbox |

F14 (`simular_conversaciones`), integración DB y `sonda_catalogo`: **BLOCKED** (igual que
baseline). F15.1 contra modelo real (proxy): **NOT RUN** (F15.1 no se modificó; cubierto
determinista).

## 7. Reglas no negociables — cumplimiento

1. No se debilitó ni duplicó F14/F15/F15.1/G5/G6/G13 (verificado por sondas/guards). ✓
2. No se creó bypass ni segunda puerta de autorización (`order_attempt` no autoriza). ✓
3. El LLM no autoriza, no decide permisos, no escribe SQL (reserva por `ON CONFLICT`
   parametrizado, sin SQL dinámico). ✓
4. **Comportamiento observable preservado:** el cableado es aditivo/best-effort; mismos
   mensajes, misma nota de ficha, mismas señales G13, mismo cierre/limpieza. Único cambio:
   un registro durable extra en NEA. Cubierto por e2e. ✓
5. No se reescribió `turn.py`/`tools.py` (solo +15 líneas aditivas en `_finalizar_pedido`,
   sin tocar el orden de autorización/tools/guards/envío). ✓
6. No se alteraron sondas/fixtures/tests para forzar PASS. ✓
7. **Migración `016` con ADR (0007), rollback (`DROP TABLE`) y prueba en DB desechable**
   (§3b). Sin dependencia nueva (solo stdlib). ✓
8. Sin push/merge/rebase/deploy. ✓
9. Estados reportados como PASS/PARCIAL/BLOCKED sin inventar. ✓
10. Lo que exige CRM sandbox se mantiene **fail-safe** (G13: no afirma aceptación) y se
    reporta BLOCKED, no se fuerza. ✓

## 8. 🔸 Pendiente para cerrar el Paso 7 por completo

Decisiones 1-3 **confirmadas definitivas**; migración aplicada y caso de uso cableado.
Queda, dependiente de infraestructura/autorización:

1. **CRM sandbox:** configurar `CRM_BASE_URL`/`CRM_BOT_API_KEY` de sandbox para ejercer los
   3 casos aún BLOCKED (timeout CRM, revalidación precio/stock contra catálogo, variantes
   de CRM caído más allá del fallo de `put_ficha` ya cubierto).
2. **Revalidación de precio/stock al confirmar** (ADR-0007 §Revisión P8): releer el
   catálogo y comparar con `cart_fingerprint` antes de cerrar. Requiere CRM sandbox para
   probarse end-to-end; se diseña como extensión del `ConfirmarPedidoUseCase`.
3. **Máquina de estados extendida del pedido** (`aceptado/preparado/entregado`): solo si el
   negocio decide seguimiento de estado real; hoy NEA registra el intento y no afirma
   aceptación (G13). Decisión de producto pendiente.

## 9. Riesgos y rollback

- **Riesgo:** bajo. El cableado es aditivo y best-effort (un fallo del registro no derriba
  el turno ni cambia mensajes); e2e demuestra preservación de comportamiento y que el
  pedido sobrevive a un fallo del CRM. La tabla nueva no afecta flujos existentes.
- **Rollback:** `DROP TABLE order_attempt;` (migración 016) + revertir el commit (quita el
  import/cableado en `tools.py`, los métodos de store y los módulos/tests nuevos). Sin
  datos productivos que migrar.

## 10. Recomendación

**Mejora entregada y verificada**, lista para revisión del propietario. Preserva todo el
comportamiento observable y aporta un beneficio concreto: **registro durable y auditable
del pedido en NEA que sobrevive a un fallo del CRM** (antes el pedido se perdía si la nota
best-effort fallaba), con idempotencia probada en memoria y en Postgres. El siguiente PR,
con **CRM sandbox**, cerraría los 3 casos BLOCKED y añadiría la revalidación de precio/stock
al confirmar. **No hacer push/merge/deploy por iniciativa propia.**
