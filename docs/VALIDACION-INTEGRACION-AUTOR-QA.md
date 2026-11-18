# Validación QA — Integración selectiva del autor original

## A. Identificación

| Campo | Valor |
|---|---|
| Repositorio | `arquimedesgarcia/genteFarma-nea` (espejo `gentefarma`) · origen `rsomoza01/nea-agent` (solo lectura en esta tarea) |
| Rama validada | `fix/integracion-autor-compatible` |
| SHA validado (rama y `refs/pull/3/head`) | `907fea7cc8e68a2369f9c9b4dba242ac66abd23a` |
| SHA base `agent-qa` (= `origin/desarrollo` = `gentefarma/agent-qa`) | `0f09e0a3f7fb60a0e49fbd4afa5d24b28e75bdbb` |
| `refs/pull/3/merge` | `145528fb7dd9964a6ae2526e84193a92b91ada02` — **árbol idéntico al head de la rama** (merge a `agent-qa` = fast-forward) |
| Relación con `agent-qa` | `git merge-base --is-ancestor 0f09e0a… 907fea7…` → **sí** (FF puro, 7 commits) |
| Fecha y hora | 2026-11-09 22:36 VST (America/Caracas) |
| Entorno | Windows 11 · git-bash (MSYS) · worktree **detached** en `D:\Proyectos\GenteFarma\_wt-intautor` (la rama de trabajo del repo, `desarrollo`, no se tocó) |
| Python | 3.11.16 (`.venv-test`) · pytest 9.1.1 · uv 0.12.18 · Docker **no disponible** (daemon caído) |
| Proxy LLM | `http://127.0.0.1:11500/v1` **vivo** (HTTP 200) · modelo medido: `deepseek/deepseek-v4.1-flash` |

`git status --short` limpio en el repo principal (`desarrollo` @ `0f09e0a`) y en el worktree antes y después de la batería. No se hizo merge, rebase, push, ni se cambió el estado del PR #3.

## B. Resumen ejecutivo

**APROBADO CON OBSERVACIONES.**

Los 7 commits son una portación **aditiva y acotada** (7 archivos, +315/−1): fijan `pypdf==6.21.4`, añaden dos ajustes LLM opcionales **vacíos por defecto** y su fallback, con 4 tests nuevos. No tocan `tools.py`, `turn.py`, `guards.py`, `intencion.py` ni `relevancia.py` (verificado con `git diff --quiet`: sin cambios), así que el comportamiento funcional de farmacia es, por construcción, el mismo árbol que `agent-qa`.

Verificado por ejecución real: compila; **4/4** tests nuevos; suite **178 passed** (174 históricos + 4); `uv pip check` compatible; `pypdf==6.21.4` instala y extrae texto igual que 5.1.0 y 6.19.0 en 6 fixtures PDF; `pip-audit` = **0 vulnerabilidades** con el pin nuevo frente a **103** con el piso anterior; sonda F15.1 **0 huecos** (10 casos sin modelo, 17 con modelo real); F14 determinista **byte a byte idéntico** al árbol base y 102 tests F14 verdes; corpus **49/50 en ambos árboles** (0 falsos positivos).

Se aprueba **con observaciones** porque dos niveles quedaron **BLOQUEADOS** por infraestructura ausente (Docker caído): el Laboratorio/DB desechable (Fase 10) y los niveles conversacionales F14 (simulador) y el A/B de hints dentro del agente. No hay ningún hallazgo crítico ni alto, pero esas validaciones no se declaran hechas.

## C. Matriz de pruebas

