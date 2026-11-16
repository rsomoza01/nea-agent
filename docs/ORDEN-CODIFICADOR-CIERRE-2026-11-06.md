# ORDEN AL CODIFICADOR — cierre de los pendientes de la versión

**Fecha:** 2026-11-06 · **Repo:** `D:\Proyectos\GenteFarma\agentNEA` · **Base:** HEAD de `desarrollo`
**Entrega:** **3 paquetes, uno por rama**. No se mezclan paquetes en una misma rama.

> **Autorización incluida en esta orden:** tocar el chasis (`app/guards.py` + un `elif` en la cadena de
> vetos de `app/turn.py`) **solo** para el veto G13 del Paquete 3, tal como pide
> `docs/SPEC-G13-afirmacion-sin-accion.md` §6. Nada más del chasis se toca. Si el responsable del
> proyecto retira §PAQUETE 3, los paquetes 1 y 2 siguen igual.

---

## 0. Reglas del proyecto (no negociables)

- Cambios **aditivos** y quirúrgicos; nada de refactors de vecindad ni dependencias nuevas.
- **No** reimplementar F15.1 ni añadir una segunda puerta de autorización de búsqueda: la única puerta es
  `tools._autorizar_busqueda` (decide **por término**). Tu trabajo no la cambia (Paquete 2 es un **veto**,
  no una autorización).
- **No debilitar ningún veto** para que una respuesta "pase" (ni relajar G5, ni ampliar bypasses, ni borrar patrones).
- **No** modificar ni borrar tests existentes. **No** tocar el dominio farmacéutico, el carrito comercial,
  precios, OCR ni el flujo de agenda.
- **No** tocar `main`/`master`, ni `upstream` (kevinrivm), ni los remotos del cliente, ni borrar/reescribir
  ramas de otros. Entrega **tu rama** en el espejo (`gentefarma`); el merge a `desarrollo` lo hace el
  responsable tras verificarlo.
- Los **instrumentos de medición no se tocan**: las sondas de `eval/qa-2026-11-06/` y las de fases
  anteriores son la vara con la que se te va a medir. Si crees que una está mal, dilo en el reporte y no la edites.
- Un caso **no ejecutado** no puede declararse PASS. Nada de "debería funcionar".

---

## 1. Antes de escribir código: mira el fallo con tus ojos (coste 0)

```bash
cd D:/Proyectos/GenteFarma/agentNEA
PY=D:/Proyectos/GenteFarma/agentNEA/.venv-test/Scripts/python.exe   # venv de tests del repo

$PY -m pytest tests/ -q            # hoy: 166 passed
$PY -m py_compile app/*.py

$PY eval/qa-2026-11-06/sonda_bucle.py      # Paquete 1: A1 A2 A3 A4 salen SIN RESPUESTA
$PY eval/qa-2026-11-06/sonda_g5.py         # Paquete 2: B2 B4 B5 B7-5 salen TAL CUAL; B8-3 falso positivo
$PY eval/qa-2026-11-06/sonda_guardas.py    # Paquete 2: cobertura frase por frase del detector
$PY eval/g13-afirmacion-sin-accion/detector_g13.py   # Paquete 3: medición previa exigida por el spec
```

Lee antes de empezar: `docs/QA-2026-11-06-bucle-de-herramientas-y-g5.md` (diagnóstico, evidencia y
matriz de pruebas) y `docs/SPEC-G13-afirmacion-sin-accion.md` (diseño del Paquete 3).
Las líneas citadas abajo son del commit `5b7815e`: si el HEAD cambió, localiza por el fragmento de código.

Los guiones de prueba son **deterministas y sin modelo real**: un LLM guionado devuelve exactamente la
respuesta a medir y un CRM de mentira sirve el catálogo. Lo que la sonda captura en `ctx.lab_outbox` es lo
que el cliente habría recibido. **No uses el modelo real ni Docker para validar** (regla de coste del proyecto).

---

## PAQUETE 1 — `fix/tool-loop-exhaustion`
### Objetivo: *ningún turno de farmacia termina sin un mensaje para el cliente*

