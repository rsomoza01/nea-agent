# Plan de QA independiente — Paso 8 (`feat/clinical-handoff`)

> **Estado:** PLANIFICADO — listo para lanzar; **aún no ejecutado**.
> **Rol:** QA independiente y revisor de release. **NO implementa correcciones**, no
> modifica código/tests/fixtures/sondas, no hace merge ni push.
> **PR bajo prueba:** `feat/clinical-handoff` · base `6e5115c` → HEAD `61b5615`.
> **Entregable al ejecutar:** `docs/architecture/PR-feat-clinical-handoff-QA.md` +
> recomendación **APPROVE / REQUEST CHANGES / BLOCKED**.
> **Insumos:** [`PR-feat-clinical-handoff.md`](PR-feat-clinical-handoff.md),
> [`ADR-0006`](ADR/ADR-0006-handoff-clinico-fail-closed.md), [`USE-CASES.md`](USE-CASES.md)
> UC-08, [`PLAN`](PLAN-MODERNIZACION-FARMACIA.md) §7-§8, plantilla QA de
> [`PROMPTS-MODERNIZACION.md`](PROMPTS-MODERNIZACION.md).

## 0. Prompt para lanzar al agente QA

```text
Actúa como QA independiente del PR feat/clinical-handoff (Paso 8). No implementes
correcciones ni modifiques código, tests, fixtures ni sondas. No hagas merge ni push.
Sigue docs/architecture/PR-feat-clinical-handoff-QA-PLAN.md paso por paso, reproduce los
casos listados (incluidos negativos y límite), clasifica cada criterio
PASS/FAIL/BLOCKED/NOT RUN con evidencia (comando + salida), reporta defectos con archivo,
línea, pasos de reproducción y severidad, y entrega
docs/architecture/PR-feat-clinical-handoff-QA.md con una recomendación
APPROVE / REQUEST CHANGES / BLOCKED. No avances a otra etapa.
```

## 1. Preparación del entorno

| Ítem | Comando / verificación | Esperado |
|---|---|---|
| Rama y HEAD | `git rev-parse --abbrev-ref HEAD; git rev-parse HEAD` | `feat/clinical-handoff` @ `61b5615` |
| Árbol limpio | `git status --short` | vacío |
| Diff del PR | `git diff --stat 6e5115c..61b5615` | 10 archivos (6 mod + 4 nuevos) + 2 docs |
| Intérprete | `.venv-test/Scripts/python.exe` (Python 3.11) | sin instalar/actualizar paquetes |
| Encoding (Windows) | `PYTHONUTF8=1 PYTHONIOENCODING=utf-8` | evita crash cp1252 en emojis |
| Docker | `docker ps` | UP (para DB desechable); si DOWN → §6 BLOCKED |
| `pip check` | `.venv-test/Scripts/python.exe -m pip check` | sin conflictos / sin deps nuevas |

**Confirmar alcance del diff** (no debe tocar nada fuera de lo declarado): revisar que
`git diff 6e5115c..61b5615 -- app/turn.py app/tools.py` sean **aditivos** (G14, campo
`clinical_category`), sin reescritura ni cambios en el orden de autorización/tools/guards.

## 2. Barreras que deben seguir INTACTAS (no relajadas)

Objetivo: probar que el Paso 8 **no debilitó ni duplicó** ninguna barrera vigente.

| Barrera | Comando | Criterio |
|---|---|---|
| Suite completa | `pytest tests/ -q` | **374 passed** (330 baseline + 44 nuevos); 0 fallos/errores |
| Sonda P1 | `PYTHONPATH=. python tests/harness_agente_p1.py; echo $?` | exit **0** |
| Sonda P2 | `PYTHONPATH=. python tests/harness_agente_p2.py; echo $?` | exit **0** |
| Sonda P3 | `PYTHONPATH=. python tests/harness_agente_p3.py; echo $?` | exit **0** |
| F15.1 por término | `python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py; echo $?` | **10 pruebas, 0 huecos**, exit 0 |
| G5 / G13 | `pytest tests/test_g5_evidencia.py tests/test_g13_accion.py -q` | **passed** |
| G6 (script) | `python tests/test_g6_lista_mostrada.py; echo $?` | **15/15 OK**, exit 0 |
| F14 determinista | `pytest tests/test_f14_busqueda.py -q` | **passed** |
| Puerta única F15.1 | Inspección: `tools._autorizar_busqueda` sigue siendo la única puerta; el clasificador clínico **no autoriza búsquedas** | sin segunda puerta |

## 3. Funcionalidad nueva — pruebas a reproducir

### 3a. Pruebas automatizadas del PR
| Prueba | Comando | Esperado |
|---|---|---|
| Unitarias clínicas (32) | `pytest tests/test_clinical_handoff.py -q` | 32 passed |
| E2E/wiring (5) | `pytest tests/test_clinical_handoff_wiring.py -q` | 5 passed |

### 3b. Casos obligatorios del prompt (punto 10) — mapa de cobertura
| Caso | Dónde se prueba | Verificar |
|---|---|---|
| Derivación | `test_consulta_clinica_se_deriva_y_no_aconseja` | responde `TPL_DERIVACION_CLINICA`, handoff pedido, categoría registrada |
| Fallo de CRM | `test_derivacion_sobrevive_fallo_del_crm` | turno no se cae; derivación persiste en NEA |
| Reintento / idempotencia | unit `test_doble_solicitud_no_duplica_handoff` + índice parcial | mismo `clinical_handoff_id`; 1 fila |
| Doble solicitud | wiring `test_doble_solicitud_no_duplica_handoff` | 1 handoff CRM; 2º turno → mensaje de estado |
| Atención | unit `test_atencion_y_reanudacion_de_conversacion` | avance solicitado→asignado→atendido |
| Reanudación | idem | tras retomar, consulta nueva abre otra derivación |