| Fase | Prueba | Resultado | Evidencia | Observaciones |
|---|---|---|---|---|
| 1 | `py_compile app/*.py` (árbol de integración) | PASS | `PY_COMPILE_OK` (sin salida de error) | — |
| 1 | Alcance del diff | PASS | `git diff --name-status 0f09e0a...HEAD` = `.env.example`, `app/config.py`, `app/llm.py`, `app/main.py`, `docs/INTEGRACION-AUTOR-ORIGINAL.md`, `requirements.txt`, `tests/test_llm_provider_hints.py` | Ningún archivo fuera del alcance declarado |
| 1 | Archivos protegidos intactos | PASS | `git diff --quiet 0f09e0a...HEAD -- app/tools.py app/turn.py app/guards.py app/intencion.py app/relevancia.py app/media.py app/aliases.py app/state.py` → vacío en los 8 | `eval/` también sin cambios |
| 1 | Árbol del PR #3 | PASS | `git diff --stat gentefarma/pr3-merge 907fea7` → vacío | El merge del PR no aporta contenido distinto |
| 2 | `pytest tests/test_llm_provider_hints.py -v` | PASS | `4 passed in 2.20s` | Los 4 escenarios se ejercitan de verdad (no mocks complacientes: el fake solo sustituye `chat.completions.create`) |
| 3 | `pytest tests/ -q` | PASS | `178 passed in 2.07s` | 174 referencia histórica + 4 nuevas; 0 fallos, 0 omitidos, 0 errores de colecta |
| 4 | `uv pip check` (venv del repo) | PASS | `Checked 53 packages … All installed packages are compatible` | El venv no tiene `pip` (`No module named pip`); se usó `uv` |
| 4 | Instalación del pin `pypdf==6.21.4` en entorno aislado | PASS | `uv pip install -r requirements.txt` en `duvenv` → `pypdf-6.21.4.dist-info`; `uv pip check` → compatible | Instalación limpia, sin conflictos |
| 4 | `pip-audit -r requirements.txt` (pin nuevo) | PASS | `No known vulnerabilities found` | — |
| 4 | `pip-audit` con el piso anterior (`pypdf==5.1.0`) | PASS (informativo) | `Found 103 known vulnerabilities in 1 package` | Confirma el valor real del cambio (H-07) |
| 4 | `pip-audit` con `pypdf==6.19.0` (versión que tiene el venv) | PASS (informativo) | 15 CVE (fixes hasta 6.21.4) | Ver H-04 (venv desalineado) |
| 5 | Extracción PDF real con `app.media._pdf_text` (6 fixtures) × pypdf **5.1.0 / 6.19.0 / 6.21.4** | PASS | Resultados **idénticos** en las tres versiones (ver Anexo A.4) | No existen tests PDF en el repo (H-05) |
| 6 | `probe_autorizacion_por_termino.py` (sin modelo) | PASS | `PRUEBAS: 10  HUECOS: 0` | Puerta F15.1 + `sugerir_generico` |
| 6 | `probe_autorizacion_por_termino.py --model deepseek/deepseek-v4.1-flash` | PASS | `PRUEBAS: 17  HUECOS: 0` | Incluye candidato ruidoso («oiga», «firma», «maria fernandez cmp 45231») y typo del mismo fármaco |
| 6 | Fallback de hints con proveedor REAL que rechaza `provider.sort` | PASS | Condición C del A/B: `hints_al_final=False`, `errores=0` | Prueba end-to-end de que nunca se sacrifica la respuesta |
| 7 | `eval/f14-busqueda/simular_casos.py` (determinista, sin LLM), integración vs base | PASS | JSON **idéntico byte a byte** (sin el campo `root`) | Evidencia más fuerte de no-regresión F14 |
| 7 | `pytest tests/test_f14_busqueda.py -q` | PASS | `102 passed` | — |
| 7 | `eval/f14-busqueda/simular_conversaciones.py` (nivel conversacional) | BLOCKED | `docker` → `cannot connect to the docker API … daemon is running` | Requiere el contenedor `nea` + catálogo inyectado |
| 8 | Corpus `medir_corpus.py` (50 frases, modelo real, proxy vivo) — árbol de integración | PASS | `ACIERTOS: 49/50 (98.0%)`, `falsos POSITIVOS: 0`, `falsos NEGATIVOS: 1`, `INCONCLUYENTES: 0` | Ver H-06: el objetivo absoluto 50/50 no se reprodujo |
| 8 | Corpus — árbol BASE (`0f09e0a3`), mismo modelo, mismo proxy | PASS | `ACIERTOS: 49/50 (98.0%)`, `falsos POSITIVOS: 0`, `falsos NEGATIVOS: 0`, `INCONCLUSIVOS: 1` (misma frase) | Corrida pareada: **sin regresión atribuible** |
| 9 | A/B a nivel de capa LLM con proveedor real (4 prompts × 3 condiciones) | PASS (parcial, fuera del agente) | A: 7539 ms / 2421 tok · B (`reasoning=minimal`): 6646 ms / 1985 tok / 0 errores · C (`provider.sort`): rechazado por el proveedor → fallback | **No** sustituye el A/B conversacional: las llamadas son al LLM pelado, sin prompt de agente ni herramientas |
| 10 | Integración con Docker + PostgreSQL desechable | BLOCKED | Daemon de Docker Desktop caído | No se ejecutó nada contra servicios productivos |

