# PR — Paso 4: Contratos de aplicación y puertos mínimos

> **Rama:** `refactor/application-contracts` · **Base inicial:** `b55866c`
> (`docs/pharmacy-domain-design`) · **Fecha:** 2026-11-10 · **Modo:** implementación
> limitada, aditiva.
>
> **Estado de publicación:** el commit original de la etapa, `d388b58`, ya fue
> **confirmado y publicado** (`git push -u gentefarma refactor/application-contracts`).
> Las correcciones descritas en §10 son **posteriores a la revisión** y se confirman
> sobre esta misma rama.

## 1. Resumen ejecutivo

Se introduce la **capa de aplicación** de NEA para un único caso de uso, **búsqueda de
catálogo (UC-01)**, con contratos tipados y un puerto mínimo. El cambio es **aditivo y
paralelo**: son archivos nuevos bajo `app/application/` más dos archivos de prueba; **no
se modifica `turn.py`, `tools.py` ni ningún módulo de la ruta caliente**. El
comportamiento observable del bot es idéntico *por construcción*. El cableado funcional
del caso de uso en el turno corresponde al Paso 5.

## 2. Alcance y archivos

| Archivo | Nuevo | Rol |
|---|---|---|
| `app/application/__init__.py` | ✅ | Documenta la capa y su invariante (no autoriza, no llama al LLM). |
| `app/application/contracts.py` | ✅ | `CatalogSearchStatus`, `CatalogSearchRequest`, `CatalogSearchResult` (+`to_tool_dict()`), errores, `mapear_resultado` (pura). |
| `app/application/ports.py` | ✅ | `CatalogSearchPort` (Protocol estructural). |
| `app/application/catalog_search.py` | ✅ | `BuscarCatalogoUseCase`. |
| `app/application/adapters.py` | ✅ | `ToolRuntimeCatalogAdapter` (delega en `execute`). |
| `tests/test_application_catalog_contract.py` | ✅ | Mapeo puro + use case con dobles. |
| `tests/test_application_catalog_adapter.py` | ✅ | Adaptador sobre `ToolRuntime` real + CRM falso. |

`git diff --stat -- app/turn.py app/tools.py` → **vacío** (sin cambios).

## 3. Decisiones y justificación

1. **No cableado (paralelo).** El Paso 4 pide contratos *sin cambiar el comportamiento*;
   el cableado es el Paso 5. Dejar el caso de uso fuera de la ruta caliente garantiza
   diff pequeño y comportamiento idéntico.
2. **Puerto al nivel del motor de búsqueda**, no del CRM crudo (ver §6, bloqueo).
3. **`NO_AUTORIZADO` separado de `NO_CONSULTA`.** `no_autorizado_por_comprension` es el
   veredicto de la puerta única F15.1 ([ADR-0003](ADR/ADR-0003-puerta-unica-autorizacion.md));
   fundirlo con las heurísticas internas del handler ("no es un medicamento") borraría la
   frontera de autorización y la auditoría. La familia "no es medicamento"
   (`nombre_vacio`/`solo_sal`/`sin_medicamento`/`solo_saludo`/`no_medicamento`) sí se
   **agrupa** en `NO_CONSULTA`.
4. **`aviso_forma` y `principio_activo` → `PARCIAL`.** Hay producto, pero no exactamente
   lo pedido (otra forma / principio activo); es un cumplimiento parcial.
5. **`AMBIGUO` reservado.** El handler actual no emite un dict distinto para "varias
   opciones / forma no resuelta"; esa ambigüedad se presenta hoy como `PARCIAL`. No se
   diseña contra un supuesto.
6. **Serialización preservada.** `CatalogSearchResult.raw` guarda el dict original y
   `to_tool_dict()` lo devuelve por identidad (test dedicado).
7. **Cancelación vs. timeout.** El caso de uso **no** captura `asyncio.CancelledError`
   (propaga, por ser `BaseException`): una cancelación del turno nunca se vuelve
   resultado de negocio. Un `TimeoutError`/`asyncio.TimeoutError` (y
   `ProveedorNoDisponibleError`) sí se normaliza a `PROVEEDOR_NO_DISPONIBLE` — un timeout
   de dependencia externa es "proveedor no disponible", no un `NO_CONSULTA` silencioso
   (corrección de revisión §10.2).

### Mapeo (handler real → estado tipado)

```
ok=True, products, sin aviso, sin principio_activo   -> ENCONTRADO
ok=True + aviso_forma                                -> PARCIAL
ok=True + principio_activo=True                      -> PARCIAL (por_principio_activo=True)
error="sin_resultados"                               -> NO_ENCONTRADO
error="no_autorizado_por_comprension"                -> NO_AUTORIZADO
error="crm_error"                                    -> PROVEEDOR_NO_DISPONIBLE
error in {nombre_vacio, solo_sal, sin_medicamento,
          solo_saludo, no_medicamento}               -> NO_CONSULTA
error desconocido                                    -> ERROR  (no se asume NO_CONSULTA)
```

> **Corrección de revisión (§10.1):** un error no catalogado se tipa como `ERROR`
> explícito, no como `NO_CONSULTA`, para no enmascarar un fallo real como "el cliente
> no pidió un medicamento".

## 4. Pruebas (comandos y resultados)

