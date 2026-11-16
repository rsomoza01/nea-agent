# Cierre de la versión — integración de los paquetes de QA (2026-11-09)

**Rama de trabajo:** `agent-qa` · **Base de partida:** `eeee17e` (incluye la portación del autor original
documentada en `docs/VALIDACION-INTEGRACION-AUTOR-QA.md`) · **Destino:** `desarrollo` (espejo y cliente).

Este documento registra **qué se integró, en qué orden, con qué evidencia y qué se dejó fuera**, para que
nadie vuelva a abrir la decisión desde cero.

---

## 1. Orden de integración y por qué ese orden

Se integró **de a un paquete por merge**, validando después de cada uno (`py_compile` + suite + sonda).

| # | Rama | Qué cierra | Por qué en esta posición |
|---|---|---|---|
| 1 | `fix/tool-loop-exhaustion` | **P1** — ningún turno de farmacia termina en silencio (D-A1..D-A4) | Es el único defecto que deja al cliente **sin ninguna respuesta**. Va primero y aislado: si algo se rompe después, se sabe que no es de aquí. |
| 2 | `fix/g5-composition-evidence` | **P2** — G5 con evidencia por (afirmación, producto), fail-closed | Segundo por severidad: hoy afirmaba composición/equivalencia **sin respaldo**, y vetaba preguntas honestas. Toca `guards.py`, así que va antes de añadir el veto nuevo de P3 (que se apila al final de la cadena). |
| 3 | `fix/g13-afirmacion-sin-accion` | **P3** — veto de afirmar una acción que no ocurrió | **Chasis** (`guards.py` + `elif` en la cadena de vetos). Autorizado explícitamente en `docs/ORDEN-CODIFICADOR-CIERRE-2026-11-06.md` §autorización. Va al final de la cadena de vetos para no reordenarla dos veces. |
| 4 | `fix/comprension-antes-de-busqueda` | Comprender → inferir el medicamento → buscar; las meta-frases («principio activo del daflon») ya no se fuerzan al catálogo | Última porque es la que **más superficie mueve** en la puerta de búsqueda (`_buscar_medicamento`). Validarla sobre el árbol final es lo que importa. |
| 5 | *(higiene)* | Test de regresión de PDF (`tests/test_media_pdf.py`) | Cierra el hallazgo H-05 del informe de QA: la extracción de PDF no tenía ninguna prueba. |

## 2. Evidencia antes / después (sondas reales, coste 0)

Las sondas de `eval/qa-2026-11-06/` son los instrumentos de medición del proyecto y **no se tocaron**.

### Bucle de herramientas (`sonda_bucle.py`)

| Caso | Antes (`eeee17e`) | Después |
|---|---|---|
| A1 agotamiento normal | **silencio** (0 respuestas) | responde |
| A2 llamadas repetitivas | **silencio** | responde |
| A3 argumentos vacíos | **silencio** | responde |
| A4 seis medicamentos | **silencio** | responde |
| A7 texto en la ronda 5 | responde | responde |
| C0 control flujo normal | responde | responde |

### G5 (`sonda_g5.py`)

| Caso | Antes | Después |
|---|---|---|
| B1 composición sin evidencia | bloqueado | bloqueado |
| B2 «Es equivalente a Panadol» | **pasaba tal cual** | **bloqueado** |
| B4 resultados irrelevantes | **pasaba tal cual** | **bloqueado** |
| B5 evidencia parcial (precio sí, composición no) | **pasaba tal cual** | **bloqueado** |
| B7-5 «es equivalente a» / B7-7 «su composición incluye» | **no se detectaban** | **bloqueados** |
| B8-2 limitación honesta | **sin respuesta** (falso positivo) | pasa tal cual |
| B8-3 el agente pregunta por el principio activo | **bloqueado** (falso positivo) | pasa tal cual |
| B9 info comercial del catálogo | pasaba | pasa (no interfiere) |

### G13 (`detector_g13.py`)

12 textos reales del agente, **0 coincidencias** → el veto entra sin falsos positivos sobre lo ya hablado.
El veto exige **señal de estado** (handoff real, pedido registrado, add al carrito): la frase sola no dispara.

### F15.1 — autorización por término (protección que NO se debía tocar)

| Sonda | Antes | Después |
|---|---|---|
| `probe_autorizacion_por_termino.py` (determinista) | 10 casos · **0 huecos** | 10 casos · **0 huecos** |
| ídem con modelo real (`deepseek/deepseek-v4.1-flash`) | 17 casos · **0 huecos** | 17 casos · **0 huecos** |

### F14 — precisión de búsqueda

`eval/f14-busqueda/simular_casos.py` sobre el árbol integrado vs el árbol base `0f09e0a3`:
**salida idéntica byte a byte** (extracción de términos, listas, opciones, filtrado de relevancia).