Estados: PASS / FAIL / BLOCKED / NOT RUN. No hay FAIL ni NOT RUN: todo lo ejecutable se ejecutó.

## D. Hallazgos

### H-01 — Heurística de rechazo por subcadena (severidad MEDIA)
- **Archivo/línea:** `app/llm.py:375-384` (condición en la línea 378: `if "reasoning" in message or "provider" in message`).
- **Descripción:** la decisión de desactivar los hints se toma buscando las subcadenas `reasoning`/`provider` en el texto del error. Cualquier error **no relacionado** que mencione la palabra «provider» desactiva los hints de forma **permanente para la instancia**.
- **Evidencia:** sonda con un mensaje de error realista de caída del upstream —`Error code: 502 - {'error': {'message': 'Provider returned error', 'code': 502}}`— → `LlmExhausted` y `hints siguen activos: False`. El proveedor real devuelve mensajes que sí contienen la palabra por motivos ajenos (p. ej. el 502 del proxy: `{'status': 400, 'message': 'This endpoint honors only \`provider.data_collection\`…'}`).
- **Impacto:** no rompe el turno (el fallo del turno se decide por el resultado de la llamada, no por esto), pero el operador pierde el ajuste configurado sin más señal que un `logger.warning`, y sin vía de re-habilitación salvo reiniciar el proceso. Viola el punto 4 del §5.C («los errores no relacionados … no deben provocar que se desactiven»).
- **Recomendación:** decidir por señal estructurada del `openai` (`exc.status_code == 400` y `getattr(exc, 'param', None)`/cuerpo del error apuntando al parámetro) en vez de por subcadena; como mínimo, exigir el nombre del parámetro completo (`reasoning.effort`/`provider.sort`) y distinguir reintento transitorio de rechazo. No se corrigió: fuera del alcance de esta validación.

### H-02 — Cualquier valor no vacío habilita los hints (severidad BAJA)
- **Archivo/línea:** `app/llm.py:78-80` (`_extras_ok = bool(self._reasoning_effort or self._provider_sort)`), `app/config.py:65-66`.
- **Descripción:** `llm_reasoning_effort` / `llm_provider_sort` son `str` libres; no hay validación ni semántica de «apagado». `LLM_PROVIDER_SORT=0` (o `false`, `none`, `off`) **envía** `provider: {"sort": "0"}` en vez de no enviar nada.
- **Evidencia:** sonda `sonda_env_hints.py` → `valor='0' -> _extras_ok=True  _extras()={'reasoning': {'effort': '0'}, 'provider': {'sort': '0'}}`; ídem `none`/`false`/`off`.
- **Impacto:** un valor de apagado «intuitivo» activa los hints; contra un proveedor que los rechaza eso cuesta llamadas extra (H-08) y produce un rechazo evitable. No afecta al estado por defecto (vacíos).
- **Recomendación:** tipar con `Literal["", "minimal", "low", …]`/enum + normalizar los valores de apagado a `""`; documentarlo en `.env.example`.

### H-03 — Desactivación todo-o-nada (severidad BAJA)
- **Archivo/línea:** `app/llm.py:362-373` (`_extras()` retorna `{}` cuando `_extras_ok` es falso) y `376-384`.
- **Descripción:** si el proveedor rechaza solo uno de los dos hints, se descartan **ambos** y no se vuelve a intentar enviar el aceptado.
- **Evidencia:** condición C del A/B (real): el proveedor rechaza `provider.sort` → `hints_al_final=False`; en el corpus y en la sonda, `reasoning.effort=minimal` **sí** es aceptado por el mismo proveedor (HTTP 200).
- **Impacto:** pérdida silenciosa del hint válido. Sin efecto sobre el flujo de farmacia.
- **Recomendación:** desactivar por parámetro (`_reasoning_ok` / `_provider_ok`) en lugar de un flag único.

