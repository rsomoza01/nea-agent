# PR — Paso 8: Seguridad clínica y derivación humana

> **Rama:** `feat/clinical-handoff` · **Base / HEAD inicial:** `6e5115c`
> (`feat/pharmacy-order-workflow`) · **Fecha:** 2026-11-13 · **Modo:** implementación de
> seguridad. **Objetivo:** derivar de forma segura, NO construir un médico virtual.
>
> **Decisiones del propietario (esta sesión):** (1) **cableado = guard determinista +
> registro** — se cambia el comportamiento observable para dejar de responder
> clínicamente, respaldado con regresión F14/F15.1/G5/G6/G13; (2) **NO añadir `clinico`**
> al catálogo de handoff del CRM — la derivación se mapea al motivo seguro `cliente` y la
> categoría clínica vive solo en el estado/auditoría interno de NEA.

## 1. Resumen ejecutivo

Se implementa la derivación clínica **fail-closed** de [ADR-0006](ADR/ADR-0006-handoff-clinico-fail-closed.md)
/ [USE-CASES.md](USE-CASES.md) UC-08 / PLAN §7-§8, sobre la **puerta de handoff existente**
(no se crea un segundo mecanismo de escalado):

1. **Clasificador conservador y determinista** (`app/application/clinical_handoff.clasificar`)
   de las nueve categorías del PLAN §7: diagnóstico, síntoma de alarma, dosis, interacción,
   contraindicación, embarazo/lactancia, pediatría, reacción adversa y sustitución
   individualizada. De **alta precisión**: dispara ante una solicitud de consejo clínico y
   **no** ante una consulta comercial (`¿tienen X?`, `precio`, `disponible`, `¿para qué
   sirve?`). **0 falsos positivos** sobre el corpus comercial de prueba.
2. **Guard G14 en `run_turn`** (máxima prioridad del chain): ante una categoría detectada,
   sustituye el texto por una **plantilla de recepción autorizada** (no diagnostica, no
   prescribe, no ajusta dosis) y **fuerza el handoff** — igual que G4/G9/G11.
3. **Máquina de estados** (detectado → solicitado → asignado → atendido → retomado/cerrado)
   con transiciones explícitas, **puerto `ClinicalHandoffStore`**, implementación en
   `MemoryStore` y `PgStore`, y **anti-duplicado idempotente** (una sola derivación abierta
   por conversación; un 2º mensaje clínico NO duplica el handoff y recibe un **mensaje de
   estado** autorizado).
4. **Dato clínico mínimo**: se persiste la **categoría** (enum) + correlación, **nunca el
   texto clínico** del cliente (PLAN §7 punto 5, ADR-0006).
5. **Fail-safe**: si el store o el CRM fallan, el cliente igual recibe la derivación y la
   evidencia queda registrada en NEA; la seguridad no depende de la persistencia.

El flujo comercial (F14/F15/F15.1/G5/G6/G13) **no se toca**: verificado por la suite
completa y las sondas.

## 2. Auditoría del handoff real (paso 1 del prompt)

| Aspecto | Realidad AS-IS | Evidencia |
|---|---|---|
| Ejecución del handoff | `tools._handoff` marca `handoff_reason`; `turn._safe_handoff` llama `crm.post_handoff` **tras** el mensaje | `tools.py:2650`, `turn.py:3021` |
| Catálogo de motivos CRM | Cerrado: `{cliente, modelo, error, ventana, hostilidad, medicamento_no_disponible}`; **sin** `clinico` | `crm.py:84` |
| Handoff determinista previo | hostilidad (3er strike), G4 (fraude), G9 (pide humano), G11 (promesa escalado) | `turn.py:666/671/1032/1061`, `guards.py` |
| Detección clínica | **No existía** | — |
| Proceso humano de derivación clínica | **No documentado** (PLAN §8 q6, abierta) | — |

Conclusión: la puerta sirve para **detener** el consejo autónomo y escalar, pero no tenía
taxonomía clínica, estados, anti-duplicado ni registro. El Paso 8 añade esa capa **sin**
crear una segunda puerta y **sin** debilitar las existentes.

## 3. Alcance y archivos