| ID | Defecto (archivo:línea en `5b7815e`) | Hoy | Esperado |
|---|---|---|---|
| D-A1 | `app/turn.py`, en `_tool_loop`, tras `empty_rounds >= 3`: `return _fallback_farmacia(user_text, runtime, ctx, farmacia)` — y `_fallback_farmacia` es `async def` | devuelve una **coroutine**; `guards.afirma_recibir_media` lanza `TypeError: expected string or bytes-like object, got 'coroutine'`; `handle_flush` registra `turno … reventó — silencio`; el cliente no recibe nada | se envía el respaldo; el valor que devuelve `_tool_loop` es `str` no vacío |
| D-A2 | `app/turn.py`, final de `_tool_loop`: `logger.warning("turno: demasiadas rondas de herramientas — corto sin texto")` → `return None`; y en `run_turn` el envío exige `final_text` no vacío | 5 rondas de llamadas **válidas** sin texto ⇒ silencio (pedir 6 medicamentos = cero respuesta) | respaldo determinista: si hay `runtime.last_products`, la lista real (USD/Bs) + `MENSAJE_SUGERIDO_CARRITO`; si no, la limitación honesta, **sin** inventar precio/stock/disponibilidad |
| D-A3 | `app/turn.py`, backstop de búsqueda forzada: `if term and (not runtime.consulted_catalog or (runtime.med_not_found and not runtime.catalog_retried)):` → `catalog_retried = True` → `execute("buscar_medicamento", …)` | si el modelo responde texto y el término forzado **no está autorizado** (capa off / fail-safe / rechazo conclusivo), `consulted_catalog` nunca pasa a `True` y el backstop **re-fuerza cada ronda** hasta agotar → silencio | **un solo intento** por término y turno; si la puerta no autoriza, se sale del bucle con la respuesta honesta que F15.1 prevé (preguntar el medicamento / reconocer la limitación) |
| D-A4 | `app/turn.py`, contador `empty_rounds` (solo cubre `arguments` vacíos) | repetir la **misma** llamada con los **mismos** argumentos no se detecta: quema rondas y cae en D-A2 | detectar la repetición degenerada y salir **con respuesta** |

- **No cambies `MAX_TOOL_ROUNDS`** para "arreglar" el agotamiento: debe haber respuesta también cuando el límite se agota.
- Conserva el rescate actual por `runtime.pide_recomendacion` **antes** del respaldo nuevo.
- Reutiliza lo que ya existe (`_fallback_farmacia`, `_formatear_lista_productos`, plantillas de respaldo); no crees un cuarto mecanismo.
- **No toques `app/guards.py` en este paquete.**
- **Falso positivo a evitar (crítico):** una secuencia **legítima** de búsquedas distintas (un medicamento por ronda y luego el texto) **no** es un bucle y no debe cortarse. Criterio sugerido: solo cuentan como degeneradas las llamadas **idénticas** (misma herramienta + argumentos normalizados) repetidas **≥3 veces consecutivas**.

### Tests obligatorios — `tests/test_tool_loop_respaldo.py` (+ arnés `tests/harness_agente_p1.py`)

Copia el arnés de `eval/qa-2026-11-06/sonda_bucle.py` (contrato del `CrmStub` incluido: `get_context` con
`providerId`, `conversation.id`, `aiEnabled`, `windowOpen`; `get_products` con la clave `producto`).
Usa un nombre de arnés **propio de este paquete** para que las tres ramas se puedan mergear sin conflictos.

| ID | Cliente | Guion del modelo | Aserción |
|---|---|---|---|
| A1 | `busco losartan 50 mg` | 6 búsquedas válidas distintas, nunca texto | hay **una** respuesta no vacía; menciona lo obtenido |
| A2 | `busco losartan 50 mg` | 6 búsquedas **idénticas** | hay respuesta (corte por degeneración) |
| A3 | `busco losartan 50 mg` | 6 llamadas con `arguments={}` | hay respuesta; **no** hay excepción; el texto final es `str` |
| A3b | idem | idem | `handle_flush` no registra `reventó` (captura el log) |
| A4 | 6 medicamentos en un mensaje | 1 búsqueda válida por ronda | hay respuesta; ningún medicamento obtenido se pierde del texto |
| A5 | `busco losartan 50 mg` | búsqueda + el catálogo devuelve error/timeout | no se inventa precio, stock ni disponibilidad; se comunica la limitación |
| A6 | idem | 2 búsquedas OK + 1 fallida | lo respaldado se conserva; lo fallido se declara |
| A7 | `busco losartan` | 4 rondas de tool + texto válido en la 5ª | se acepta el texto del modelo; **no** se sustituye por respaldo |
| A8 | flujo normal (buscar → presentación → precio → carrito) | guion normal | sin interferencia del mecanismo de protección |
| A9 | D-A3 | término no autorizado + el modelo responde texto | el backstop fuerza **una** vez; no se repite la ronda; hay respuesta |

---

## PAQUETE 2 — `fix/g5-composition-evidence`
### Objetivo: *ninguna afirmación de composición, principio activo o equivalencia genérica llega al cliente sin respaldo del catálogo para ESE producto* — sin bloquear la información comercial respaldada

