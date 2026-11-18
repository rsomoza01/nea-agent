# QA técnico independiente — GenteFarma NEA
## Bucle de herramientas (`fix/tool-loop-exhaustion`) y G5 (`fix/g5-composition-evidence`)

Fecha: 2026-11-06 · Repo: `github.com/arquimedesgarcia/genteFarma-nea` (espejo) + `rsomoza01/nea-agent` (origin)
Ejecutado por: agente QA independiente (solo lectura; **no se modificó ningún archivo, rama ni commit**)
Entorno: copia local `D:\Proyectos\GenteFarma\agentNEA`, venv `.venv-test` (python del repo), sin contenedores, sin modelo LLM (sondas con LLM guionado → coste 0)

---

## A. Estado general

**FAIL — no hay nada que validar en ninguna de las dos líneas, y la base (`desarrollo`) tiene defectos CRÍTICOS reproducidos que esas dos líneas debían cerrar.**

Resumen ejecutivo:

1. **Las dos ramas de trabajo apuntan al mismo commit que `desarrollo`** (`5b7815e`), con **árbol idéntico** (`3a26aaa2ddfefab796ddc87877d40fc3c8ab40e7`). Cero commits adelante, cero archivos modificados, cero diff. No existe bucle de herramientas "arreglado" ni G5 "con evidencia" que evaluar: **las ramas son marcadores de posición**.
2. Verificado por dos vías independientes: `git ls-remote` local y la API de GitHub (`/repos/arquimedesgarcia/genteFarma-nea/branches`). Ninguna rama equivalente existe en `origin` (`rsomoza01/nea-agent`).
3. Como no hay cambio que validar, se midió **la línea base** en su lugar, con sondas deterministas (sin modelo) sobre el código real. Resultado: **4 hallazgos CRÍTICOS reproducidos**, dos de ellos dejan al cliente **sin ninguna respuesta**.
4. La instrucción "no aceptar como evidencia que los tests pasen" se cumple: los 166 tests pasan (`166 passed`) y **aun así** los casos A1/A2/A3/A4 y B1/B2/B4/B5 fallan en ejecución.

---

## B. Línea base

### Comits verificados (HEAD reales, no asumidos)

| Ref | Commit | Fecha | Título |
|---|---|---|---|
| `desarrollo` (local, `origin/desarrollo`, `gentefarma/desarrollo`) | `5b7815e` | 2026-11-05 | fix(f15.1): el rechazo de la capa es conclusivo + spec del veto G13 |
| `gentefarma/fix/tool-loop-exhaustion` | `5b7815e` | 2026-11-05 | (idéntico: sin commits propios) |
| `gentefarma/fix/g5-composition-evidence` | `5b7815e` | 2026-11-05 | (idéntico: sin commits propios) |

Los tres árboles comparten el hash `3a26aaa` → `git diff --stat desarrollo gentefarma/fix/tool-loop-exhaustion` = vacío (ídem G5).

### Procedencia de los commits citados en el encargo