### H-04 — Entorno de pruebas desalineado de `requirements.txt` (severidad BAJA, entorno)
- **Archivo/entorno:** `D:\Proyectos\GenteFarma\agentNEA\.venv-test`.
- **Descripción:** el venv de pruebas tiene `pypdf 6.19.0`, no la versión fijada `6.21.4`, y **no tiene `pip`** (`python -m pip` → `No module named pip`), así que `pip check`/`pip-audit` no corren dentro de él.
- **Evidencia:** `python -c "import pypdf; print(pypdf.__version__)"` → `6.19.0`; `pip-audit` con `pypdf==6.19.0` → 15 CVE (corregidas en 6.21.0/6.21.2/6.21.3/6.21.4).
- **Impacto:** la suite local no ejercita la versión fijada (la brecha se cubrió con una sonda multi-versión independiente: 5.1.0/6.19.0/6.21.4 → resultados idénticos). El despliegue por imagen sí instala el pin.
- **Recomendación:** recrear/alinear el venv con `requirements.txt` antes de la próxima corrida.

### H-05 — Sin pruebas de PDF en el repositorio (severidad BAJA)
- **Archivo:** `tests/` (7 archivos, ninguno cubre PDF); consumidor real `app/media.py:375-384` (`_pdf_text`) y `app/media.py:387-413` (`_document`).
- **Descripción:** `_pdf_text` no tiene `try/except` propio; la garantía de «el turno nunca rompe» viene del envoltorio de `describe_item` (`app/media.py:492-499`, `except Exception` → fallback honesto). No hay ninguna prueba automática que fije ese contrato.
- **Evidencia (sonda con el código real):** `receta-1p.pdf` → 174 chars extraídos; `documento-12p.pdf` → 510 chars (tope de 10 páginas aplicado, `reader.pages[:10]` válido en 6.x); `valido-sin-texto.pdf` → `""` (el llamador lo convierte en marcador honesto); `truncado.pdf` y `basura.pdf` → `pypdf.errors.PdfStreamError`; `cifrado.pdf` → `pypdf.errors.FileNotDecryptedError`. Idéntico en 5.1.0, 6.19.0 y 6.21.4.
- **Impacto:** riesgo de regresión no detectado si en el futuro alguien envuelve o reemplaza esa excepción.
- **Recomendación:** añadir un test de regresión con fixtures (válido / vacío / corrupto / cifrado / tope de páginas) que afirme: texto extraído, `""` para el vacío y «propaga excepción» (o «devuelve fallback honesto») para corrupto/cifrado.

### H-06 — El corpus no reproduce 50/50 en ninguno de los dos árboles (severidad BAJA, informativo)
- **Archivo:** `eval/f15-frases-conversacion/medir_corpus.py`, corpus de 50 frases.
- **Descripción:** árbol de integración 49/50 con **1 falso negativo**; árbol base 49/50 con **1 inconclusivo** — y es la **misma frase** (`'fexofenasina para la alergia esta sirviendo?'`, que el propio medidor anota como «otro: pregunta si le sirve, no pide el producto»). En ambas corridas: **0 falsos positivos**, 0 términos imperfectos.
- **Impacto:** ninguno atribuible a la integración: `app/intencion.py` y `app/turn.py` no cambian y el veredicto de esa frase oscila (fail-safe por JSON inválido vs. «no es consulta»). El objetivo documentado 50/50 **no** se reproduce hoy con `deepseek/deepseek-v4.1-flash` en ninguna de las dos ramas.
- **Recomendación:** registrar el resultado por corrida (no como valor fijo) y decidir si la frase debe exigir búsqueda o si el corpus debe marcarla como ambigua.

### H-07 — Pin exacto vs. rango abierto (severidad BAJA, informativo)
- **Archivo:** `requirements.txt:9`.
- **Descripción:** `pypdf>=5.1` (piso sin techo, 103 CVE conocidas en el piso) → `pypdf==6.21.4` (0 CVE). A cambio de la seguridad, el pin exacto exige un bump manual para futuras correcciones.
- **Evidencia:** `pip-audit -r requirements.txt` → `No known vulnerabilities found`; con `pypdf==5.1.0` → `Found 103 known vulnerabilities in 1 package`.
- **Recomendación:** mantener el pin y añadir una tarea periódica de auditoría (`pip-audit`), como ya se hace en el job de seguridad del autor.