| ID | Defecto | Hoy | Esperado |
|---|---|---|---|
| D-B1 | `app/turn.py`, veto G5: `elif (not es_negativa_con_carrito and guards.afirma_composicion(final_text) and not runtime.last_products):` — la evidencia es del **turno** | cualquier resultado del catálogo apaga la guarda: con resultados **irrelevantes** (B4) o **parciales** (B5) la afirmación **sale enviada** | evidencia por **(afirmación, producto)**: solo se permite la afirmación si los datos del producto devuelto por la herramienta la respaldan. **Fail-closed**: si el payload no expone composición, no se permite ninguna afirmación de composición—el catálogo de hoy (`producto`, `nombre`, `precio`, `precioBs`, `presentacion`, `productId`, `laboratorio`) **no** respalda composición |
| D-B2 | `app/guards.py`, `AFIRMA_COMPOSICION` (detector de G5) | detecta "su principio activo es…", "tiene el mismo principio activo…", "es el genérico **de**…", "su ingrediente activo…", "contiene X". **No** detecta: "es equivalente a…", "equivale a…", "es el genérico **del**…", "el genérico es…", "tiene el mismo efecto que…", "se puede reemplazar por…", "es el mismo medicamento que…", "lleva X", "tiene X" | cubrir todas las variantes afirmativas de composición/equivalencia |
| D-B3 | `app/guards.py`, alternativa `composici[oó]n\s+\w` dentro de un grupo cerrado por `\b` | inerte (solo casaría una palabra de **una** letra): "su composición incluye X" y "la composición del Daflon es Y" no se detectan | patrón corregido y **normalización de tildes** en el detector (los modelos escriben "composicion" y "composición") |
| D-B4 | mismo detector | el agente **preguntando** "¿Cuál es el principio activo que buscas?" es vetado; también "No puedo confirmar el principio activo sin datos del catálogo." | exigir **modo afirmativo** (excluir interrogativas y negaciones/limitaciones honestas) |

- **Conserva la información comercial respaldada** al sustituir el texto (precio/presentación/opciones ya listadas): el veto no puede vaciar la respuesta.
- Revisa con el mismo criterio las afirmaciones que hoy dependen de booleanos de turno (`not runtime.last_products` en G6, `not runtime.consulted_catalog` en G10a): como mínimo documéntalo y cúbrelo con tests; **no lo empeores**.
- **No conviertas G5 en un bloqueo indiscriminado**: B3 y B9 deben quedar verdes.
- Esto es un **veto**, no una autorización: no toques la puerta de búsqueda.

### Tests obligatorios — `tests/test_g5_evidencia.py` (+ arnés `tests/harness_agente_p2.py`)

El arnés debe poder servir payloads **con** y **sin** campo de composición (así se mide "con evidencia" vs "sin evidencia").

| ID | Cliente | Evidencia del catálogo | Texto del modelo | Aserción |
|---|---|---|---|---|
| B1 | "¿qué principio activo tiene el daflon?" | ninguna herramienta | "Su principio activo es la diosmina." | no se envía principio activo |
| B2 | "¿cuál es el genérico del panadol?" | ninguna | "Es equivalente al Panadol, mismo efecto." | no se envía equivalencia |
| B3 | idem | payload **con** composición que sí respalda | "Su principio activo es X" | se permite **solo** lo respaldado |
| B4 | "busco daflon 500" | productos irrelevantes | "El Daflon tiene el mismo principio activo que el Panadol." | sigue bloqueado |
| B5 | "busco losartan 50 mg" | precio sí, composición no | "El losartan tiene el mismo principio activo que el Panadol." | bloqueado; el precio/lista respaldados se conservan |
| B6 | dos medicamentos | evidencia de uno solo | afirma composición de **los dos** | se bloquea solo el no respaldado |
| B7 | variantes | ninguna | las 6+ redacciones de D-B2 | todas bloqueadas |
| B8 | variantes | ninguna | pregunta / negación / limitación honesta | **ninguna** bloqueada |
| B9 | comercial | catálogo con datos | búsqueda, precios, disponibilidad, presentación | G5 no interfiere; texto intacto |

---

## PAQUETE 3 — `fix/g13-afirmacion-sin-accion` (rama **nueva**, desde `desarrollo`)
### Objetivo: *el agente no afirma una acción que no ocurrió*

Sigue **al pie de la letra** `docs/SPEC-G13-afirmacion-sin-accion.md` (ya medido: 1 positivo real, 0 falsos
positivos en 66 textos del agente). Caso que lo motiva:

