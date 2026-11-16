# PR — Paso 5: Extraer la búsqueda de catálogo como caso de uso (UC-01)

> **Rama:** `refactor/pharmacy-catalog-usecase` · **Base / HEAD inicial:** `140770d`
> (`refactor/application-contracts`) · **Fecha:** 2026-11-10 · **Modo:** primera
> extracción funcional (cableado del caso de uso en la ruta caliente).

## 1. Resumen ejecutivo

La búsqueda de catálogo se **cablea** como caso de uso de aplicación: la herramienta
`buscar_medicamento` ya no se despacha directamente contra el motor, sino que pasa por
`BuscarCatalogoUseCase` (introducido, aditivo, en el Paso 4). El caso de uso **orquesta
el motor existente sin reescribirlo** y produce un `CatalogSearchResult` tipado con
`fuente`, `campos_disponibles`, `estado` (status) y `limitaciones`. A la herramienta se
le devuelve `to_tool_dict()`, que es **el mismo dict** que emitía el motor: el formato
externo y lo que ve el LLM no cambian.

El comportamiento observable se preserva **por construcción** y se verifica con una
prueba de equivalencia antes/después (`execute(...) == _buscar_medicamento(...)`) sobre
un corpus determinista. La puerta única F15.1 (`_autorizar_busqueda`) y todas las
heurísticas (typos, alias, principio activo, forma, relevancia, laboratorio, multi-med)
quedan **intactas dentro del motor**; el motor `_buscar_medicamento` (~930 líneas) no se
tocó.

## 2. Alcance y archivos

| Archivo | Cambio | Rol |
|---|---|---|
| `app/tools.py` | +24 / −1 | 3 imports + 1 línea de despacho + `_buscar_catalogo` (orquestador fino de 20 líneas). **El motor `_buscar_medicamento` no se modifica.** |
| `app/application/adapters.py` | +36 / −1 | Nuevo `ToolRuntimeEngineCatalogAdapter` (envuelve el **motor** directo) + nota en el adaptador existente. |
| `app/application/contracts.py` | +107 / −38 | `CatalogSearchResult` gana `fuente`, `campos_disponibles`, `limitaciones`; `mapear_resultado` los deriva (puro) y se unifica en un único punto de construcción. `raw`/`to_tool_dict()` sin cambios. |
| `app/application/catalog_search.py` | +4 / −1 | Docstring: el caso de uso ya está cableado en la ruta caliente (antes decía que no lo invocaba nadie). Sin cambios de lógica. |
| `tests/test_catalog_search_wiring.py` | nuevo | 16 pruebas del Paso 5 (ver §4). |

`git diff 140770d HEAD --numstat -- app/`: 4 archivos de runtime, **+171 / −41** + 1 test nuevo.

## 3. Antes de editar — responsabilidades y puertas identificadas

- **Motor de búsqueda** = `ToolRuntime._buscar_medicamento` (`app/tools.py:2957-3888`).
  Único llamador real: el despacho `ToolRuntime.execute` (`:2450`). `turn.py` y los
  backstops invocan siempre `runtime.execute("buscar_medicamento", ...)`, nunca el
  método directamente. `_buscar_por_principio_activo` es un **helper interno** del motor.
- **Normalización / typo / alias / principio activo / forma / relevancia / laboratorio
  / multi-med**: todo vive dentro del motor y sus helpers (`aliases.reescribir`,
  `relevancia.*`, `_aplicar_forma`/`_aviso_forma`, `_buscar_por_principio_activo`). No se
  reescribió ninguno: el caso de uso los **reutiliza** a través del puerto.
- **Puerta única de autorización por término (F15.1)**: `_autorizar_busqueda`
  (`:2771`), invocada **dentro** del motor antes de tocar el catálogo. Se conserva como
  única puerta; el caso de uso **no autoriza**.
- **Evidencia vs. no evidencia (DOMAIN-MODEL §6)**: precio (USD/Bs), disponibilidad
  (`disponible`), presentación/forma y campos del catálogo **son evidencia** del
  producto; la **equivalencia por principio activo es mitigación, no evidencia** (no hay
  fuente de verdad). Esto se refleja en los metadatos tipados (§5).

## 4. Decisiones y justificación

1. **Cableado en el despacho `execute`, no en `turn.py`.** Hay ~15 call-sites de
   `runtime.execute("buscar_medicamento", ...)` en `turn.py` (LLM + backstops). Enrutar
   en el único punto de despacho (`execute`) los cubre todos con un cambio de una línea y
   sin tocar `turn.py`.