### H-08 — Un hint rechazado multiplica las peticiones del primer turno (severidad BAJA, §8.4)
- **Archivo/línea:** `app/llm.py:391-424` (bucle de reintentos) + `AsyncOpenAI` con `max_retries` por defecto (2 sobre 5xx).
- **Descripción:** con `provider.sort=throughput` contra el proveedor actual (que lo rechaza y lo devuelve como 502), la primera petición del proceso cuesta **4 peticiones HTTP** (1 rechazada + 2 reintentos internos del SDK + 1 reintento limpio de la app).
- **Evidencia:** condición C del A/B: `intentos_app=5`, `peticiones_http=7` para 4 prompts (frente a 4/4 en A y B), `errores=0` — es decir, el mecanismo de la app funciona: se pierde latencia, no la respuesta. No hay escrituras duplicadas: la operación es de solo lectura.
- **Recomendación:** si algún día se habilita `provider_sort`, hacerlo con un proveedor que lo soporte y con `max_retries` explícito en el cliente para no multiplicar los reintentos.

## E. Compatibilidad funcional (F14, F15, F15.1)

- **Por construcción (diff):** `app/tools.py`, `app/turn.py`, `app/guards.py`, `app/intencion.py`, `app/relevancia.py`, `app/media.py`, `app/aliases.py` y `app/state.py` son **byte a byte** los de `agent-qa` (`git diff --quiet` sin salida). El diff se limita a `requirements.txt`, `app/config.py`, `app/llm.py`, `app/main.py`, `.env.example`, un documento y un test nuevo. No hay cambios en la interpretación de mensajes, la autorización por término, la identificación de medicamentos ni la ejecución de herramientas.
- **F15.1 (autorización por término y candidato) — conservada:** sonda determinista `PRUEBAS: 10  HUECOS: 0`; con modelo real `PRUEBAS: 17  HUECOS: 0` (incluye el caso G: mensaje con consulta real pero candidato ruidoso, y el typo del mismo fármaco que **debe** pasar). Fail-closed intacto: sin autorización, `sugerir_generico` **no** consulta el catálogo.
- **F14 (precisión de búsqueda) — conservada:** el simulador determinista produce un JSON **idéntico byte a byte** al del árbol base (extracción de términos, «parece lista», opciones múltiples, negativos de opción, filtrado de relevancia) y `tests/test_f14_busqueda.py` da `102 passed`.
- **F15 (capa de comprensión) — conservada:** corpus 49/50 en ambos árboles, **0 falsos positivos** en ambos; la diferencia de una frase es oscilación del modelo sobre la misma entrada (H-06), no del código (que no cambió).
- **G1-G12 / vetos:** archivos sin cambios en el diff; la cadena de vetos no se tocó.

## F. Dependencias

| Elemento | Resultado |
|---|---|
| Pin del proyecto | `pypdf==6.21.4` (antes `pypdf>=5.1`) |
| Instalación | `uv pip install -r requirements.txt` en entorno aislado → OK, `pypdf-6.21.4` |
| `pip check` | `uv pip check` (el venv no tiene `pip`): `Checked 53 packages … All installed packages are compatible` (repo) y `Checked 48 packages … compatible` (entorno aislado con el pin) |
| `pip-audit` con el pin nuevo | `No known vulnerabilities found` |
| `pip-audit` con el piso anterior | `Found 103 known vulnerabilities in 1 package` (incl. 4 corregidas exactamente en 6.21.4: CVE-2026-119203/119204/119205) |
| Versión realmente instalada en el venv del repo | **6.19.0** (desalineada del pin) → 15 CVE conocidas; ver H-04 |
| Compatibilidad funcional del cambio | Extracción idéntica en 5.1.0 / 6.19.0 / 6.21.4 sobre 6 fixtures (válido, 12 páginas, sin texto, truncado, basura, cifrado) |

## G. Recomendación sobre los hints LLM

**Mantenerlos desactivados.** Están vacíos por defecto y no se habilitaron en ningún momento de esta validación.

Evidencia recogida (nivel cliente, proveedor real, 4 prompts por condición):

| Condición | Latencia media | Tokens | Errores | Veredicto del proveedor |
|---|---|---|---|---|
| A — sin hints | 7539 ms | 2421 | 0 | — |
| B — `reasoning_effort=minimal` | 6646 ms (−12 %) | 1985 (−18 %) | 0 | **aceptado** (HTTP 200) |
| C — `provider_sort=throughput` | 6937 ms | 1692 | 0 | **rechazado** → hints desactivados y respuesta entregada |

