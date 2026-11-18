# PR — Paso 6: Modularizar orquestación de turn.py (extracción 1 — partición de mensajes)

> **Rama:** `refactor/turn-orchestrator` · **Base / HEAD inicial:** `525525e`
> (`refactor/pharmacy-catalog-usecase`) · **Fecha:** 2026-11-10 · **Modo:** refactor
> controlado (primera extracción incremental del God-module `turn.py`).

## 1. Resumen ejecutivo

Se extrae **una sola responsabilidad de bajo riesgo** de `turn.py`: la **partición de
mensajes salientes para WhatsApp** (`_partir_mensaje_largo` + `WA_MAX_CHARS`). Es una
función **pura** (sin estado, sin efectos, sin excepciones) que divide un texto >4096
chars en partes enviables cortando por párrafos para no romper una opción `💊` a la
mitad. Se mueve a `app/wtext.py` —el módulo hoja que ya posee el texto saliente de
WhatsApp (`a_formato_whatsapp`)— como `partir_mensaje_largo` (API pública del módulo).

`turn.py` mantiene su flujo público **intacto como fachada**: reimporta
`WA_MAX_CHARS` con el mismo nombre y conserva `_partir_mensaje_largo` como alias
(`_partir_mensaje_largo is wtext.partir_mensaje_largo`). `sender.py` deja de importar
desde `turn` (import tardío) y pasa a importar de `wtext` a nivel de módulo, lo que
**elimina una arista de acoplamiento worker→orquestador** (DEPENDENCY-MAP §2.5,
`sender.py:56`).

El comportamiento observable se preserva **por identidad**: la lógica no se tocó (se
movió byte-a-byte) y los call-sites (`turn._send`, `sender._attempt`) siguen llamando
a la misma función con los mismos argumentos y en el mismo orden
(`a_formato_whatsapp` → partir, en el turno vivo). No se alteró autorización, tools,
guards, envío ni handoff.

## 2. Alcance y archivos

| Archivo | Cambio | Rol |
|---|---|---|
| `app/wtext.py` | +45 / −0 | Nuevos `WA_MAX_CHARS` y `partir_mensaje_largo` (movidos desde `turn.py`, lógica idéntica) + nota en el docstring del módulo. |
| `app/turn.py` | +6 / −37 | Elimina la definición local; importa de `wtext`; deja alias de fachada `_partir_mensaje_largo` y re-export de `WA_MAX_CHARS`. |
| `app/sender.py` | +2 / −3 | Import de `wtext` a nivel de módulo (antes import tardío de `turn`). |
| `tests/test_wtext_particion.py` | nuevo | 10 pruebas (unidad + fachada + integración; ver §5). |

`git diff 525525e HEAD --numstat -- app/`: 3 archivos de runtime, **+53 / −40** + 1 test
nuevo. `turn.py` pasa de 8328 a **8297 LOC**.

## 3. Antes de editar — responsabilidad, consumidores y puertas

- **Responsabilidad**: partir un mensaje largo en partes ≤ tope para la entrega por
  WhatsApp (el CRM responde 422 a >4096 chars — incidente receta de 8 medicamentos,
  4831 chars). Entradas: `(texto: str, max_chars: int)`. Salida: `list[str]`
  (nunca partes vacías). **Pura**: solo usa `re`; sin estado, sin I/O, sin excepciones.
- **Consumidores** (exhaustivo, `grep` en `app/`):
  1. `turn._send` (`turn.py:3016`, ruta caliente del turno vivo) —
     `_partir_mensaje_largo(a_formato_whatsapp(text), WA_MAX_CHARS)`.
  2. `sender._attempt` (`sender.py:58`, reenvío diferido de `pending_send`) —
     `_partir_mensaje_largo(item.content, WA_MAX_CHARS)` (sin reformatear: el contenido
     ya se guardó en dialecto WhatsApp).
- **Efectos secundarios / estado mutado / excepciones**: ninguno (función pura). El
  estado y la I/O viven en los consumidores (`lab_outbox`, `crm.send_message`,
  `enqueue_pending_send`), que **no se tocaron**.