2. **Adaptador a nivel de motor para evitar recursión.** El adaptador del Paso 4
   (`ToolRuntimeCatalogAdapter`) delega en `execute`; si `execute` ahora enruta por el
   caso de uso, ese adaptador **reentraría en bucle**. Se añade
   `ToolRuntimeEngineCatalogAdapter`, que llama al **motor** (`_buscar_medicamento`)
   directamente. El adaptador basado en `execute` se conserva **intacto** (y su test),
   para consumidores externos que quieran el despacho completo.
3. **`CrmError` se maneja igual que antes.** El motor puede lanzar `CrmError`; propaga a
   través del caso de uso (que **no** la captura) hasta el `try/except CrmError` de
   `execute`, que la vuelve `{ok:False, error:"crm_error"}`. Verificado: el test del
   Paso 4 `test_adaptador_traduce_crm_error_a_proveedor_no_disponible` sigue en verde.
4. **El `TimeoutError` del caso de uso no altera el comportamiento real.** El caso de uso
   normaliza `TimeoutError`→`PROVEEDOR_NO_DISPONIBLE`, pero el motor **nunca** lanza un
   `TimeoutError` desnudo: los timeouts de CRM se envuelven como `CrmError`
   (`crm.py:119-120`, `httpx.TimeoutException` ⊂ `httpx.HTTPError`) y los del LLM se
   capturan dentro de `llm.py`. La rama es, por tanto, inalcanzable por el motor real → sin
   cambio observable. `CancelledError` sigue propagando (es `BaseException`).
5. **Metadatos estructurados sin cambiar la serialización (prompt punto 5/6).** Se añaden
   `fuente`, `campos_disponibles`, `limitaciones` al resultado tipado; son **derivados**
   del dict del motor y NO modifican `raw`/`to_tool_dict()`. `equivalencia_no_confirmada`
   materializa el punto 6 (no afirmar equivalencia sin evidencia).
6. **`sugerir_generico` fuera de alcance.** También consulta el catálogo y la misma
   puerta F15.1, pero emite otro contrato; extraerlo excede el Paso 5. Queda como
   candidato a un paso posterior. No se modificó.

### Metadatos derivados (`mapear_resultado`)

```
fuente             = "catalogo_farmacia"  si status ∈ {ENCONTRADO, PARCIAL,
                     NO_ENCONTRADO, PROVEEDOR_NO_DISPONIBLE} ; None en NO_AUTORIZADO,
                     NO_CONSULTA, ERROR (el catálogo no se consultó).
campos_disponibles = unión ordenada de claves de los productos (evidencia presente).
limitaciones       = equivalencia_no_confirmada (principio activo) ·
                     forma_pedida_no_disponible (aviso_forma) ·
                     precio_bs_incompleto (algún producto sin Bs).
```

## 5. Pruebas (comandos y resultados)

