# Matriz de pruebas de la línea base — Paso 1

> Complementa [`BASELINE.md`](./BASELINE.md). HEAD `c80f90e` (= `agent-qa` @ `9715eee` en código).
> Entorno: `.venv-test` (Python 3.11.16), pytest 9.1.1, pypdf 6.21.4, Windows 11.
> Variables de entorno de la corrida: `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=.`
> Estados: **PASS / FAIL / BLOCKED / NOT RUN**. No hay FAIL.

## Matriz

| # | Prueba | Comando | Resultado | Evidencia | Tipo |
|---|---|---|---|---|---|
| 1 | Compilación | `python -m py_compile app/*.py` | **PASS** | `PY_COMPILE_OK` (sin error) | determinista |
| 2 | Suite completa | `python -m pytest tests/ -q` | **PASS** | `252 passed in 18.13s` | determinista |
| 3 | P1 — bucle de herramientas | `python eval/qa-2026-11-06/sonda_bucle.py` | **PASS** | A1/A2/A3/A4/A7/C0 → `respuestas=1` cada uno; ningún silencio. A1-A4 usan respaldo determinista (log `WARNING … respaldo determinista en vez de silencio`) | determinista |
| 4 | P2 — G5 evidencia | `python eval/qa-2026-11-06/sonda_g5.py` | **PASS** | Bloqueados: B1, B2, B4, B5, B7-7, B7-5. Pasan (sin veto): B8-2, B8-3, B9 | determinista |
| 5 | P3 / G13 — afirmación sin acción | `python eval/g13-afirmacion-sin-accion/detector_g13.py` | **PASS** | 12 textos del agente, **0 coincidencias** en las 4 señales (`escalado_ya_avisado`, `pedido_registrado`, `agregado_al_pedido`, `apartado_o_agendado`) → 0 falsos positivos | determinista |
| 6 | G6 — importe respaldado por lista | `python tests/test_g6_lista_mostrada.py` | **PASS** | `15/15 OK` (ejecutado como script; pytest no lo colecta: tiene `main()`, no funciones `test_`) | determinista |
| 7 | F15.1 — autorización por término (sin modelo) | `python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` | **PASS** | `PRUEBAS: 10   HUECOS: 0`. Segunda puerta (`sugerir_generico` sin autorización) → `NO consultó` el catálogo | determinista |
| 8 | F15.1 — autorización por término (modelo real) | `OPENAI_BASE_URL=http://127.0.0.1:11500/v1 python … probe_autorizacion_por_termino.py --model deepseek/deepseek-v4.1-flash` | **PASS** | `PRUEBAS: 17   HUECOS: 0`. Candidato ruidoso (`oiga`, `firma`, `maria fernandez cmp 45231`) → NO PASA; typo del mismo fármaco (`fexofenadina`) → PASA | modelo real |
| 9 | F14 — precisión de búsqueda (determinista) | `python eval/f14-busqueda/simular_casos.py --root . --out <tmp>/f14-baseline.json` | **PASS** | JSON producido con claves `extraccion`, `relevancia`, `alias`, `matriz_typo`. Línea base de referencia para comparaciones futuras | determinista |
| 10 | Escenarios P1/P2/P3 | `python tests/harness_agente_p{1,2,3}.py` | **PASS** | `exit=0`, sin salida — son módulos de escenarios (sin bloque `__main__`); importan y ejecutan limpio | determinista |
| 11 | F14 conversacional (catálogo) | `python eval/f14-busqueda/sonda_catalogo.py` | **BLOCKED** | `KeyError: 'CRM_BASE_URL'` — exige CRM real (`CRM_BASE_URL`/`CRM_BOT_API_KEY`), no disponible en local | requiere CRM |
| 12 | Integración Docker / DB desechable | — | **BLOCKED** | `docker ps` → `failed to connect to the docker API … daemon is running` (daemon caído) | requiere Docker |
| 13 | F14 conversacional (simulador) | `eval/f14-busqueda/simular_conversaciones.py` | **BLOCKED** | Depende del contenedor `nea` + catálogo inyectado (Docker caído) | requiere Docker |
| 14 | Corpus F15 (50 frases) | `eval/f15-frases-conversacion/medir_corpus.py` | **NOT RUN** | Fuera del alcance del Paso 1; escribe JSON dentro de `eval/` versionado. Documentado como 49/50 ruidoso en el cierre | modelo real |

## Conteos por archivo de prueba (pytest, informativo)

| Archivo | Resultado |
|---|---|
| `tests/test_f14_busqueda.py` | 102 passed |
| `tests/test_f15_intencion.py` | 64 passed |
| `tests/test_g5_evidencia.py` | 32 passed |
| `tests/test_g13_accion.py` | 12 passed |
| `tests/test_media_pdf.py` | 6 passed |
| `tests/test_tool_loop_respaldo.py` | 9 passed |
| `tests/test_comprension_termino.py` | 15 passed |
| `tests/test_equivalencia_y_forma.py`, `test_familia_pedida.py`, `test_llm_provider_hints.py` | incluidos en los 252 |
| `tests/test_g6_lista_mostrada.py` | **0 colectados** por pytest (es script, ver fila 6) |
| **Suite completa** | **252 collected · 252 passed** |

## Estado de infraestructura (comprobado en la corrida)

```text
docker ps        → failed to connect to the docker API … daemon is running   (CAÍDO)
curl /v1/models  → HTTP 501
curl POST /v1/chat/completions (deepseek/deepseek-v4.1-flash) → HTTP 200       (VIVO)
```

## Clasificación de cada prueba por dependencia

- **Determinista (sin red, sin modelo, sin servicios):** filas 1–7, 9, 10 y todos los conteos pytest.
- **Requiere modelo real (proxy LLM):** fila 8 (ejecutada, PASS) y fila 14 (no ejecutada).
- **Requiere Docker:** filas 12, 13 (BLOCKED).
- **Requiere CRM:** fila 11 (BLOCKED).
- **Requiere DB/secretos:** cubierto por las filas Docker/CRM; no se usaron datos reales ni credenciales de producción.

## Concordancia con el cierre documentado

Todos los números deterministas coinciden con `docs/CIERRE-VERSION-2026-11-09.md` (suite 252,
P1 sin silencio, G5 con los mismos bloqueos/pasos, G13 0 falsos positivos, F15.1 10/0 y 17/0).
No se detectó ninguna regresión respecto a la línea de cierre. **No se intentó reparar nada**: esta
etapa solo congela y mide.