- **Orden de ejecución a preservar**: en el turno vivo, **formatear antes de partir**
  (`a_formato_whatsapp` → `partir_mensaje_largo`). En el reenvío diferido, **no** se
  reformatea. Ambos órdenes se conservan y se verifican con tests (§5).
- **Barreras protegidas**: ninguna interviene. La partición no autoriza, no busca, no
  decide permisos ni handoff; es puramente de presentación/entrega.

## 4. Decisiones y justificación

1. **Hogar = `wtext.py`, no un módulo nuevo.** `wtext` ya es la hoja (nivel 0, solo
   stdlib) dueña del texto saliente de WhatsApp; su docstring ya describía el pipeline
   de `_send`. Poner la partición junto al formateo agrupa la responsabilidad "preparar
   texto para la entrega" sin crear un módulo central (regla 7 del Paso 6). Se prefiere
   extraer **funciones puras primero** (regla 3) antes de cualquier coordinador.
2. **Nombre público `partir_mensaje_largo` + alias de fachada.** En `wtext` la función
   es API pública del módulo (sin guion bajo). `turn.py` mantiene `_partir_mensaje_largo`
   como alias para no romper call-sites internos ni `from app.turn import
   _partir_mensaje_largo`; `WA_MAX_CHARS` se re-exporta con el mismo nombre. `run_turn` y
   el flujo público quedan como fachada compatible (regla 4).
3. **Corte del acoplamiento `sender → turn`.** El import tardío en `sender._attempt`
   existía para **evitar importar el orquestador pesado en import-time**. Al vivir la
   partición en una hoja, `sender` importa `wtext` a nivel de módulo sin reintroducir el
   ciclo: es una dependencia **hacia abajo** (nivel 4 → nivel 0), no una inversión.
4. **Lógica movida byte-a-byte.** No se reescribió el algoritmo de corte; se copió
   literal. La equivalencia se garantiza por construcción y se prueba por identidad
   (`partes == partir_mensaje_largo(a_formato_whatsapp(texto), WA_MAX_CHARS)`).

## 5. Pruebas (comandos y resultados)