Intérprete: `.venv-test/Scripts/python.exe` (Python 3.11.16). Encoding:
`PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.

| Prueba | Comando | Resultado |
|---|---|---|
| Compilación | `python -m py_compile app/tools.py app/application/*.py` | **PASS** |
| Import de arranque | `python -c "import app.tools; import app.main"` | **PASS** (sin ciclo) |
| **Equivalencia motor↔execute** (corpus) | `pytest tests/test_catalog_search_wiring.py::test_execute_equivale_al_motor` | **6 passed** |
| Llamada única + F15.1 vía cableado + estructura | `pytest tests/test_catalog_search_wiring.py` | **16 passed** |
| Regresión F14 | `pytest tests/test_f14_busqueda.py` | **PASS** (incluido en 227) |
| Comprensión / F15 | `pytest tests/test_f15_intencion.py tests/test_comprension_termino.py` | **PASS** |
| Equivalencia y forma / G13 | `pytest tests/test_equivalencia_y_forma.py tests/test_g13_accion.py` | **PASS** |
| Contrato + adaptador (Paso 4) | `pytest tests/test_application_catalog_contract.py tests/test_application_catalog_adapter.py` | **PASS** |
| Subconjunto de regresión (todo lo anterior) | — | **227 passed** |
| **Suite completa** | `pytest tests/ -q` | **294 passed** (278 baseline + 16 nuevos) |
| Sonda P1 | `PYTHONPATH=. python tests/harness_agente_p1.py` | **exit 0** (paso) |
| Sonda P2 | `PYTHONPATH=. python tests/harness_agente_p2.py` | **exit 0** (paso) |
| Sonda F15.1 por término | `PYTHONPATH=. python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` | **10 pruebas, 0 huecos, exit 0** |

### Comparación antes/después

El corpus (`CORPUS` en el test) cubre: **éxito**, **typo** (`omeprasol`→OMEPRAZOL),
**forma/presentación** (`buscapina … grageas`), **varios términos**, **sin resultados**
y **término vacío**. Para cada uno, `execute("buscar_medicamento", {"nombre": t})`
devuelve un dict **idéntico** al del motor `_buscar_medicamento({"nombre": t})` y deja
**el mismo estado del runtime** (`last_options`, `last_products`, `aviso_forma`,
`es_laboratorio`, `es_sal`, `busqueda_por_principio`) que consumen los backstops del
turno. No hay diferencias no intencionales. No se editó ninguna sonda ni fixture.

## 6. Pruebas no ejecutadas y motivo

- **F14 (`simular_conversaciones`), integración DB, `sonda_catalogo`**: **BLOCKED** por
  Docker caído / credenciales CRM (igual que la baseline, `BASELINE-TEST-MATRIX.md`).
  La ruta de búsqueda del motor no cambió; la equivalencia determinista cubre la no
  regresión.
- **Sonda F15.1 contra modelo real (proxy)**: **NOT RUN**. El endpoint del proxy no
  respondió en esta sesión; la autorización por término se cubrió de forma determinista
  (sonda `probe_autorizacion_por_termino.py` + `test_f15_1_veto_por_termino_a_traves_del_cableado`),
  y el motor/F15.1 no se modificaron.
- **Sonda P3**: **NOT RUN** (P1/P2 deterministas bastan como evidencia; el motor no
  cambió).

## 7. Riesgos y rollback

- **Riesgo:** bajo. El motor y F15.1 no se tocan; el cableado devuelve el dict del motor
  por identidad. El único punto nuevo en la ruta caliente es `_buscar_catalogo`
  (20 líneas) y un adaptador de 1 llamada.
- **Rollback:** revertir la línea de despacho (`_buscar_catalogo` → `_buscar_medicamento`)
  y eliminar `_buscar_catalogo`, el adaptador nuevo, los campos derivados de `contracts`
  y el test nuevo. Sin migraciones, dependencias ni esquema que revertir.

## 8. Reglas no negociables — cumplimiento

1. No se debilitó ni duplicó F14/F15/F15.1/G5/G6/G13 ni guards. ✓
2. La puerta única sigue siendo `_autorizar_busqueda`; no se creó bypass ni segunda
   puerta (verificado por sonda, incl. `sugerir_generico`). ✓
3. El caso de uso no autoriza, no llama al LLM, no decide permisos. ✓
4. Sin cambios de contrato público ni de comportamiento observable (equivalencia probada). ✓
5. No se reescribió `turn.py` ni `tools.py`; el motor quedó intacto. ✓
6. No se alteraron sondas, fixtures ni tests existentes. ✓
7. Sin dependencias nuevas ni cambios de esquema. ✓
8. Sin push/merge/rebase/deploy. ✓

## 9. Decisiones resueltas (ratificadas por el propietario)

1. **`principio_activo` NO es `EQUIVALENTE`.** Compartir principio activo no demuestra
   equivalencia farmacéutica/terapéutica. Se mantiene `PARCIAL` y se hace explícita la
   coincidencia por principio activo **sin elevarla a equivalencia**, con
   `por_principio_activo=True` + limitación `equivalencia_no_confirmada`. No se introduce
   un estado `EQUIVALENTE`. (El contrato no tiene hoy un estado dedicado de "coincidencia
   por principio activo"; conservar `PARCIAL` es la opción segura.)
2. **`AMBIGUO` permanece reservado.** El contrato no selecciona automáticamente entre
   varios candidatos plausibles; la resolución exige regla explícita y evidencia
   suficiente. Mientras tanto, la respuesta debe ser conservadora o pedir precisión.
3. **`sugerir_generico` se extrae en un paso posterior.** No se mezcla con esta
   extracción; sugerir un genérico exige reglas y evidencia propias y NO se deriva
   automáticamente de compartir principio activo.

Decisiones documentadas en el docstring de `CatalogSearchStatus` (`contracts.py`).

## 10. Recomendación

**Continuar** tras aprobación del propietario. El cableado es behavior-preserving y deja
el caso de uso UC-01 en la ruta caliente, habilitando el Paso 6. No avanzar por
iniciativa propia.