- `provider_sort` **no debe habilitarse**: el proveedor actual (proxy Nous) responde que solo atiende `provider.data_collection` y `provider.zdr`; «Other caller-supplied `provider` routing preferences (e.g. `only`, `ignore`, `order`, `require_parameters`, `sort`) are not enforced». Habilitarlo solo cuesta llamadas extra antes de autodesactivarse (H-08).
- `reasoning_effort=minimal` sí lo acepta el proveedor y en esta muestra redujo latencia y tokens sin errores ni respuestas vacías. **Pero** la muestra son 4 llamadas al LLM **pelado** (sin el prompt del agente, sin herramientas y sin catálogo): no dice nada sobre precisión de búsqueda, identificación de medicamentos ni comportamiento en conversaciones ambiguas, que es exactamente lo que el §9 exige antes de recomendar la activación.
- Por tanto: **A/B conversacional pendiente** (Laboratorio con juez + sonda F14/F15.1), que hoy está **BLOQUEADO** por Docker. Hasta entonces, el ajuste debe seguir apagado en producción.

## H. Decisión de integración

**¿Recomiendas integrar `fix/integracion-autor-compatible` en `agent-qa`?**

**Sí, con condiciones.**

Motivos:

1. El contenido está acotado a 7 archivos y **no toca el flujo de farmacia** (verificado por diff, no por inspección visual): F14/F15/F15.1 y G1-G12 conservan su comportamiento (evidencia: sonda 0 huecos, JSON de F14 idéntico byte a byte, 102 + 178 tests verdes, corpus sin falsos positivos).
2. El cambio de dependencia es una mejora de seguridad medida: **103 CVE → 0**, sin diferencia funcional detectable en la extracción de PDF.
3. Los hints llegan **apagados** y su fallback funciona contra el proveedor real (nunca sacrifica la respuesta).
4. El merge es **fast-forward** y el árbol de `refs/pull/3/merge` es idéntico al head de la rama: no hay sorpresas en el merge.

Condiciones (ninguna bloqueante para un merge a la rama de trabajo `agent-qa`, todas sí antes de dar la funcionalidad por «lista»):

1. Mantener `LLM_REASONING_EFFORT` y `LLM_PROVIDER_SORT` **sin configurar** en todos los entornos (producción incluida) hasta completar el A/B conversacional.
2. Abrir los H-01/H-03 (heurística por subcadena y desactivación todo-o-nada) como paquete aparte si algún día se habilita el ajuste, y H-02 (validación de valores) junto con la documentación del `.env.example`.
3. Ejecutar las fases **BLOQUEADAS** (Laboratorio/DB desechable, simulador F14 conversacional y camino real por `/chat`) cuando Docker esté disponible; hoy no se declaran validadas.
4. Alinear el venv de pruebas con `requirements.txt` (H-04) y añadir la cobertura PDF (H-05).
5. Registrar el resultado del corpus por corrida (49/50 hoy en ambos árboles) y no fijar «50/50» como cifra contractual (H-06).

La decisión de merge corresponde exclusivamente al propietario del repositorio.

---

## Anexo A — Comandos ejecutados y salida literal (recortada a lo esencial)

Todos los comandos se ejecutaron en el worktree `D:\Proyectos\GenteFarma\_wt-intautor` (detached en `gentefarma/fix/integracion-autor-compatible`) o en copias de trabajo fuera del repositorio; el venv usado es `D:\Proyectos\GenteFarma\agentNEA\.venv-test`.

### A.1 Estado e integridad