Intérprete: `.venv-test/Scripts/python.exe` (Python 3.11.16). Encoding:
`PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.

| Prueba | Comando | Resultado |
|---|---|---|
| Compilación | `python -m py_compile app/application/*.py` | **PASS** |
| Contrato + adaptador | `python -m pytest tests/test_application_catalog_contract.py tests/test_application_catalog_adapter.py -q` | **26 passed** |
| Suite completa | `python -m pytest tests/ -q` | **278 passed** (252 baseline + 26 nuevos) |
| Ruta caliente intacta | `git diff --stat -- app/turn.py app/tools.py` | **vacío** |
| Sonda P1 | `PYTHONPATH=. python tests/harness_agente_p1.py` | **exit 0** (paso) |
| Sonda P2 | `PYTHONPATH=. python tests/harness_agente_p2.py` | **exit 0** (paso) |

Cobertura de contrato exigida por el prompt (punto 8): **éxito** (ENCONTRADO),
**no encontrado** (NO_ENCONTRADO), **parcial** (PARCIAL por forma y por principio
activo), **error externo** (CrmError→PROVEEDOR_NO_DISPONIBLE, vía adaptador real y vía
excepción de aplicación), **timeout** (TimeoutError→PROVEEDOR_NO_DISPONIBLE) y
**cancelación** (CancelledError propaga). Además: **error desconocido**→ERROR.

### Antes / después

`turn.py`/`tools.py` sin cambios ⇒ sondas P1/P2 y las 252 pruebas previas con el mismo
resultado que la baseline (`BASELINE.md`). Los 26 tests nuevos solo ejercitan la capa
nueva.

## 5. Pruebas no ejecutadas y motivo

- **F14 (`simular_conversaciones`), integración DB, `sonda_catalogo`**: BLOCKED por
  Docker caído / credenciales CRM (igual que la baseline, `BASELINE-TEST-MATRIX.md`). No
  afectadas por este cambio (ruta caliente intacta).
- **Sonda P3**: NOT RUN en esta entrega (deterministas P1/P2 bastan como evidencia de no
  regresión dado que el runtime no cambió).

## 6. Bloqueo documentado (prompt punto 11)

**No se pudo introducir un puerto a nivel de catálogo CRM** (`crm.get_products`) sin un
refactor mayor: el algoritmo de búsqueda (typos, alias, principio activo, forma,
relevancia, opción N, multi-med) está entrelazado en `ToolRuntime._buscar_medicamento`
(`app/tools.py:2957-3888`, ~930 líneas) junto con la autorización F15.1, el logging de
`med_queries` y los backstops del turno. Definir ese puerto exigiría extraer el motor =
**Paso 5**. Por eso el puerto mínimo de esta etapa (`CatalogSearchPort`) envuelve el
**motor de búsqueda** completo; el puerto a nivel de proveedor de catálogo queda diferido.

## 7. Riesgos y rollback

- **Riesgo:** muy bajo. Código nuevo no referenciado por la ruta caliente; `import` de
  `app.application` no tiene efectos de arranque (no lo importa `main.py`).
- **Rollback:** eliminar `app/application/` y los dos tests nuevos; no hay migraciones,
  deps ni cambios de esquema que revertir.

## 8. Preguntas / decisiones pendientes

- Confirmar en Paso 5 si `principio_activo` debe ser un estado propio (p. ej.
  `EQUIVALENTE`) en vez de `PARCIAL`.
- Confirmar la semántica de `AMBIGUO` cuando el Paso 5 exponga "varias opciones".

## 9. Recomendación

**Continuar** con el Paso 5 (extraer el motor de búsqueda detrás de `CatalogSearchPort`)
tras aprobación del propietario. No avanzar por iniciativa propia.

## 10. Correcciones posteriores a la revisión

Aplicadas sobre la misma rama `refactor/application-contracts`, partiendo del commit
publicado `d388b58`. No modifican `turn.py`/`tools.py` ni la ruta caliente, ni amplían
el alcance.

1. **Clasificación de errores desconocidos** (`app/application/contracts.py`): se añade el
   estado `CatalogSearchStatus.ERROR`. Un error no reconocido ya **no** se clasifica como
   `NO_CONSULTA`; se tipa como `ERROR`. Se conservan intactos los mapeos de
   `NO_AUTORIZADO`, `NO_CONSULTA`, `NO_ENCONTRADO` y `PROVEEDOR_NO_DISPONIBLE`. Pruebas:
   `test_error_desconocido_se_tipa_como_error`,
   `test_familia_no_consulta_no_se_contamina_con_error_desconocido`.
2. **Timeout** (`app/application/catalog_search.py`): `TimeoutError`/`asyncio.TimeoutError`
   se normaliza a `PROVEEDOR_NO_DISPONIBLE` (no a `NO_CONSULTA`), diferenciado de la
   propagación de `asyncio.CancelledError`. Pruebas:
   `test_use_case_timeout_es_proveedor_no_disponible`,
   `test_use_case_timeout_de_asyncio_tambien_es_proveedor_no_disponible`,
   `test_use_case_propaga_cancelacion`.
3. **Este informe**: se registra el commit original `d388b58` como publicado y se marcan
   las correcciones como posteriores a la revisión.

**Resultados tras las correcciones:** tests nuevos **26 passed**; suite completa
**278 passed**; `py_compile` OK; `git diff` sobre `turn.py`/`tools.py` **vacío**.