| Archivo | Cambio | Rol |
|---|---|---|
| `app/application/clinical_handoff.py` | **nuevo** | Clasificador puro + máquina de estados + puerto `ClinicalHandoffStore` + impl. en memoria + `DerivarConsultaClinicaUseCase`. Sin LLM, sin autorización. |
| `app/guards.py` | +37 | Plantillas constantes `TPL_DERIVACION_CLINICA` (recepción) y `TPL_DERIVACION_CLINICA_EN_CURSO` (estado). |
| `app/turn.py` | +58/−4 | Guard **G14** (1ª prioridad del chain): detecta, deriva, registra idempotente, elige recepción/estado. Protege la restauración de resumen y la cancelación por carrito (la seguridad clínica prevalece). |
| `app/tools.py` | +5 | `ToolRuntime.clinical_category` (efecto observable que lee turn.py). |
| `app/crm.py` | +7 | `canonical_handoff_reason`: `clinico/clínico → cliente` (motivo seguro del catálogo; no 422). |
| `app/state.py` | +75 | `Store` protocol + `MemoryStore`: `reservar_derivacion`/`derivacion_abierta`/`avanzar_estado_derivacion`. |
| `app/db.py` | +88 | `PgStore`: idem con índice único parcial + `ON CONFLICT DO NOTHING RETURNING`. |
| `migrations/017_clinical_handoff.sql` | **nuevo (validado en DB desechable)** | Tabla `clinical_handoff` + índice único **parcial** (anti-duplicado atómico). Rollback: `DROP TABLE`. |
| `tests/test_clinical_handoff.py` | **nuevo** | 32 pruebas: precisión del clasificador (positivos por categoría + negativos comerciales), estados, caso de uso idempotente, mapeo CRM, atención/reanudación. |
| `tests/test_clinical_handoff_wiring.py` | **nuevo** | 5 e2e sobre `run_turn`: derivación, fallo de CRM, doble solicitud, carrito activo, consulta comercial no deriva. |

**Comportamiento observable:** la única diferencia intencional es que una **consulta
clínica de consejo** ahora se deriva (mensaje de recepción + handoff) en lugar de ser
respondida por el LLM — el objetivo del Paso 8. Todo lo comercial se preserva.

## 3b. Validación contra Postgres desechable (regla no negociable 7)

Docker UP: `postgres:16-alpine`, **17 migraciones aplicadas en orden** (todas OK). Sobre
`clinical_handoff`:
- Estructura verificada: PK `clinical_handoff_id`, FK `conversation_id → bot_conversation`
  `ON DELETE CASCADE`, índice `idx_clinical_handoff_conv`, e índice **único parcial**
  `uq_clinical_handoff_abierto (conversation_id) WHERE state IN
  (detectado,solicitado,asignado,atendido)`.
- **Anti-duplicado en SQL real:** 1ª reserva (`dosis`) → 1 fila; 2ª con la 1ª **abierta**
  (otra categoría) → **0 filas** (`ON CONFLICT DO NOTHING`), total = 1, conserva la
  **primera** categoría. Tras `UPDATE state='cerrado'` en la 1ª, una nueva reserva → **sí**
  (total = 2): el índice parcial solo cuenta las abiertas. Coincide exactamente con
  `PgStore` y con la impl. en memoria.
- **Rollback:** `DROP TABLE clinical_handoff;`. Contenedor `--rm`.

## 4. Decisiones y justificación

1. **Fail-closed sobre la puerta existente.** G14 reutiliza `handoff_reason`/`_safe_handoff`
   (ADR-0006 decisión 3): no hay segundo mecanismo de escalado.
2. **Clasificador de alta precisión, no agresivo.** "Conservador" = conservador respecto a
   la SEGURIDAD (derivar ante consejo clínico claro), no derivar cualquier palabra de
   salud. Exige marco de solicitud de consejo (verbo de ingesta/permiso/pregunta clínica),
   nunca la mera mención de un fármaco o síntoma dentro de una compra → protege F14/F15.1.
3. **`clinico → cliente` en el CRM** (decisión del propietario). No se cambia el contrato
   del CRM (un reason fuera de catálogo da 422 — cert. 002); `cliente` es el bucket seguro
   más fiel. La **categoría** clínica no se envía al CRM.
4. **Anti-duplicado por índice único parcial.** Una sola derivación abierta por conversación;
   mismo idioma idempotente que `processed_message`/`order_attempt`. El 2º mensaje clínico
   recibe un **mensaje de estado** autorizado y **no** vuelve a pedir handoff (PLAN §8 p6).
5. **La seguridad clínica prevalece.** G14 no se cancela por carrito activo ni se pisa por
   la restauración del resumen del pedido (cambios acotados en `turn.py`).
6. **Dato clínico mínimo** (categoría, no texto) en tabla y logs (PLAN §7 p5 / §8 p8).

## 5. Pruebas (comandos y resultados)