| Commit | ¿Ancestro de `desarrollo`? | Nota |
|---|---|---|
| `962c541` spec F15.1 | sí | |
| `3800475` re-medición corpus | sí | |
| `20dda54` F15.1 por término | sí | cierra F15.1 |
| `d84d1b8`, `65c53b5` validación/procedencia | sí | |
| `5b7815e` contrato + G13 + A/B | sí | HEAD actual |
| `238ad7a` (PR #1, `fix/f15-fail-closed`) | **no** | su contenido se portó en `20dda54`; la rama sigue abierta como PR #1 |
| `9bab279` (PR #2, `fix/f15-term-scoped-search`) | **no** | ídem; PR #2 abierto |

`origin` (`rsomoza01/nea-agent`) solo publica `main`, `desarrollo` y `001-agente-farmacia-rol`: **las dos ramas del encargo no existen en el remoto del cliente**.

### Ramas remotas en el espejo

`agent-qa` (`8a8505b`, atrasada), `desarrollo` (`5b7815e`), `fix/f15-fail-closed` (`238ad7a`), `fix/f15-term-scoped-search` (`9bab279`), `fix/g5-composition-evidence` (`5b7815e`), `fix/tool-loop-exhaustion` (`5b7815e`). PRs abiertos: #1 y #2 (línea F15 previa).

### Archivos afectados por cada rama

**Ninguno.** No hay archivos, funciones ni rutas de ejecución que la rama cambie; por construcción tampoco hay "commit base" distinto: su base *es* su HEAD.

### Tests existentes relacionados

- `tests/test_f14_busqueda.py` + `tests/test_f15_intencion.py` → **166 casos** (`pytest tests/ -q`: `166 passed in 1.77s`).
- `app/guards.py`, `app/turn.py`, `app/tools.py` compilan (`python -m py_compile app/*.py` → OK).
- No existe test alguno que ejercite `_tool_loop` (agotamiento, argumentos vacíos, repetición) ni la ruta de G5 de punta a punta.

### Riesgos identificados antes de ejecutar

1. Validación A y B **no ejecutables** con esta entrega (no hay cambio).
2. Las dos líneas corresponden a mecanismos **ya presentes** en la base (`MAX_TOOL_ROUNDS`, `_fallback_farmacia`, veto G5) con huecos visibles por lectura; el riesgo de "creer que están cubiertos porque el código los menciona" era alto.
3. El catálogo real (Firestore) no está disponible en local → B3 ("evidencia explícita") no es verificable con datos reales.

---

## C. Resultados por rama

### C.1 Bucle de herramientas — `fix/tool-loop-exhaustion`

**Sin contenido.** No se puede ejecutar A1–A8 contra la rama. Lo que sí se ejecutó es la base, con el resultado de la matriz D: **A1, A2, A3 y A4 fallan** (turno sin respuesta o turno reventado), **A7 y el control pasan**.

### C.2 G5 — `fix/g5-composition-evidence`

**Sin contenido.** No se puede ejecutar B1–B9 contra la rama. Base: **B2, B4, B5, B7-5, B8-3 fallan**; B9 (no interferencia con info comercial) pasa; B3 no ejecutable (depende del catálogo real).

---

## D. Matriz de pruebas

Ejecutadas (todas reproducibles con los comandos de la sección H):

| ID | Escenario | Resultado | Evidencia | Observaciones |
|---|---|---|---|---|
| T-01 | Suite existente en `desarrollo` | PASS | `pytest tests/ -q` → `166 passed in 1.77s` | No cubre `_tool_loop` ni G5 e2e |
| T-02 | Compilación de módulos | PASS | `py_compile app/*.py` OK | |
| T-03 | Árbol de las dos ramas | **FAIL (sin entregable)** | tree `3a26aaa` == `desarrollo` en ambas; `diff --stat` vacío | Confirmado por `git ls-remote` y API GitHub |
| T-04 | Sonda del bucle (LLM guionado, determinista) | A1 FAIL · A2 FAIL · A3 FAIL · A4 FAIL · A7 PASS · C0 PASS | `sonda_bucle.json` | A3 además lanza excepción (TypeError) |
| T-05 | Sonda de detectores de guardas (30 frases) | B7: 5/12 detectadas · B8: 2 falsos positivos | `sonda_guardas.txt` | Funciones puras de `app/guards.py` |
| T-06 | Sonda G5 de punta a punta (LLM guionado) | B1 sin respuesta · B2 pasa · B4 pasa · B5 pasa · B7-7 sin respuesta · B7-5 pasa · B8-2 sin respuesta · B8-3 bloqueado (falso positivo) · B9 PASS | `sonda_g5.json` | El texto del modelo se compara contra las plantillas constantes de `guards.py` |

Detalle de T-04 (respuestas capturadas en `ctx.lab_outbox`, es decir lo que el cliente habría recibido):

| Caso | Guion del modelo | Rondas consumidas | Respuesta al cliente |
|---|---|---|---|
| A1 agotamiento normal | 6 búsquedas válidas distintas, nunca texto | 5/5 | **ninguna (silencio)** |
| A2 llamadas repetitivas | 6 búsquedas idénticas `losartan 50 mg` | 5/5 | **ninguna (silencio)** |
| A3 argumentos vacíos `{}` | 6 llamadas con `{}` | 3 | **ninguna + excepción** `TypeError: expected string or bytes-like object, got 'coroutine'` |
| A4 seis medicamentos | 6 medicamentos, 1 llamada por ronda | 5/5 | **ninguna (silencio)** |
| A7 texto en la ronda 5 | 4 rondas de tool + texto válido | 4+texto | texto aceptado (sin fallback innecesario) — **PASS** |
| C0 control | 1 búsqueda + texto | 1+texto | lista de opciones con USD y Bs — **PASS** |

---

## E. Hallazgos

### H-1 · CRÍTICO · El respaldo de argumentos vacíos no se `await`ea y revienta el turno
- **Archivo/función:** `app/turn.py:2744` en `_tool_loop` → `return _fallback_farmacia(user_text, runtime, ctx, farmacia)` (la función es `async def`, `app/turn.py:4110`).
- **Comportamiento actual:** al tercer lote de tool-calls con `{}` se devuelve un **objeto coroutine** en lugar de texto. El llamador (`app/turn.py:652`) lo asigna a `final_text` y las guardas lo tratan como texto: `guards.afirma_recibir_media` lanza `TypeError: expected string or bytes-like object, got 'coroutine'` (`app/guards.py:255`), el turno se aborta en `handle_flush` (`app/turn.py:242-244`, log `turno de … reventó — silencio`) y **el cliente no recibe nada**. `RuntimeWarning: coroutine '_fallback_farmacia' was never awaited`.
- **Comportamiento esperado:** la respuesta de respaldo (plantilla honesta o búsqueda directa del término) debe enviarse.
- **Reproducir:** sonda T-04, caso A3 (ver sección H).
- **Procedencia:** el `return` sin `await` entra con `ba7060a` ("endpoint /api/chat para el Laboratorio", autor Roberto, 2026-08-28), es decir es base heredada, no de las ramas del encargo.

### H-2 · CRÍTICO · Agotar `MAX_TOOL_ROUNDS` sin texto deja el turno en silencio
- **Archivo/función:** `app/turn.py:2868-2869` (`logger.warning("turno: demasiadas rondas … corto sin texto")` → `return None`) con el envío condicionado en `app/turn.py:1072-1073` (`if final_text and final_text.strip()`) y `runtime.pide_recomendacion` como único rescate (`app/turn.py:2862`).
- **Comportamiento actual:** cinco rondas de llamadas **válidas** sin texto final ⇒ `None` ⇒ **no se envía ningún mensaje**. No hay plantilla de respaldo en `run_turn` para este camino (todas las asignaciones de respaldo de `run_turn` — `app/turn.py:713-1055` — están condicionadas a `final_text` no vacío).
- **Impacto medido:** A4 lo lleva al cliente real: **pedir 6 medicamentos (más de 5 rondas) produce cero respuesta**, no "pérdida parcial de artículos".
- **Comportamiento esperado:** respuesta de respaldo útil y honesta construida con lo que sí se obtuvo.
- **Reproducir:** T-04, casos A1/A2/A4.

### H-3 · CRÍTICO · Bucle de re-fuerzo de búsqueda ⇒ silencio (interacción backstop ↔ puerta F15.1)
- **Archivo/función:** `app/turn.py:2684` (`if term and (not runtime.consulted_catalog or (runtime.med_not_found and not runtime.catalog_retried))`) + `app/turn.py:2698` (`catalog_retried = True`) frente a la puerta de autorización por término en `app/tools.py` (`_autorizar_busqueda`).
- **Comportamiento actual:** si el modelo responde **texto sin llamar la herramienta** y el término forzado **no queda autorizado** (capa de comprensión apagada, en fail-safe por `null`/`timeout`/JSON inválido, o rechazo conclusivo), `runtime.consulted_catalog` **nunca** pasa a `True`; como la condición depende de él, el backstop **re-fuerza el mismo término en cada ronda** (`catalog_retried` solo acota la rama de `med_not_found`) hasta agotar las 5 rondas → `None` → **silencio**.
- **Evidencia (logs de la sonda, capa desactivada):** `backstop: forzando buscar_medicamento('principio activo daflon') — consulted=False med_not_found=False` ×5 + `búsqueda NO autorizada por la capa: 'principio activo daflon' (acto=otro, consulta=None): capa desactivada` ×5 + `turno: demasiadas rondas de herramientas — corto sin texto`.
- **Riesgo:** pasa de "el agente pregunta en vez de buscar" (coste documentado del fail-closed) a **"el agente no dice nada"** — se pierde incluso la pregunta. Los tres casos B1/B7-7/B8-2 de T-06 mueren por esta vía, no por el veto G5.
- **Observación asociada (BAJO):** dos criterios en desacuerdo — `_termino_es_medicamento_plausible("principio activo daflon")` lo acepta para forzar la búsqueda, pero la puerta F15.1 lo rechaza. El desacuerdo es la raíz del bucle.
- **Comportamiento esperado:** un solo intento de búsqueda forzada; si no está autorizada, responder con la plantilla honesta/pregunta, nunca repetir la ronda.
- **Reproducir:** T-06, casos B1/B7-7/B8-2.

### H-4 · CRÍTICO · G5 no cubre la equivalencia genérica por otras redacciones
- **Archivo/función:** `app/guards.py:134-140` (`AFIRMA_COMPOSICION`) y la cadena de vetos en `app/turn.py:971-977`.
- **Detectadas:** "su principio activo es…", "tiene el mismo principio activo…", "es el genérico de…" (con esa forma exacta), "su ingrediente activo…", "contiene X".
- **NO detectadas (llegan al cliente tal cual):** "Es equivalente al Panadol", "Es equivalente a Panadol", "Panadol equivale al paracetamol", "Tiene el mismo efecto que el Panadol", "Se puede reemplazar por Panadol", "Es el mismo medicamento que el Panadol", "El genérico es Panadol" (sin "es el genérico **de**"), "Es el genérico **del** Panadol", "Lleva paracetamol", "Tiene paracetamol".
- **Impacto:** B2 y B7-5 se reproducen de punta a punta: la afirmación de equivalencia sin respaldo **sale enviada**.
- **Reproducir:** T-05 (tabla por frase) y T-06 (B2, B7-5).

### H-5 · CRÍTICO · G5 se desactiva con *cualquier* resultado de catálogo del turno
- **Archivo/función:** `app/turn.py:971-975`: `elif (not es_negativa_con_carrito and guards.afirma_composicion(final_text) and not runtime.last_products)`.
- **Comportamiento actual:** la única condición de evidencia es "el turno trajo productos", **no** "los productos respaldan esta afirmación concretа". Con resultados **irrelevantes** (B4) o **parciales** (B5) G5 queda apagado y la afirmación de composición/equivalencia del producto consultado **llega al cliente**.
- **Evidencia:** T-06 B4 (`busco daflon 500` → catálogo devuelve un producto distinto → "El Daflon tiene el mismo principio activo que el Panadol." enviado) y B5 (`busco losartan 50 mg` → "El losartan tiene el mismo principio activo que el Panadol." enviado).
- **Nota estructural:** el mismo patrón de puerta por turno gobierna G6 (`not runtime.last_products`) y G10a (`not runtime.consulted_catalog`, `app/turn.py:1023-1026`) → **B6 (varios medicamentos con evidencia dispar por producto) no puede cumplirse con este diseño**: la evidencia es del turno, no del producto.
- **Comportamiento esperado:** la evidencia debe evaluarse contra el **producto y la afirmación** (mapeo afirmación → producto del catálogo), no contra un booleano de turno.

### H-6 · MEDIO · La alternativa "composición" del regex de G5 es inerte
- **Archivo/función:** `app/guards.py:138` — alternativa `composici[oó]n\s+\w` dentro de un grupo cerrado por `\b` (`…|contiene\s+[a-záéíóúñ]{4,})\b`): tras `\w` el `\b` exige fin de palabra, así que solo podría casar una palabra de **una** letra.
- **Consecuencia:** "Su composición incluye paracetamol" y "La composición del Daflon es diosmina" **no se detectan** (ni con tilde ni sin ella). Verificado por sonda T-05 en ambos sentidos.
- **Corrección esperada:** quitar el `\b` final de esa alternativa (o moverla fuera del grupo) y añadir regresión con tildes.

### H-7 · MEDIO · Falsos positivos de G5 (bloqueos innecesarios)
- "¿Cuál es el principio activo que buscas?" → **vetado** por G5 y sustituido por la plantilla "No tengo información confirmada sobre la composición…" (T-06 B8-3). El agente **preguntando** composición queda silenciado por la guarda de afirmar composición.
- "No puedo confirmar el principio activo sin datos del catálogo." → detectado por G5 (T-05/T-06 B8-2); en la sonda no llegó a enviarse, pero por H-3, no por el veto.
- **Corrección esperada:** exigir modo afirmativo (no interrogativo/negado) antes de vetar.

---

## F. Regresiones

| Área | Estado | Evidencia |
|---|---|---|
| F15.1 (no revalidada por instrucción) | **Efecto lateral detectado, no regresión declarada** | H-3: la puerta F15.1 + backstop de búsqueda forzada convierten en silencio turnos que antes terminaban buscando. No se re-midió el corpus ni el Lab de F15.1; se señala como riesgo a evaluar cuando exista una rama real. |
| Búsqueda y catálogo | Sin regresión detectable en lo ejercitado | T-04 C0 y T-06 B9: lista de opciones con precio USD/Bs llega correcta |
| Selección y carrito | **NO ejercitado** | No se probó el flujo opción → `agregar_al_carrito` → resumen (fuera del alcance de una línea base sin rama); el pie de carrito sí aparece en las respuestas |
| Respuestas farmacológicas | **Huecos abiertos** | H-4, H-5, H-6 (afirmaciones sin respaldo llegan al cliente) |
| Turnos sin respuesta | **Regresión funcional de la base** | H-1, H-2, H-3 (tres caminos distintos al silencio) |

---

## G. Recomendación

| Rama | Decisión única |
|---|---|
| `fix/tool-loop-exhaustion` | **No debe integrarse** (no contiene cambios: nada que revisar; los defectos que debía cerrar — H-1, H-2, H-3 — siguen vivos y reproducidos en la base) |
| `fix/g5-composition-evidence` | **No debe integrarse** (no contiene cambios: nada que revisar; los defectos que debía cerrar — H-4, H-5, H-6, H-7 — siguen vivos y reproducidos en la base) |

Ninguna de las dos puede declararse PASS: un test no ejecutado (ni un árbol sin diff) no cuenta como PASS.

### Diagnóstico para el agente de desarrollo (no implementado aquí)

**Línea A (bucle):**
1. `app/turn.py:2744` — `await _fallback_farmacia(...)` (o devolver la plantilla) y prueba de regresión que verifique que el texto final es `str`.
2. `app/turn.py:2868` — sustituir `return None` por una respuesta de respaldo determinista (plantilla honesta + lista de lo obtenido), conservando el rescate por `pide_recomendacion`.
3. `app/turn.py:2684` — acotar el re-fuerzo: recordar el término ya forzado por turno (p. ej. reutilizar `catalog_retried` también cuando `consulted_catalog` sigue en `False`) y, si la puerta no autoriza, salir del bucle con la pregunta honesta. Alinear `_termino_es_medicamento_plausible` con `_autorizar_busqueda` para que no se contradigan.
4. Detección de llamadas degeneradas: hoy solo hay contador de lotes con `arguments` vacíos; A2 (misma llamada idéntica repetida) no se detecta (`app/turn.py:2736-2746`).

**Línea B (G5):**
1. Evidencia **por afirmación y por producto**, no por turno (`app/turn.py:974`): retirar `not runtime.last_products` como única condición y comparar la afirmación con los datos del producto devuelto por el catálogo.
2. Cubrir las redacciones de equivalencia (H-4) con lista explícita de patrones y regresión por frase, con y sin tildes.
3. Reparar la alternativa inerte de "composición" (`app/guards.py:138`).
4. Exigir modo afirmativo antes de vetar (H-7) y añadir los casos B8 a las regresiones.

---

## H. Cómo reproducir todo (coste 0, sin modelo)

```bash
REPO=D:/Proyectos/GenteFarma/agentNEA
PY=$REPO/.venv-test/Scripts/python.exe

# 0) línea base / árboles idénticos
cd $REPO
git fetch gentefarma && git ls-remote --heads gentefarma
git rev-parse desarrollo^{tree} gentefarma/fix/tool-loop-exhaustion^{tree} gentefarma/fix/g5-composition-evidence^{tree}
git diff --stat desarrollo gentefarma/fix/tool-loop-exhaustion      # vacío
git diff --stat desarrollo gentefarma/fix/g5-composition-evidence   # vacío

# 1) suite existente y compilación
$PY -m pytest tests/ -q
$PY -m py_compile app/*.py

# 2) sondas (LLM guionado determinista, catálogo de mentira; no tocan el CRM ni la red)
$PY eval/qa-2026-11-06/sonda_bucle.py     # A1 A2 A3 A4 A7 C0  -> sonda_bucle.json
$PY eval/qa-2026-11-06/sonda_g5.py        # B1 B2 B4 B5 B7 B8 B9 -> sonda_g5.json
$PY eval/qa-2026-11-06/sonda_guardas.py   # B7/B8 frase por frase -> sonda_guardas.txt
```

Las sondas no tocan el CRM, la red ni el modelo: corren el código real con un LLM guionado y un catálogo de mentira. Cookbook del arnés (contrato del stub de CRM, trampas que silencian el turno): skill `vocero-nea-qa`, `references/simulador-conversaciones.md` y `references/sonda-bucle-y-guards.md`.

---

## I. Lo que NO se ejecutó (y por qué)

- **Validación A y B contra las ramas:** imposible: árboles idénticos a la base (T-03).
- **Laboratorio (`/api/lab/runs`) y simulador con modelo real:** mediría la base, no una rama; además exige stack Docker + proxy LLM y consume modelo. No aporta al veredicto de las ramas y no se gastó.
- **B3 (evidencia explícita de composición):** el catálogo real es Firestore y no hay credenciales locales; el stub de la sonda no expone campo de composición. Requiere catálogo real o fixture con el esquema de producción.
- **A5/A6 (fallo de herramienta y resultados parciales):** no ejecutados en esta pasada (la sonda cubre A1–A4 y A7; A5/A6 quedan pendientes y se pueden añadir al mismo arnés).
- **A8/regresión de carrito:** no ejercitado (ver F).

---

## J. Artefactos

- `eval/qa-2026-11-06/sonda_bucle.py` · `sonda_bucle.json` — línea A (T-04)
- `eval/qa-2026-11-06/sonda_guardas.py` · `sonda_guardas.txt` — detectores de guardas (T-05)
- `eval/qa-2026-11-06/sonda_g5.py` · `sonda_g5.json` — G5 de punta a punta (T-06)
- Este informe: `docs/QA-2026-11-06-bucle-de-herramientas-y-g5.md`

Todo commiteado en `desarrollo` (solo estos paths; el árbol de trabajo tiene artefactos de otra sesión que NO se tocan). Los arreglos NO están implementados aquí: el encargo del QA prohibía modificar código. La orden de cierre para el codificador (3 paquetes: bucle, G5, G13) vive en `docs/ORDEN-CODIFICADOR-CIERRE-2026-11-06.md`.