```bash
$ git ls-remote gentefarma | grep -E "integracion|agent-qa|refs/pull"
0f09e0a3f7fb60a0e49fbd4afa5d24b28e75bdbb  refs/heads/agent-qa
907fea7cc8e68a2369f9c9b4dba242ac66abd23a  refs/heads/fix/integracion-autor-compatible
907fea7cc8e68a2369f9c9b4dba242ac66abd23a  refs/pull/3/head
145528fb7dd9964a6ae2526e84193a92b91ada02  refs/pull/3/merge

$ git diff 0f09e0a3f7fb60a0e49fbd4afa5d24b28e75bdbb...HEAD --stat
 .env.example                       |   5 ++
 app/config.py                      |   4 ++
 app/llm.py                         |  32 ++++++++
 app/main.py                        |   2 +
 docs/INTEGRACION-AUTOR-ORIGINAL.md | 139 ++++++++++++++++++++++++++++++++
 requirements.txt                   |   2 +-
 tests/test_llm_provider_hints.py   | 132 +++++++++++++++++++++++++++++++++++++
 7 files changed, 315 insertions(+), 1 deletion(-)

$ git diff --quiet <base>...HEAD -- app/tools.py app/turn.py app/guards.py app/intencion.py app/relevancia.py app/media.py app/aliases.py app/state.py
(sin salida: sin cambios en los 8 archivos)

$ .venv-test/Scripts/python.exe -m py_compile app/*.py && echo PY_COMPILE_OK
PY_COMPILE_OK
```

### A.2 Tests

```bash
$ .venv-test/Scripts/python.exe -m pytest tests/test_llm_provider_hints.py -v
tests/test_llm_provider_hints.py::test_hints_apagados_por_defecto[asyncio] PASSED
tests/test_llm_provider_hints.py::test_hints_se_envian_solo_si_se_configuran[asyncio] PASSED
tests/test_llm_provider_hints.py::test_rechazo_de_hint_desactiva_y_reintenta_sin_hints[asyncio] PASSED
tests/test_llm_provider_hints.py::test_error_no_relacionado_no_desactiva_hints[asyncio] PASSED
============================== 4 passed in 2.20s ==============================

$ .venv-test/Scripts/python.exe -m pytest tests/ -q
178 passed in 2.07s

$ .venv-test/Scripts/python.exe -m pytest tests/test_f14_busqueda.py -q
102 passed in 1.51s
```

Referencia histórica del encargo: 174 pruebas → **174 + 4 nuevas = 178** (0 fallos, 0 omitidos).

### A.3 Dependencias

```bash
$ .venv-test/Scripts/python.exe -m pip --version
…python.exe: No module named pip          # el venv no trae pip
$ uv pip check --python …/.venv-test/Scripts/python.exe
Checked 53 packages in 128ms
All installed packages are compatible
$ uv venv duvenv --python 3.11 && uv pip install --python duvenv/Scripts/python.exe -r requirements.txt
 + pypdf==6.21.4 …
$ uv pip check --python duvenv/Scripts/python.exe
Checked 48 packages in 3ms
All installed packages are compatible
$ uvx pip-audit -r requirements.txt
No known vulnerabilities found
$ uvx pip-audit -r <requirements con pypdf==5.1.0>
Found 103 known vulnerabilities in 1 package
pypdf 5.1.0   CVE-2026-119203  6.21.4
pypdf 5.1.0   CVE-2026-119205  6.21.4
pypdf 5.1.0   CVE-2026-119204  6.21.4   (…)
$ uvx pip-audit -r <requirements con pypdf==6.19.0>
pypdf 6.19.0  CVE-2026-119203  6.21.4   (… 15 en total)
```

### A.4 PDF (sonda con `app.media._pdf_text` real)

Fixtures sintéticos generados por script (receta 1 página, documento 12 páginas, válido sin texto, truncado, basura, cifrado). Resultado **idéntico** con `pypdf` 5.1.0, 6.19.0 y 6.21.4:

```json
{"PDF_MAX_PAGES": 10, "PDF_MAX_CHARS": 8000, "resultados": {
 "receta-1p.pdf":        {"ok": true,  "chars": 174, "head": "RECETA MEDICA - Dra. Prueba QA / M.P.S. 00000. Amoxicilina 500 mg: …"},
 "documento-12p.pdf":    {"ok": true,  "chars": 510, "head": "Documento de prueba QA - pagina 1 - Losartan 50 mg\n…"},
 "valido-sin-texto.pdf": {"ok": true,  "chars": 0},
 "truncado.pdf":         {"ok": false, "exc": "pypdf.errors.PdfStreamError", "msg": "Stream has ended unexpectedly"},
 "basura.pdf":           {"ok": false, "exc": "pypdf.errors.PdfStreamError", "msg": "Stream has ended unexpectedly"},
 "cifrado.pdf":          {"ok": false, "exc": "pypdf.errors.FileNotDecryptedError", "msg": "File has not been decrypted"}}}
```

