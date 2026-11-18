# Línea base congelada — Paso 1 de la modernización arquitectónica

> **Alcance:** Prompt 01 de `docs/architecture/PROMPTS-MODERNIZACION.md` — *congelar y medir la
> línea base actual*. Auditoría y ejecución de pruebas; **no se modificó runtime**. No se reparó
> ningún fallo. No se hizo commit ni push: los dos documentos quedan preparados para revisión.

## 1. Identificación y entorno

| Campo | Valor |
|---|---|
| Repositorio | `arquimedesgarcia/genteFarma-nea` (remoto `gentefarma`) |
| Rama de medición | `chore/architecture-baseline` (creada **local**, sin push, desde el HEAD de `docs/plan-modernizacion-farmacia`) |
| HEAD medido | `c80f90eeec218e484556a8fc6cc27963d761e55d` |
| Rama de referencia del plan | `agent-qa` @ `9715eeea4b6a8870bbda219cc4d35996be22a4b3` |
| **Relación código ↔ `agent-qa`** | El árbol medido es **idéntico byte a byte** a `agent-qa`: `docs/plan-modernizacion-farmacia` = `agent-qa` + 2 commits **solo-docs** (`4fda35b`, `c80f90e`). `git diff gentefarma/agent-qa HEAD -- . ':(exclude)docs/**'` → **vacío**. La baseline medida aquí **es** la de `agent-qa`. |
| Fecha de medición | 2026-11-10 |
| Árbol de trabajo | **limpio** (`git status --porcelain` vacío antes y después de la batería) |
| SO | Windows 11 Pro (26300) |
| Intérprete | `.venv-test/Scripts/python.exe` → **Python 3.11.16** |
| pytest | 9.1.1 |
| pypdf (venv) | **6.21.4** — **alineado** con el pin `requirements.txt:pypdf==6.21.4` (la observación H-04 de `docs/VALIDACION-INTEGRACION-AUTOR-QA.md`, que reportaba 6.19.0 desalineado, está **resuelta**) |
| Docker | **caído** (daemon no responde: `npipe:////./pipe/dockerDesktopLinuxEngine … daemon is running` → no) |
| Proxy LLM | `http://127.0.0.1:11500/v1` **vivo** para `chat/completions` (HTTP 200); `/v1/models` responde 501 |
| Modelo medido | `deepseek/deepseek-v4.1-flash` (vía proxy) |

### Nota de entorno — codificación de consola (no es un defecto de código)

Dos sondas (`sonda_bucle.py`, `sonda_g5.py`) imprimen respuestas del agente que contienen emojis
(`💊`, `🛒`, …). La consola de Windows usa `cp1252` por defecto y **aborta el `print`** con
`UnicodeEncodeError` al llegar al emoji — la lógica de la sonda ya había corrido correctamente; solo
falla la impresión. Se resolvió **a nivel de entorno**, sin tocar la sonda, exportando
`PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`. No se modificó ningún instrumento de medición.

## 2. Instrumentos localizados (no se inventaron rutas)

| Instrumento | Ruta real | Tipo |
|---|---|---|
| Compilación | `app/*.py` vía `py_compile` | determinista |
| Suite | `tests/` vía `pytest` | determinista |
| P1 — bucle de herramientas | `eval/qa-2026-11-06/sonda_bucle.py` | determinista |
| P2 — G5 evidencia | `eval/qa-2026-11-06/sonda_g5.py` | determinista |
| P3 / G13 — afirmación sin acción | `eval/g13-afirmacion-sin-accion/detector_g13.py` | determinista |
| G6 — importe respaldado por lista | `tests/test_g6_lista_mostrada.py` (script `main()`, **no** test de pytest) | determinista |
| F15.1 — autorización por término | `eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` | determinista (+ variante `--model`) |
| F14 — precisión de búsqueda | `eval/f14-busqueda/simular_casos.py` | determinista |
| F14 conversacional (catálogo) | `eval/f14-busqueda/sonda_catalogo.py` | **requiere CRM real** |
| Escenarios P1/P2/P3 | `tests/harness_agente_p{1,2,3}.py` | módulos de escenarios (sin `__main__`; importan limpio) |