### Suite completa

| Punto | Tests verdes |
|---|---|
| `agent-qa` de partida (`eeee17e`) | 178 |
| + P1 | 187 |
| + P2 | 219 |
| + P3 | 231 |
| + comprensión antes de buscar | 246 |
| + regresión de PDF | **252** |

---

## 3. Qué se dejó FUERA y por qué

| Rama | Decisión | Motivo (verificado) |
|---|---|---|
| `fix/f15-fail-closed` (`238ad7a`) | **No integrar** | Superada: es el intento anterior de la autorización **por término**, ya implementada de forma más completa en F15.1 (`20dda54`, `5b7815e`: `intent_terminos_autorizados`, `_clave_termino`, `_mismo_producto`, `intent_rechazos`, `candidato_obligatorio`). El merge **da conflicto** en `app/tools.py` y su contenido retrocedería la puerta actual. Verificado con la sonda: 0 huecos hoy. |
| `fix/f15-term-scoped-search` (`9bab279`) | **No integrar** | Contiene a la anterior + «reclasificar cada término nuevo» y «registrar rechazos»: eso **es** F15.1, ya integrado y validado con 17 casos. Merge con conflicto en `app/tools.py` y `tests/test_f15_intencion.py`. |
| `fix/integracion-autor-auditoria` (`b85a58b`) | **No integrar** | **No trae código**: es otro documento con el **mismo nombre** (`docs/INTEGRACION-AUTOR-ORIGINAL.md`, 173 líneas) que el ya integrado (139 líneas). Mergearlo produce un add/add contra el existente. Si aporta algo, se absorbe a mano en el doc actual; no como rama. |
| `upstream/fix/032-seguimiento-permitidos` | **No integrar (fuera de alcance)** | Tema WhatsApp/seguimiento (`ALLOWED_WA_IDS`), no farmacia; toca `turn.py`/`sender.py`/`followup.py`. Decisión de producto aparte. |
| `upstream/fix/conflicto-409-anidado` | **No integrar (fuera de alcance)** | Agenda/CRM (`slot_taken`), no farmacia. |
| `upstream/fix/zoom-reminder-confirmation` | **No aplicable** | Depende de `app/crm_brains.py`, que no existe en este árbol. |

`upstream/main` sigue en `ad89138` — **sin commits nuevos** que integrar.

---

## 4. Pendientes que quedan abiertos (nada de esto bloquea la rama)

1. **Hints LLM** (`LLM_REASONING_EFFORT` / `LLM_PROVIDER_SORT`): **apagados**. `provider.sort` lo rechaza el
   proveedor actual; `reasoning=minimal` se acepta pero no hay A/B conversacional. Hallazgos H-01/H-02/H-03 del
   informe de QA siguen abiertos (solo se trabajan si se decide encenderlos).
2. **Validación con Docker** (bloqueada por daemon caído): Laboratorio/DB desechable, simulador F14
   conversacional y camino real por `/chat`. Necesaria **antes de llevar esto a producción**, no para `agent-qa`.
3. **Corpus F15**: se re-mide por corrida; el objetivo 50/50 no se reproduce hoy con este modelo en ningún árbol
   (49/50 en ambos). No es regresión: la capa no se tocó.
4. **G13** entra al chasis: si produce algún falso positivo en producción, se revierte ese paquete con
   `git revert <merge de P3>` sin tocar P1/P2.

---

## 5. Reproducción

```bash
PY=D:/Proyectos/GenteFarma/agentNEA/.venv-test/Scripts/python.exe

$PY -m py_compile app/*.py
$PY -m pytest tests/ -q                                   # 252 passed
$PY eval/qa-2026-11-06/sonda_bucle.py                     # A1..A4 responden
$PY eval/qa-2026-11-06/sonda_g5.py                        # B2/B4/B5/B7-5/B7-7 bloqueados; B8-2/B8-3 pasan
$PY eval/g13-afirmacion-sin-accion/detector_g13.py        # 0 falsos positivos
$PY eval/f15-frases-conversacion/probe_autorizacion_por_termino.py            # 10 casos, 0 huecos
$PY eval/f15-frases-conversacion/probe_autorizacion_por_termino.py --model deepseek/deepseek-v4.1-flash  # 17 casos, 0 huecos
$PY eval/f14-busqueda/simular_casos.py --root . --out /tmp/f14.json          # comparar vs árbol base
```

**Ojo al medir:** las sondas de `eval/qa-2026-11-06/` **fijan** `REPO = D:/Proyectos/GenteFarma/agentNEA` en
`sys.path`. Si se ejecutan desde un worktree diferente, miden el árbol del repo principal, no el worktree.
Por eso todo el cierre se validó en el repo principal.