Intérprete: `.venv-test/Scripts/python.exe` (Python 3.11.16). Encoding:
`PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.

| Prueba | Comando | Resultado |
|---|---|---|
| Compilación | `python -m py_compile app/wtext.py app/turn.py app/sender.py` | **PASS** |
| Import de arranque | `python -c "import app.turn, app.sender, app.wtext, app.main"` | **PASS** (sin ciclo) |
| Fachada (nombres + identidad) | incluido en el import check | **PASS** (`WA_MAX_CHARS==4000`, alias `is`) |
| **Unidad** partición (párrafos, tope, bordes, receta) | `pytest tests/test_wtext_particion.py` | **10 passed** |
| **Integración** turno vivo (`_send` formatea→parte, lab_outbox) | idem (incluida) | **PASS** |
| **Integración** reenvío diferido (`sender._attempt` usa la partición) | idem (incluida) | **PASS** |
| Suite completa | `pytest tests/ -q` | **304 passed** (294 baseline + 10 nuevos) |
| Sonda P1 | `PYTHONPATH=. python tests/harness_agente_p1.py` | **exit 0** (paso) |
| Sonda P2 | `PYTHONPATH=. python tests/harness_agente_p2.py` | **exit 0** (paso) |
| Sonda P3 | `PYTHONPATH=. python tests/harness_agente_p3.py` | **exit 0** (paso) |
| Sonda F15.1 por término | `PYTHONPATH=. python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` | **10 pruebas, 0 huecos, exit 0** |
| Guards G5 / G13 | `pytest tests/test_g5_evidencia.py tests/test_g13_accion.py` | **44 passed** |
| Guard G6 (script) | `PYTHONPATH=. python tests/test_g6_lista_mostrada.py` | **15/15 OK, exit 0** |

### Nueva cobertura

`_partir_mensaje_largo` **no tenía pruebas** antes del Paso 6 (ni `wtext`). La extracción
añade cobertura real, no solo movimiento: corte por párrafos sin romper opciones `💊`,
acumulación mientras entren, corte duro de un párrafo que excede el tope, caso tipo
"receta de 8 medicamentos" (>4000 chars) con todas las partes ≤ tope y ningún bloque
perdido, y la equivalencia de orden formatear→partir en el turno vivo.

## 6. Pruebas no ejecutadas y motivo

- **F14 (`simular_conversaciones`), integración DB, `sonda_catalogo`**: **BLOCKED** por
  Docker caído / credenciales CRM (igual que la baseline, `BASELINE-TEST-MATRIX.md`). La
  responsabilidad extraída es de presentación/entrega y **no toca** la ruta de búsqueda,
  intención ni guards; las sondas P1/P2/P3 y los guards deterministas cubren la no
  regresión del turno.
- **Sonda F15.1 contra modelo real (proxy)**: **NOT RUN**; la autorización por término
  se cubrió de forma determinista y F15/F15.1 no se modificaron.

## 7. Métricas de reducción de acoplamiento (Paso 6, punto 10)

| Métrica | Antes | Después | Δ |
|---|---|---|---|
| Aristas de import **→ `turn`** desde `app/` | 4 (followup, **sender**, main, webhook) | 3 (followup, main, webhook) | **−1** |
| Imports tardíos (function-level) **→ `turn`** | 2 (sender, `llm`/`media` aparte) | 1 | **−1 en la ruta de envío** |
| Acoplamiento **worker→orquestador** (`sender→turn`) | sí | **eliminado** (`sender→wtext`, hoja) | resuelto |
| Nombres que `turn.py` **define** y exporta | +`WA_MAX_CHARS`, `_partir_mensaje_largo` | solo fachada (alias/re-export) | responsabilidad trasladada |
| LOC `turn.py` | 8328 | 8297 | −31 |
| LOC movidos a la hoja `wtext` | — | +45 (func 33 + constante + docstring) | — |
| Cobertura de la partición | 0 tests | 10 tests | +10 |

El éxito **no es el recuento de LOC** (−31 es modesto): es que una función pura de
entrega sale del God-module, que `sender` deja de depender del orquestador, y que una
responsabilidad antes sin pruebas queda cubierta y con un hogar correcto en la capa 0.

## 8. Reglas no negociables — cumplimiento

1. No se reescribió `turn.py`: solo se removió una función pura y se dejó fachada. ✓
2. No se alteró el orden de autorización, tools, guards, envío ni handoff. ✓
3. `conversation_lock`, coalescing, idempotencia, tool rounds, backstops y fallbacks
   intactos (no se tocó ninguno). ✓
4. No se creó un módulo central que absorba reglas; se reusó la hoja `wtext`. ✓
5. No se debilitó ni duplicó F14/F15/F15.1/G5/G6/G13. ✓ (verificado por sondas/guards)
6. Comportamiento del cliente preservado por identidad (misma partición, mismo orden). ✓
7. Tests unitarios **y** de integración para la responsabilidad extraída. ✓
8. Sin dependencias nuevas, sin cambios de esquema. ✓
9. Sin push/merge/rebase/deploy. ✓

## 9. Riesgos y rollback

- **Riesgo:** muy bajo. La lógica no cambió (movida byte-a-byte); los consumidores llaman
  igual; la fachada evita romper imports existentes. La única dependencia nueva es
  `sender → wtext` (hoja).
- **Rollback:** devolver `WA_MAX_CHARS` + `_partir_mensaje_largo` a `turn.py`, revertir el
  import/alias, restaurar el import tardío en `sender.py` y borrar el test nuevo. Sin
  migraciones ni esquema que revertir.

## 10. Recomendación

**Continuar** tras aprobación del propietario. La extracción es behavior-preserving, corta
una arista de acoplamiento real y deja un patrón repetible para las siguientes
extracciones puras de `turn.py` (p. ej. el cluster de parsing de OCR/receta, §1 del
mapa). No avanzar por iniciativa propia ni hacer push.