### 3c. Casos límite / negativos a reproducir manualmente (anti-falsos-positivos)
Ejecutar `clinical_handoff.clasificar(texto)` sobre estos y confirmar el veredicto. **El
riesgo principal del PR es un falso positivo que derive una consulta comercial.**

- **Deben derivar** (≠ None), una por categoría: diagnóstico, síntoma de alarma, dosis,
  interacción, contraindicación, embarazo/lactancia, pediatría, reacción adversa,
  sustitución individualizada (ver corpus en `tests/test_clinical_handoff.py`).
- **NO deben derivar** (None): `¿tienen paracetamol?`, `precio del losartán`, `¿hay
  ibuprofeno disponible?`, `quiero 2 cajas`, `¿tienen el genérico de Daflon?`, `pañales
  para mi bebé`, `¿para qué sirve el omeprazol?`, `¿tienen jarabe para la tos de niños?`,
  `¿tienen algo para el dolor de cabeza?`, `LISTO, confirmo`.
- **QA adicional sugerido** (ampliar el corpus negativo con frases reales de los `eval/`
  y `run_*.json` de F14): confirmar 0 falsos positivos sobre términos de catálogo reales.
  Reportar cualquier derivación de una consulta comercial como **defecto de severidad alta**.

### 3d. Integración (opcional, con stubs del arnés)
Reproducir en `run_turn` vía el patrón de `test_clinical_handoff_wiring.py`:
- consulta clínica **con carrito activo** → deriva igual (no se cancela el handoff);
- consulta **comercial** → no deriva, no handoff (flujo de catálogo intacto).

## 4. Base de datos y migración (regla no negociable 7)

Con Docker UP, en Postgres desechable (`postgres:16-alpine`, `--rm`):
1. Aplicar **las 17 migraciones en orden**; confirmar que `017_clinical_handoff.sql` crea
   la tabla, el FK `ON DELETE CASCADE`, `idx_clinical_handoff_conv` y el índice **único
   parcial** `uq_clinical_handoff_abierto (conversation_id) WHERE state IN
   (detectado,solicitado,asignado,atendido)`.
2. **Anti-duplicado en SQL real:** 1ª reserva → 1 fila; 2ª con la 1ª abierta → **0 filas**
   (ON CONFLICT DO NOTHING), total=1, conserva la 1ª categoría; tras cerrar la 1ª, nueva
   reserva → sí (total=2). Debe coincidir con `PgStore.reservar_derivacion` y `MemoryStore`.
3. **Rollback:** `DROP TABLE clinical_handoff;` sin error. Sin datos productivos.
4. Comando de referencia: ver §3b de `PR-feat-clinical-handoff.md`.
   Si Docker está DOWN → **BLOCKED** (no PASS).

## 5. Seguridad, logs y datos clínicos (PLAN §7 p5 / §8 p8)

- **No se vuelca texto clínico ni PII** en logs ni en la tabla: solo la **categoría** (enum)
  y la correlación. Revisar:
  - `grep -n "clinical_category\|G14\|clinical_handoff\|TPL_DERIVACION" app/turn.py app/guards.py`
    → confirmar que los logs usan la **categoría**, nunca `user_text` clínico.
  - Migración/DDL `017`: la tabla no tiene columna de texto libre del cliente.
- **CRM:** `canonical_handoff_reason("clinico") == "cliente"`; `"clinico"` **no** está en
  `HANDOFF_REASONS` (no se cambió el contrato del CRM). Verificar que no se envía la
  categoría clínica al CRM.
- **Fail-safe:** confirmar que un fallo del store/CRM no impide la derivación (cubierto por
  e2e; revisar los `try/except` en `turn.py` G14 y `_safe_handoff`).

## 6. Matriz de resultados (a completar al ejecutar)

| # | Criterio | Resultado | Evidencia |
|---|---|---|---|
| 1 | Rama/HEAD/diff conformes | ⬜ | |
| 2 | `pip check` sin deps nuevas | ⬜ | |
| 3 | Suite completa 374 passed | ⬜ | |
| 4 | P1/P2/P3 exit 0 | ⬜ | |
| 5 | F15.1 10/0 huecos | ⬜ | |
| 6 | G5/G6/G13 passed | ⬜ | |
| 7 | F14 determinista passed | ⬜ | |
| 8 | Unitarias clínicas 32 passed | ⬜ | |
| 9 | E2E clínicas 5 passed | ⬜ | |
| 10 | 0 falsos positivos (corpus negativo ampliado) | ⬜ | |
| 11 | Carrito activo → deriva igual | ⬜ | |
| 12 | DB desechable: migración + anti-duplicado + rollback | ⬜ | |
| 13 | Logs sin texto clínico / PII | ⬜ | |
| 14 | CRM: `clinico→cliente`, sin cambio de contrato | ⬜ | |
| 15 | Sin segunda puerta de autorización | ⬜ | |

Leyenda: PASS / FAIL / BLOCKED / NOT RUN. Un FAIL en #3-#9, #10 o #15 ⇒ **REQUEST CHANGES**.
BLOCKED solo por infraestructura (Docker/CRM sandbox), nunca por falta de evidencia.

## 7. Defectos y recomendación

- Reportar cada defecto con: archivo, línea, pasos de reproducción, severidad (alta =
  falso positivo comercial, barrera relajada o PII en logs; media = falso negativo clínico;
  baja = cosmética/doc).
- Recomendación final: **APPROVE** (todo PASS salvo BLOCKED de infraestructura documentado),
  **REQUEST CHANGES** (algún FAIL accionable), o **BLOCKED** (no se pudo ejecutar el núcleo).
- No corregir código durante el QA. No merge/push. La aprobación final es del propietario.