```
cliente : Sí, eso es todo, finaliza mi pedido
agente  : Listo, quedó registrado.  👋     ← carrito VACÍO y sin llamada a finalizar_pedido
```

Resumen del diseño a implementar:
1. Detector léxico en `app/guards.py` junto a los demás, familias `pedido_registrado` /
   `agregado_al_pedido` / `apartado_o_agendado`, **con guarda de negación** (`no puedo`, `no se puede`,
   `no hay`, `no quedó`, `no logré`, `no alcancé`).
2. Señal de estado **aditiva** en `app/tools.py`: `runtime.pedido_finalizado` (True solo si
   `_finalizar_pedido` devuelve `ok`) y `runtime.carrito_agregado_turno` (True en `_agregar_al_carrito`
   **y** en el add del backstop).
3. Disparo = frase de la familia **Y** señal en `False`. La frase sola **nunca** dispara.
4. Colocación: `elif` **tras G12**, con plantilla constante `TPL_G13_ACCION` en `guards.py` (la del spec),
   sin inventar datos. Log: `logger.warning("guarda G13: afirmó una acción que no ocurrió …")`.
5. **No** juzga escalado (es G11), ni promesas de futuro, ni precios/stock/composición (G5/G6/G10).

### Tests obligatorios — `tests/test_g13_accion.py` (+ arnés `tests/harness_agente_p3.py`)

- Detector sobre los textos reales de las dos corridas del Laboratorio + negaciones + casos legítimos:
  **1 positivo, 0 falsos positivos**; las 2 frases de `pide_humano` (handoff real) **no** disparan.
- Escenario tipo `comprador_decidido` con **carrito vacío** y el modelo diciendo "Listo, quedó registrado.":
  el texto final **no** afirma el registro.
- El mismo texto con `pedido_finalizado=True`: **no** se veta.
- "No puedo cerrar el pedido con el carrito vacío": **no** se veta.

---

## 4. Definición de terminado (por paquete y global)

**Por paquete (en tu rama):**
1. `PY -m pytest tests/ -q` → **todo verde** (166 existentes + los nuevos).
2. `PY -m py_compile app/*.py` → OK.
3. Las sondas correspondientes **después** del cambio:
   - P1: A1, A2, A3, A4 pasan de "silencio" a **respuesta enviada** (A3 además sin excepción); A7 y C0 siguen enviando texto.
   - P2: B2, B4, B5, B7-5 dejan de llegar tal cual; B8-3 deja de ser falso positivo; B9 sigue tal cual.
   - P3: el detector da 1 positivo / 0 falsos positivos y el caso del carrito vacío no afirma registro.
4. Evidencia **commiteada en la rama**: `eval/qa-2026-11-06/RESULTADO-P1.md` (o `-P2`, `-P3`) con la salida
   **literal** de las sondas y de `pytest` (**antes y después**) y la tabla de casos con PASS/FAIL.
5. Commit(s) pequeños con mensaje en español y prefijo del repo (`fix(…)`, `test(…)`, `eval(…)`).
   Push **solo de tu rama** al espejo `gentefarma`. **No** mergees a `desarrollo` (lo verifica otro).

**Global (lo comprueba el responsable al mergear los tres paquetes):** los tres conjuntos de tests + los 166
previos en verde, las tres sondas con los casos críticos resueltos, la sonda de F15.1
(`eval/f15-frases-conversacion/probe_autorizacion.py`) **sin cambios** en su veredicto (0 falsos positivos /
0 falsos negativos) — el cierre no puede reabrir F15.1 — y ningún caso nuevo de "el cliente se queda sin respuesta".

## 5. Qué reportar de vuelta

Por paquete: rama y SHA(s) de commit; qué defecto (`D-A*/D-B*`, G13) cierra cada cambio con archivo y función;
la tabla de casos con el resultado real; el riesgo que introduce tu cambio; **qué no ejecutaste y por qué**; y
cualquier discrepancia con este documento o con las sondas. Si un caso no pasa, no lo maquilles: repórtalo.

## 6. Fuera de esta orden (no lo toques)

- `merge-develop` con el autor original (decisión del responsable, no del codificador).
- Los 2 rojos del Laboratorio que son del **cliente** (`fuera_de_kb` → `app/turn.py:607/614`, commit
  `732cf1e`; `receta_foto` → OCR) y cualquier push a sus repos.
- Credenciales de Firebase (catálogo real), `VISION_*` y despliegues.
- Scripts de Laboratorio/A-B (`eval/lab-ab-f151*`) — hay otra sesión usándolos.
