# TAREA PENDIENTE — Comprender la conversación ANTES de buscar, e inferir el medicamento desde la comprensión

**Fecha:** 2026-11-06 · **Estado:** propuesta (sin implementar) · **Prioridad:** alta (afecta casos básicos visibles al cliente)
**Dueño del requerimiento:** responsable del proyecto · **Relacionado:** F15/F15.1, hallazgo H-3 de `docs/QA-2026-11-06-bucle-de-herramientas-y-g5.md`

## Problema observado

El agente se equivoca en cosas básicas: **antes de comprender la conversación, se enfoca en responder por el medicamento**. Con frases que **no** contienen un medicamento, inventa una búsqueda de una palabra suelta que no es un fármaco (ej.: "No puede ser Indu." → "No tengo SER INDU"; "Vivo en Ciudad Bolívar." → consultas con 'bol'/'boli'; "principio activo daflon" → fuerza esa frase al catálogo).

## Raíz (ya diagnosticada)

Hay **dos criterios que deciden la búsqueda y se contradicen**:

1. **La capa de comprensión F15** — `app/tools.py::ToolRuntime._autorizar_busqueda` + `app/intencion.py::entender`. Decide **por término** si el mensaje trae una consulta de medicamento y **ya devuelve un término inferido** (`Entendimiento.termino`). Rechaza correctamente lo que no es consulta.
2. **El backstop de búsqueda forzada** — `app/turn.py::_tool_loop` (bloque `if term and not runtime.catalog_retried …`, ~línea 2690). Extrae el término con **heurística de regex** (`_extraer_termino_medicamento`) y lo valida con `_termino_es_medicamento_plausible`, que **acepta frases que la capa rechaza** (H-3: `_termino_es_medicamento_plausible("principio activo daflon")` → True, pero la puerta F15 lo niega). Cuando ambos discrepan, el backstop busca basura.

Además hay **atajos que saltan la capa**: `_tiene_evidencia_de_producto` (dosis/forma) autoriza la PRIMERA búsqueda del turno sin pasar por comprensión.

> Nota: el Paquete 1 (`fix/tool-loop-exhaustion`, D-A3) ya evitó que el backstop **re-fuerce en bucle** el mismo término no autorizado (antes = silencio). Esta tarea es el paso siguiente: que **no fuerce de entrada** un término que la comprensión no respalda, y que **infiera** el nombre correcto.

## Objetivo

1. **Comprender primero.** Ningún camino (ni el backstop, ni los atajos de dosis/forma) debe lanzar una búsqueda sin que la capa de comprensión haya decidido que el mensaje trae una consulta de medicamento.
2. **Inferir el medicamento desde la comprensión.** Usar `Entendimiento.termino` (el término que la capa deduce del mensaje del cliente) como **fuente autoritativa** del nombre a buscar, en vez de la extracción por regex. La regex queda solo como respaldo cuando la capa no concluye (fail-safe).
3. **Alinear los dos criterios.** `_termino_es_medicamento_plausible` no debe aceptar lo que `_autorizar_busqueda` rechaza (eliminar la contradicción de H-3).

## Enfoque propuesto (a validar en la iteración)

- Hacer que `_autorizar_busqueda` (o la capa) devuelva, además del veredicto, el **término inferido normalizado**; el `_tool_loop` busca con **ese** término, no con el heurístico.
- El backstop de búsqueda forzada se condiciona a `intent_hay_consulta is True` (comprensión afirmativa), no a `_termino_es_medicamento_plausible`.
- Revisar el atajo `_tiene_evidencia_de_producto`: mantenerlo solo cuando el término trae dosis/forma inequívoca; en la duda, que decida la capa.
- Cubrir con sondas deterministas (sin modelo) las frases sin medicamento (H-3) y los casos legítimos de consulta, midiendo 0 búsquedas inventadas y 0 consultas legítimas perdidas.

## Palanca de configuración (sin código) — HALLAZGO 2026-11-06

Estado medido en el repo (`.env.example` + defaults de `Settings`):

| Ajuste | Valor efectivo |
|---|---|
| `OPENAI_MODEL` (chat) | `gpt-4o-mini` (rápido, **no razonador**) |
| `intent_layer` | **ON** (default True; no está en `.env`) |
| `intent_model` | **vacío** → la capa F15 decide con el **mismo `gpt-4o-mini`** |

**Causa principal probable del síntoma:** la capa de comprensión está encendida, pero **razona con `gpt-4o-mini`**, demasiado débil para clasificar frases conversacionales ambiguas e inferir el término. La heurística con fugas (`_termino_es_medicamento_plausible` devuelve True para `ser indu`, `duele cabeza`, `principio activo del daflon`, `ahora`, `aceptan zelle`) solo entra cuando la capa NO concluye (timeout/fail-safe), pero el grueso de los errores viene del modelo de la capa.

**Primer experimento recomendado (cero código, reversible):** darle a la capa su propio modelo más capaz sin tocar la latencia del chat:
```env
INTENT_MODEL=<modelo más fuerte>     # gpt-4o o uno vía OpenRouter
INTENT_API_KEY=<si va en otro proveedor>
INTENT_BASE_URL=<si va en otro proveedor>
```
La capa solo devuelve un JSON de veredicto dentro de `INTENT_TIMEOUT` (12 s); puede permitirse un modelo mejor sin el gotcha del razonador que afecta al chat. Medir si bajan las búsquedas inventadas ANTES de refactorizar el código afinado de F15.

## Reglas del proyecto a respetar

- Cambios aditivos y quirúrgicos; **no** reimplementar F15.1 ni añadir una segunda puerta de autorización (la única sigue siendo `_autorizar_busqueda`).
- No debilitar vetos ni la puerta para que algo "pase".
- Validar con sondas deterministas (coste 0); el modelo real / Laboratorio los corre el responsable.
- No tocar dominio farmacéutico, carrito, precios, OCR ni agenda salvo lo que exija esta corrección.

## Criterio de terminado (borrador)

- Las frases sin medicamento **no** generan ninguna consulta al catálogo (sonda determinista).
- Las consultas legítimas se buscan con el término **inferido por la comprensión**, no por regex.
- `_termino_es_medicamento_plausible` y `_autorizar_busqueda` **no** se contradicen en el corpus de H-3.
- F15.1 sigue en 0 falsos positivos / 0 falsos negativos (verificación con modelo, responsable).
