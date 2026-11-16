# RESULTADO — Paquete 1 · `fix/tool-loop-exhaustion`

**Objetivo:** ningún turno de farmacia termina sin un mensaje para el cliente.
**Rama:** `fix/tool-loop-exhaustion` (desde `desarrollo`) · **Entorno:** `.venv-test`, sin modelo real, sin Docker (coste 0).

Las sondas de `eval/qa-2026-11-06/` **no se modificaron**: son la vara de medición.

---

## Defectos cerrados

| ID | Archivo · función | Cambio |
|---|---|---|
| D-A1 | `app/turn.py` · `_tool_loop` (rama `empty_rounds >= 3`) | `return _fallback_farmacia(...)` → `return await _respaldo_sin_texto(...)`. Antes se devolvía una **coroutine** sin `await` → `TypeError` en los guards → turno reventado (silencio). |
| D-A2 | `app/turn.py` · `_tool_loop` (final del bucle) | `return None` → `return await _respaldo_sin_texto(...)`: respaldo determinista con lo YA obtenido del catálogo (`runtime.productos_turno`), sin inventar precio/stock/disponibilidad. Se conserva el rescate por `pide_recomendacion`. |
| D-A3 | `app/turn.py` · `_tool_loop` (backstop de búsqueda forzada) | La condición ahora exige `not runtime.catalog_retried`: **un solo** intento de búsqueda forzada por término y turno. Antes, con la puerta F15.1 no autorizando, `consulted_catalog` nunca pasaba a `True` y el backstop re-forzaba cada ronda → agotamiento → silencio. |
| D-A4 | `app/turn.py` · `_tool_loop` (firma de ronda) | Detección de **la misma llamada** (herramienta + argumentos normalizados) repetida **≥3 veces seguidas** → corte con respaldo. Una secuencia de búsquedas **distintas** no es un bucle (firmas distintas → el contador se reinicia) y no se corta. |
| (soporte) | `app/tools.py` · `ToolRuntime` + `_buscar_medicamento` | Campo aditivo `productos_turno` + `_acumular_productos_turno(...)`: acumula TODAS las búsquedas con resultados del turno (dedup por `productId`) para que el respaldo no pierda ningún medicamento obtenido (A4). |

`MAX_TOOL_ROUNDS` **no se tocó**. `app/guards.py` **no se tocó** en este paquete.

---

## Sondas — antes y después (literal)

### `sonda_bucle.py` — ANTES (HEAD de `desarrollo`, `5b7815e`)

```
A1 agotamiento normal        respuestas=0      <NINGUNA — silencio>
A2 llamadas repetitivas      respuestas=0      <NINGUNA — silencio>
A3 argumentos vacios         respuestas=0      <NINGUNA + TypeError: ... got 'coroutine'>
A4 seis medicamentos         respuestas=0      <NINGUNA — silencio>
A7 texto en la ronda 5       respuestas=1      (texto del modelo aceptado)
C0 control flujo normal      respuestas=1      (lista con USD y Bs)
```

### `sonda_bucle.py` — DESPUÉS (esta rama)

```
=== RESUMEN ===
A1 agotamiento normal        respuestas=1
A2 llamadas repetitivas      respuestas=1
A3 argumentos vacios         respuestas=1
A4 seis medicamentos         respuestas=1
A7 texto en la ronda 5       respuestas=1
C0 control flujo normal      respuestas=1
```

A1/A2/A3/A4 pasan de **silencio** a **respuesta enviada**; A3 además **sin excepción**. A7 y C0 siguen enviando el texto del modelo (sin interferencia del respaldo).

---

## Tests nuevos — `tests/test_tool_loop_respaldo.py` (+ arnés `tests/harness_agente_p1.py`)

| ID | Caso | Resultado |
|---|---|---|
| A1 | 6 búsquedas válidas distintas, nunca texto | PASS — una respuesta no vacía; menciona lo obtenido |
| A2 | 6 búsquedas idénticas | PASS — corte por degeneración, con respuesta (`llamadas_llm < 6`) |
| A3 | 6 llamadas con `arguments={}` | PASS — respuesta, `str`, **sin excepción** |
| A3b | idem | PASS — `handle_flush` no registra `reventó` ni `coroutine` (caplog) |
| A4 | 6 medicamentos, 1 búsqueda por ronda | PASS — los 5 obtenidos (MAX=5) están en el texto |
| A5 | búsqueda + catálogo error/timeout | PASS — limitación honesta, sin `$`/`Bs` inventado |
| A6 | 2 búsquedas OK + 1 fallida | PASS — lo respaldado se conserva; lo fallido no se fabrica |
| A7 | 4 rondas de tool + texto válido en la 5ª | PASS — se acepta el texto del modelo |
| A8 | flujo normal (buscar → texto) | PASS — sin interferencia del mecanismo de protección |
| A9 | D-A3: término no autorizado + texto | PASS — el backstop fuerza una vez, no re-itera (`llamadas_llm < MAX`) |

### pytest — ANTES / DESPUÉS (literal)

```
# ANTES (desarrollo):  166 passed in 1.77s
# DESPUÉS (esta rama): 175 passed in 10.74s   (166 existentes + 9 nuevos)
```

`python -m py_compile app/*.py` → OK.

---

## Riesgo introducido

- El respaldo por agotamiento (`_respaldo_sin_texto`) lista `productos_turno`: si el catálogo del turno devolvió productos **irrelevantes**, el cliente podría ver esa lista. Mitigación: solo se usa como ÚLTIMO recurso (el modelo agotó las rondas sin texto) y nunca inventa datos; los guards G1/G5/G6 siguen corriendo sobre el texto final.
- El corte por repetición idéntica (D-A4) usa **≥3 rondas idénticas consecutivas**: una secuencia legítima de búsquedas distintas no se ve afectada (verificado en A1/A4/A7).

## Qué NO se ejecutó (y por qué)

- **Modelo real / Laboratorio / Docker:** prohibido por la regla de coste; las sondas deterministas cubren el veredicto.
- **B3 con catálogo real (Firestore):** fuera del alcance del Paquete 1 (es de G5).

## Discrepancias con la orden / sondas

- Ninguna. A3 produce la **limitación honesta** (plantilla de verificación de catálogo) en lugar de la lista, porque la ruta de argumentos vacíos no puebla `runtime.last_products`; el criterio de A3 ("respuesta, sin excepción, `str`") se cumple y no se inventan datos.
