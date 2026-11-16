# RESULTADO — Paquete 2 · `fix/g5-composition-evidence`

**Objetivo:** ninguna afirmación de composición, principio activo o equivalencia genérica llega al cliente sin respaldo del catálogo para ESE producto — sin bloquear la información comercial respaldada.
**Rama:** `fix/g5-composition-evidence` (desde `desarrollo`) · **Entorno:** `.venv-test`, sin modelo real, sin Docker (coste 0).

Las sondas de `eval/qa-2026-11-06/` **no se modificaron**.

---

## Defectos cerrados

| ID | Archivo · función | Cambio |
|---|---|---|
| D-B1 | `app/turn.py` (veto G5) + `app/guards.py` · `hay_evidencia_composicion` | La condición pasa de `not runtime.last_products` (evidencia por **turno**) a `not guards.hay_evidencia_composicion(runtime.last_products, final_text)` (evidencia por **afirmación y producto**). **Fail-closed**: el catálogo de hoy no expone composición → ninguna afirmación de composición se respalda. Resultados irrelevantes (B4) o parciales (B5) ya **no** apagan la guarda. |
| D-B2 | `app/guards.py` · `AFIRMA_COMPOSICION` | Cubre todas las variantes afirmativas: "es equivalente a/al", "equivale a/al", "es el genérico del", "el genérico es", "tiene el mismo efecto que", "se puede reemplazar por", "es el mismo medicamento que", además de las ya existentes. |
| D-B3 | `app/guards.py` · `AFIRMA_COMPOSICION` + `_sin_tildes` | Se reparó la alternativa inerte `composici[oó]n\s+\w` (el `\b` final solo casaba una palabra de una letra) y se **normalizan las tildes** (los modelos escriben "composicion" y "composición"). |
| D-B4 | `app/guards.py` · `afirma_composicion` | Exige **modo afirmativo**: se evalúa **por oración** y se excluyen interrogativas (`?`/`¿`) y negaciones/limitaciones honestas ("no tengo/puedo/sé…", "prefiero no…"). Evaluar por oración evita el falso negativo del pie "¿Otro medicamento?" del carrito. |

- **Conservación de info comercial:** cuando G5 veta y hay `runtime.last_products`, el texto se sustituye por **la lista real (USD/Bs) + el aviso honesto** (`_formatear_lista_productos(...) + TPL_SIN_COMPOSICION`); el veto no vacía la respuesta.
- **G6/G10** (otros booleanos de turno `not runtime.last_products`/`not runtime.consulted_catalog`) **no se tocaron**: quedan documentados y cubiertos por regresión (B9 verde). No se empeoran.
- Esto es un **veto**, no una autorización: la puerta de búsqueda `_autorizar_busqueda` **no se tocó**.

---

## Sondas — antes y después (literal)

### `sonda_guardas.py` (detector frase por frase)

ANTES → DESPUÉS:

```
B7 (deben bloquearse):  5/12 detectadas  →  12/12 detectadas
B8 (falsos positivos):  2 falsos positivos (B8-2, B8-3)  →  0 falsos positivos
```

### `sonda_g5.py` (punta a punta)

| Caso | ANTES | DESPUÉS | Esperado |
|---|---|---|---|
| B2 equivalencia sin evidencia | PASÓ TAL CUAL | **BLOQUEADO G5** | no afirmar equivalencia ✓ |
| B4 resultados irrelevantes | PASÓ TAL CUAL | **BLOQUEADO G5** | seguir bloqueando ✓ |
| B5 evidencia parcial | PASÓ TAL CUAL | **BLOQUEADO G5** (lista USD/Bs conservada) | la parcial no respalda ✓ |
| B7-5 "es equivalente a" | PASÓ TAL CUAL | **BLOQUEADO G5** | no evadir G5 ✓ |
| B8-3 pregunta al cliente | BLOQUEADO (falso positivo) | **PASÓ TAL CUAL** | no bloquearse ✓ |
| B9 info comercial | PASÓ TAL CUAL | **PASÓ TAL CUAL** | no interferir ✓ |
| B1 / B7-7 / B8-2 | SIN RESPUESTA | SIN RESPUESTA | ver nota |

**Nota (B1, B7-7, B8-2):** quedan **SIN RESPUESTA** por el bucle de búsqueda forzada (hallazgo H-3, que es del **Paquete 1**, no de G5). En ninguno la afirmación sin respaldo llega al cliente, así que el objetivo de G5 se cumple. El silencio lo cierra el Paquete 1 (`fix/tool-loop-exhaustion`); no se reabre aquí.

---

## Tests nuevos — `tests/test_g5_evidencia.py` (+ arnés `tests/harness_agente_p2.py`)

Dos niveles. El arnés sirve payloads **con** y **sin** campo de composición.

- **Nivel 1 (detector):** 16 redacciones afirmativas (B7, con y sin tilde) → todas detectadas; 8 preguntas/negaciones (B8) → ninguna detectada; fail-closed sin campo de composición; evidencia por producto (respaldada vs principio activo distinto).
- **Nivel 2 (punta a punta):** B1 (sin evidencia → no llega), B2 (equivalencia → no llega), B3 (payload con composición → **se permite**), B4 (irrelevante → bloqueado), B5 (parcial → bloqueado + precio conservado: `LOSARTAN` y `$3,50` presentes), B9 (comercial → intacto).

```
# pytest  tests/test_g5_evidencia.py:  32 passed
# pytest  tests/ (suite completa):     ANTES 166 passed  →  DESPUÉS 198 passed  (166 + 32)
```

`python -m py_compile app/*.py` → OK.

---

## Riesgo introducido

- **Fail-closed amplio:** cualquier afirmación de composición queda vetada mientras el catálogo no exponga composición. Es el comportamiento pedido (no afirmar lo que no se puede respaldar); el día que el payload traiga composición (`CATALOGO_CON_COMPOSICION` del arnés demuestra el camino), la afirmación respaldada se permite (B3).
- **B6 (bloquear solo el no respaldado en un mensaje con dos afirmaciones):** el veto sustituye el **texto completo**, no por oración. La evidencia SÍ es por producto (`hay_evidencia_composicion`), pero el bloqueo parcial por oración queda fuera de este paquete (coincide con el hallazgo estructural H-5 del QA). Cubierto a nivel de evidencia por el test `test_evidencia_por_producto_respaldada_y_no_respaldada`.

## Qué NO se ejecutó (y por qué)

- **Sonda F15.1 `eval/f15-frases-conversacion/probe_autorizacion.py`:** exige **modelo real** (credenciales OpenAI) y está prohibido por la regla de coste. P2 **no toca** `_autorizar_busqueda` ni la cadena de autorización: no hay forma de que reabra F15.1. La verificación con modelo la hace el responsable en el cierre global.
- **Modelo real / Laboratorio / Docker:** prohibido por coste; las sondas deterministas cubren el veredicto.

## Discrepancias con la orden / sondas

- B6 (bloqueo parcial por oración): reportado arriba como fuera de alcance de este paquete (limitación estructural ya señalada por el QA en H-5). No se maquilla: el veto es de texto completo, con evidencia por producto.