### A.5 F15.1 y F14

```bash
$ PYTHONPATH=<árbol> python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py
PRUEBAS: 10   HUECOS: 0
$ PYTHONPATH=<árbol> OPENAI_BASE_URL=http://127.0.0.1:11500/v1 \
  python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py --model deepseek/deepseek-v4.1-flash
PRUEBAS: 17   HUECOS: 0
$ python eval/f14-busqueda/simular_casos.py --root <integración> --out f14-casos-qa.json
$ python eval/f14-busqueda/simular_casos.py --root <base 0f09e0a3>  --out f14-casos-base.json
F14 determinista: IDENTICO byte a byte (sin campo root) -> True
```

### A.6 Corpus (corrida pareada, mismo modelo y proxy)

```bash
$ INTENT_PROXY=http://127.0.0.1:11500/v1 python eval/f15-frases-conversacion/medir_corpus.py --model deepseek/deepseek-v4.1-flash
# integración (907fea7c)
modelo: deepseek/deepseek-v4.1-flash | frases: 50 | llamadas: 48 | tokens: 75021 | 137.4s
ACIERTOS: 49/50  (98.0%)
falsos NEGATIVOS: 1  — 'fexofenasina para la alergia esta sirviendo?'
falsos POSITIVOS: 0
INCONCLUYENTES: 0
# base (0f09e0a3)
modelo: deepseek/deepseek-v4.1-flash | frases: 50 | llamadas: 48 | tokens: 75246 | 141.3s
ACIERTOS: 49/50  (98.0%)
falsos NEGATIVOS: 0
falsos POSITIVOS: 0
INCONCLUYENTES: 1  — 'fexofenasina para la alergia esta sirviendo?' (fail-safe: JSON inválido)
```

### A.7 A/B de hints contra el proveedor real (`app.llm.OpenAiLlm`, sin mocks)

```text
A sin hints                                  lat_media= 7539ms intentos_app=4 http=4 tokens=2421 errores=0 vacias=0 hints_al_final=False
B reasoning=minimal                          lat_media= 6646ms intentos_app=4 http=4 tokens=1985 errores=0 vacias=0 hints_al_final=True
C provider.sort=throughput (rechazado)       lat_media= 6937ms intentos_app=5 http=7 tokens=1692 errores=0 vacias=0 hints_al_final=False
```

Mensaje real del proveedor al rechazar `provider.sort`:

```text
Error code: 502 - {'status': 400, 'message': 'This endpoint honors only `provider.data_collection` ("allow" or "deny") and `provider.zdr` (true or false). Other caller-supplied `provider` routing preferences (e.g. `only`, `ignore`, `order`, `require_parameters`, `sort`) are not enforced …'}
```

### A.8 Infraestructura ausente

```bash
$ docker ps
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine; check if the path is correct and if the daemon is running
```

Docker Desktop está instalado (`C:\Program Files\Docker\Docker\Docker Desktop.exe`) pero el daemon está caído: por eso Fase 10 y los niveles conversacionales F14 quedan **BLOCKED**, no APROBADOS.

---

## Anexo B — Trazabilidad

- Rama validada sin checkout de `desarrollo`: worktree detached (`git worktree add ../_wt-intautor --detach`), eliminable con `git worktree remove ../_wt-intautor`.
- Las corridas con modelo se hicieron sobre **copias** del árbol en `%LOCALAPPDATA%\hermes\cache\scratch` (`qa-intautor` para la integración, `base-intautor` derivada de `git archive 0f09e0a3`), porque `medir_corpus.py` escribe su JSON dentro de `eval/` y habría sobrescrito un archivo versionado de la rama.
- Ninguna prueba tocó datos de pacientes, credenciales de producción, bases compartidas ni servicios productivos. Los fixtures PDF son sintéticos y sin datos reales.
- Artefactos crudos (fuera del repositorio): `ab-hints.json`, `corpus-run.log` (integración), `corpus-base.log` (base), `pip-audit-int.txt`, `pip-audit-pypdf5.txt`, `f14-casos-qa.json`, `f14-casos-base.json`, sondas `pdf_probe.py` / `pdf_fixtures.py` / `sonda_errores_proveedor.py` / `sonda_env_hints.py`.
- No se hizo commit ni push. El PR #3 quedó intacto.
