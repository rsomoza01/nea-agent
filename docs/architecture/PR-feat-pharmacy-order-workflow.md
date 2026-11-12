# PR — Paso 7: Carrito y flujo transaccional de pedido (auditoría + porción no bloqueada)

> **Rama:** `feat/pharmacy-order-workflow` · **Base / HEAD inicial:** `cf2dde6`
> (`refactor/turn-orchestrator`) · **Fecha:** 2026-11-12 · **Modo:** implementación de
> dominio incremental; **parcialmente BLOCKED** por entorno (Docker/CRM caídos) y por
> decisiones abiertas del propietario (PLAN §8, preguntas 1/2/8).

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
| `app/application/order_workflow.py` | nuevo (+~190) | Contratos: `OrderPhase` + grafo de transiciones, `OrderConfirmStatus`/`OrderConfirmResult`, `order_attempt_id` (`nuevo_order_attempt_id`, `OrderAttempt`), `cart_fingerprint`. **Puro, sin I/O, sin LLM, sin autorización.** |
| `tests/test_order_workflow_contracts.py` | nuevo (+~160) | 15 pruebas: unicidad/reintento de id, fingerprint ≠ clave, estabilidad del fingerprint, transiciones permitidas/prohibidas, confirmación obligatoria, limitación G13. |

**No** se tocó runtime (`turn.py`/`tools.py`/`db.py`/`crm.py`), **no** hay migración,
**no** hay dependencia nueva (solo `uuid`/`dataclasses`/`enum` de la stdlib), **no** se
cableó el caso de uso. Diff de runtime observable: **0** (módulo aditivo no importado por
la ruta caliente).

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
| **Contrato pedido** (15) | `pytest tests/test_order_workflow_contracts.py` | **15 passed** |
| Suite completa | `pytest tests/ -q` | **319 passed** (304 baseline + 15 nuevos) |
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

Los 7 casos de integración exigidos por el Paso 7 **requieren DB desechable + CRM
sandbox**, ambos no disponibles (Docker caído, credenciales CRM sin configurar):

| Caso obligatorio | Estado | Bloqueo |
|---|---|---|
| Confirmación explícita y rechazo | **BLOCKED** | necesita flujo transaccional + DB/CRM |
| Doble mensaje/webhook | **BLOCKED** | necesita persistencia de `order_attempt_id` + DB |
| Timeout antes/después de respuesta del CRM | **BLOCKED** | necesita CRM sandbox |
| Dos operaciones concurrentes | **BLOCKED** | necesita DB real + reserva atómica (parte D) |
| Precio o stock cambió | **BLOCKED** | necesita revalidación contra CRM |
| CRM no disponible | **BLOCKED** | necesita CRM sandbox |
| Recuperación de estado y rollback | **BLOCKED** | necesita migración + DB desechable |

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

## 8. 🔸 Decisiones pendientes del propietario (desbloquean el resto del Paso 7)

Antes de poder diseñar e implementar el modelo transaccional completo necesito respuesta
a las preguntas abiertas del PLAN §8 (no se pueden inferir del código ni inventar):

1. **¿El CRM ofrece (o puede ofrecer) una API de pedidos**, o el pedido seguirá siendo
   una nota de ficha? Hoy no existe (auditado en §2).
2. **¿Debe persistirse una entidad de pedido** con estados
   `confirmado/aceptado/preparado/entregado/cancelado`, y dónde (CRM vs. tabla NEA)?
3. **¿Cuál es la fuente de verdad de precio/disponibilidad al confirmar?** (¿se revalida
   contra el catálogo/CRM en el momento de cerrar?)
4. **Entorno de pruebas:** ¿puedo levantar Docker/DB desechable y apuntar a un CRM
   sandbox? Sin eso, los 7 casos obligatorios seguirán BLOCKED.

## 9. Riesgos y rollback

- **Riesgo:** muy bajo. Código aditivo, puro, **no importado** por la ruta caliente; no
  altera DB, CRM, guards ni comportamiento. No hay migración que revertir.
- **Rollback:** borrar `app/application/order_workflow.py` y
  `tests/test_order_workflow_contracts.py`. Sin efectos colaterales.

## 10. Recomendación

**Detenerse y esperar decisión del propietario** sobre §8 antes de continuar con el
modelo transaccional del Paso 7. La porción entregada es behavior-preserving, cubre un
hueco de pruebas real y deja listos los contratos de idempotencia/confirmación para
cuando el entorno (Docker/CRM) y las decisiones 1/2/8 estén disponibles. **No cablear ni
migrar por iniciativa propia; no hacer push.**