**Aviso de los autores (respetado):** las sondas de `eval/qa-2026-11-06/` fijan
`REPO = D:/Proyectos/GenteFarma/agentNEA` en `sys.path`. Toda la medición se hizo **en el repo
principal**, no en un worktree, para que midan este árbol.

## 3. Resultado — resumen

**Línea base reproducible y verde en todo lo determinista.** Los números coinciden con lo
documentado en `docs/CIERRE-VERSION-2026-11-09.md`.

- **PASS (determinista):** `py_compile`; suite **252 passed**; P1 (6/6 responden, sin silencio);
  P2/G5 (6 bloqueos + 3 pasos correctos); G13 (12 textos, 0 falsos positivos); G6 (15/15);
  F15.1 sin modelo (10 pruebas, 0 huecos); F14 determinista (JSON producido).
- **PASS (modelo real, proxy vivo):** F15.1 `--model deepseek/deepseek-v4.1-flash` (17 pruebas, 0 huecos).
- **BLOCKED:** F14 conversacional (`sonda_catalogo.py` exige `CRM_BASE_URL`/`CRM_BOT_API_KEY`);
  todo lo dependiente de Docker (Laboratorio/DB desechable, `simular_conversaciones.py`) por daemon caído.
- **NOT RUN (fuera del alcance del Paso 1):** corpus `medir_corpus.py` (escribe dentro de `eval/`
  versionado; el plan no lo pide para la baseline).

La matriz detallada con comandos y evidencia literal está en
[`BASELINE-TEST-MATRIX.md`](./BASELINE-TEST-MATRIX.md).

## 4. Limitaciones y bloqueos explícitos

1. **Docker caído** → la validación de integración con DB desechable y los niveles conversacionales
   F14 quedan **BLOCKED**, no medidos. Necesarios antes de producción, no para esta baseline.
2. **CRM no disponible** en el entorno local → `sonda_catalogo.py` (F14 conversacional) **BLOCKED**.
3. **Proxy LLM** está vivo hoy (HTTP 200 en `chat/completions`), lo que permitió medir la variante
   con modelo real de F15.1. `/v1/models` devuelve 501, pero no afecta a los probes (usan
   `chat/completions`). Nota: la nota del commit `0f09e0a` lo daba por caído; hoy responde.
4. **Corpus F15** se re-mide por corrida y es ruidoso por diseño (el propio cierre documenta 49/50,
   no 50/50). No se ejecutó en esta etapa por no estar en el alcance del Paso 1 y por escribir en
   rutas versionadas de `eval/`.

## 5. Reproducción (comandos exactos usados)

```bash
PY=.venv-test/Scripts/python.exe
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=.

$PY -m py_compile app/*.py                                                   # PY_COMPILE_OK
$PY -m pytest tests/ -q                                                      # 252 passed
$PY eval/qa-2026-11-06/sonda_bucle.py                                        # A1..A4/A7/C0 responden
$PY eval/qa-2026-11-06/sonda_g5.py                                           # B* bloqueos/pasos correctos
$PY eval/g13-afirmacion-sin-accion/detector_g13.py                           # 12 textos, 0 coincidencias
$PY tests/test_g6_lista_mostrada.py                                          # 15/15 OK
$PY eval/f15-frases-conversacion/probe_autorizacion_por_termino.py           # PRUEBAS: 10  HUECOS: 0
OPENAI_BASE_URL=http://127.0.0.1:11500/v1 \
  $PY eval/f15-frases-conversacion/probe_autorizacion_por_termino.py \
      --model deepseek/deepseek-v4.1-flash                                   # PRUEBAS: 17  HUECOS: 0
$PY eval/f14-busqueda/simular_casos.py --root . --out <tmp>/f14-baseline.json  # JSON determinista
```

## 6. Confirmación de las reglas no negociables del Paso 1

- No se modificó runtime (`app/`), ni sondas, ni fixtures, ni tests. Diff de código = **vacío**.
- No se reparó ningún fallo. El único ajuste fue de **entorno** (`PYTHONUTF8`), no de instrumento.
- No se instalaron ni actualizaron paquetes.
- No se cambió de rama compartida ni se tocó `agent-qa`/`desarrollo`/`main`/upstream.
- No se hizo commit ni push. Estos dos documentos quedan en el árbol de trabajo para revisión.

**Criterio de salida:** línea base reproducible con bloqueos explícitos, sin cambios de comportamiento.