Intérprete: `.venv-test/Scripts/python.exe` (3.11). `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.

| Prueba | Comando | Resultado |
|---|---|---|
| Compilación + import sin ciclos | `py_compile` + `import app.*` | **PASS** |
| **Clínica unitaria** (32) | `pytest tests/test_clinical_handoff.py` | **32 passed** |
| **Clínica e2e/wiring** (5) | `pytest tests/test_clinical_handoff_wiring.py` | **5 passed** |
| DB desechable (17 migr. + anti-duplicado + rollback) | `docker run postgres:16-alpine` + psql (§3b) | **PASS** |
| Suite completa | `pytest tests/ -q` | **374 passed** (330 baseline + 44 nuevos) |
| Sonda P1 / P2 / P3 | `PYTHONPATH=. python tests/harness_agente_p{1,2,3}.py` | **exit 0** (pasan) |
| F15.1 por término | `python eval/.../probe_autorizacion_por_termino.py` | **10 pruebas, 0 huecos, exit 0** |
| Guards G5 / G13 | incluidos en la suite (`test_g5_evidencia`, `test_g13_accion`) | **PASS** |
| Guard G6 (script) | `python tests/test_g6_lista_mostrada.py` | **15/15 OK, exit 0** |

Los 10 casos de prueba obligatorios del prompt (punto 10) quedan cubiertos: **derivación**
(unit+e2e), **fallo de CRM** (e2e, el turno no se cae y la derivación persiste),
**reintento** (idempotencia por `clinical_handoff_id`/índice parcial), **doble solicitud**
(e2e, 1 handoff), **atención** (avance de estados), **reanudación de conversación** (tras
retomar, una consulta nueva abre otra derivación).

## 6. Pruebas NO ejecutadas / BLOCKED

- **F14 (`simular_conversaciones`)** contra modelo real/proxy: **BLOCKED/NOT RUN** (igual
  que baseline y Paso 7; el F14 determinista `test_f14_busqueda.py` sí pasa en la suite).
- **Proceso humano real end-to-end** (asignación a un farmacéutico, SLA, canal de aviso):
  **BLOCKED** — no hay proceso aprobado (PLAN §8 q6). Se modela la máquina de estados y se
  prueba el avance de estados de forma determinista; el disparo real de
  asignado/atendido/retomado/cerrado lo hará el proceso humano cuando exista.

## 7. Reglas no negociables — cumplimiento

1. No se debilitó ni duplicó F14/F15/F15.1/G5/G6/G13 (suite + sondas verdes). ✓
2. No se creó bypass ni segunda puerta de autorización (G14 reutiliza la puerta de handoff;
   el clasificador no autoriza búsquedas). ✓
3. El LLM no autoriza, no decide permisos, no escribe SQL (clasificador determinista;
   reserva por `ON CONFLICT` parametrizado). ✓
4. **Cambio de comportamiento con pruebas y justificación** (autorizado por el propietario):
   derivar consultas clínicas. Respaldado por 37 pruebas nuevas + regresión completa. ✓
5. No se reescribió `turn.py`/`tools.py` (G14 es aditivo; `_finalizar_pedido` intacto). ✓
6. No se alteraron sondas/fixtures/tests existentes para forzar PASS. ✓
7. Migración `017` con ADR (0006), rollback (`DROP TABLE`) y prueba en DB desechable
   (§3b). Sin dependencias nuevas (solo stdlib). ✓
8. Sin push/merge/rebase/deploy. ✓
9. Estados reportados como PASS/BLOCKED/NOT RUN sin inventar. ✓
10. Lo no aprobado (proceso humano, motivo `clinico` en CRM) queda **fail-safe** y
    PENDIENTE; no se diseñó contra supuestos. ✓

## 8. 🔸 Pendiente de decisión del propietario / proceso

1. **Proceso humano de derivación clínica** (PLAN §8 q6): responsable, SLA, canal de aviso,
   y las duraciones reales de **timeout/reintento/escalamiento/cierre**. Hoy el default
   fail-safe: mientras el caso está abierto, el bot solo emite recepción/estado; no reanuda
   consejo. Al definirse, se cablea el avance de estados (ya modelado).
2. **Motivo `clinico` en el CRM:** si el negocio quiere separar la métrica de derivaciones
   clínicas en el CRM, confirmar que el catálogo lo acepta y cambiar el mapeo (hoy
   `cliente`). Cambio de contrato → requiere confirmación.
3. **Validación de las categorías con responsables farmacéuticos** (prompt punto 1): el
   conjunto implementado es el superset conservador del PLAN §7; cualquier ajuste de
   alcance (añadir/quitar categorías) se hace sobre el clasificador con pruebas.

## 9. Riesgos y rollback

- **Riesgo:** bajo-medio. El cambio deriva consultas clínicas (intencional). El riesgo
  principal sería un **falso positivo** que derive una consulta comercial; mitigado por el
  diseño de alta precisión (0 falsos positivos en el corpus) y la regresión F14/F15.1/G*.
  Un **falso negativo** mantiene el comportamiento previo (el LLM responde) — no empeora el
  estado actual; se puede endurecer el clasificador con pruebas.
- **Rollback:** `DROP TABLE clinical_handoff;` (migración 017) + revertir el commit (quita
  G14, las plantillas, el campo `clinical_category`, el mapeo CRM, los métodos de store y
  los módulos/tests nuevos). Sin datos productivos.

## 10. Recomendación

**Entregado y verificado**, listo para revisión del propietario. Cumple el objetivo del
Paso 8: **las consultas clínicas se derivan a un humano y el sistema demuestra el estado de
derivación**, sin dar consejo clínico, sin tocar el flujo comercial y manteniendo todos los
guards. El siguiente paso requiere la **definición del proceso humano real** para cablear el
avance de estados y, si el negocio lo pide, el motivo `clinico` en el CRM. **No hacer
push/merge/deploy por iniciativa propia.**
