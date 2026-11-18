# Paso 12 — Consolidación, regresión y readiness de producción (QA independiente)

> **Rol:** QA independiente y revisor de release. **No autor.** No se corrigió código.
> **Rama:** `release/pharmacy-architecture-v1` creada desde `c957944`
> (= integración lineal de los Pasos 7–11; ver §1).
> **Fecha:** 2026-11-14. **Entorno:** `.venv-test` (Python 3.11.16, pytest 9.1.1,
> pypdf 6.21.4), Windows 11. `PYTHONUTF8=1 PYTHONIOENCODING=utf-8 PYTHONPATH=.`.
> **Docker:** 29.6.1 (ARRIBA). **Proxy LLM 127.0.0.1:11500:** CAÍDO en esta corrida.
> **Estados:** PASS / FAIL / BLOCKED / NOT RUN. **No hay FAIL.**

---

## Resumen ejecutivo

El conjunto de los Pasos 7–11 vive en una cadena **lineal** de commits
(`c957944` → `dc38a54` → `9642142` → `c191773`/`61b5615` → Paso 7), de modo que
el HEAD del Staff Agent **ya es** la integración de los cuatro pasos. La rama de
release se creó desde ese SHA sin merges adicionales.

- **Suite completa:** 416 passed (0 fail). `compileall app` OK.
- **Regresiones deterministas** (F14, F15.1, G5/P2, G6, G13/P3, P1): **PASS**, idénticas a la línea base.
- **Migraciones** (incl. 016/017/018) contra **Postgres 16 desechable**: aplican limpio y son **idempotentes** (aplicadas 2×). Piloto Staff **cerrado** confirmado (0 empleados → todo denegado).
- **Seguridad Staff / aislamiento entre farmacias:** cobertura explícita de negativos (N1–N4, tenant-crossing, IDOR, replay, confirmación de mutación, logout, piloto cerrado). **PASS**.
- **Concurrencia / idempotencia / errores de proveedor / métricas no-op:** **PASS** a nivel unitario y de contrato.
- **Sin dependencias nuevas** (`requirements.txt` sin cambios vs `agent-qa`). Cambios casi 100% aditivos (8272+/47−), documentados con ADR-0001…0010 y PR-docs por paso.

**Hallazgos abiertos:** ninguno nuevo introducido por el release. Se mantienen dos
gates de *fail-open-cuando-no-configurado* **preexistentes** (F-2 firma Meta, F-1
binding de `provider_id`) que son **checklist de despliegue**, no defectos de código
de estos pasos. Ver §Hallazgos.

**Recomendación:** **GO CONDICIONADO** — apto para merge/stage de la rama de
arquitectura; el *go* a **producción** queda condicionado al checklist de despliegue
(F-2/F-1) y a cerrar los BLOCKED de entorno (CRM sandbox, pip-audit, backup/restore).
La aprobación final corresponde al propietario.

---

## 1. Cadena de PRs, SHAs y ADR (punto 1) — PASS

Verificado con `git merge-base --is-ancestor`: las tres ramas de los Pasos 8/9/10
son ancestros de `feat/pharmacy-staff-agent`. La cadena es lineal:

| Paso | Rama | SHA | Rol |
|---|---|---|---|
| 7 | `feat/pharmacy-order-workflow` | `6e5115c` | ancestro |
| 8 | `feat/clinical-handoff` | `61b5615` (+`c191773` QA-plan) | ancestro |
| 9 | `feat/pharmacy-actor-policies` | `9642142` | ancestro |
| 10 | `refactor/infrastructure-boundaries` | `dc38a54` | ancestro |
| 11 | `feat/pharmacy-staff-agent` | `c957944` | **HEAD** = integración |

ADR presentes y coherentes con el código: ADR-0001 (monolito modular), 0002 (LLM
sin autoridad), 0003 (puerta única de autorización), 0004 (guards última línea),
0005 (puertos sin migración), 0006 (handoff clínico fail-closed), 0007 (fuente de
verdad del pedido), 0008 (identidad Staff bloqueada), 0009 (fronteras infra),
0010 (identidad Staff por sesión — DESBLOQUEA 0008). PR-docs por paso presentes.

> Nota: estas ramas están en el remoto `origin`; el flujo de PR/aprobaciones en
> GitHub (reviews, checks) no se auditó desde el repo local. **NOT RUN** (revisar en GitHub).

## 2. Suite, compilación, deps (punto 2)

| Prueba | Comando | Resultado |
|---|---|---|
| Suite | `pytest tests/ -q` | **PASS** — `416 passed in 32.62s` |
| Compilación | `python -m compileall app` | **PASS** (exit 0) |
| `requirements.txt` vs baseline | `git diff agent-qa…release -- requirements.txt` | **PASS** — sin cambios (0 deps nuevas) |
| `pip check` | — | **BLOCKED** — `.venv-test` sin módulo `pip` |
| Auditoría CVE (`pip-audit`) | — | **BLOCKED** — sin `pip`/`pip-audit`; no se instaló nada sin autorización |

## 3. Regresiones deterministas (punto 3) — PASS

| Prueba | Comando | Resultado |
|---|---|---|
| P1 bucle | `eval/qa-2026-11-06/sonda_bucle.py` | **PASS** — A1/A2/A3/A4/A7/C0 `respuestas=1`, sin silencio |
| P2/G5 evidencia | `eval/qa-2026-11-06/sonda_g5.py` | **PASS** — bloquea B1–B7; pasa B8-2/B8-3/B9 |
| P3/G13 | `eval/g13-afirmacion-sin-accion/detector_g13.py` | **PASS** — 12 textos, 0 coincidencias (0 falsos positivos) |
| G6 lista | `tests/test_g6_lista_mostrada.py` | **PASS** — 15/15 |
| F15.1 término (determinista) | `eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` | **PASS** — `PRUEBAS: 10 HUECOS: 0`; 2ª puerta (`sugerir_generico`) NO consultó |
| F14 (determinista) | `eval/f14-busqueda/simular_casos.py --root . --out <tmp>` | **PASS** — JSON con `extraccion/relevancia/alias/matriz_typo` |
| F15.1 término (modelo real) | `… --model deepseek/…` | **BLOCKED** — proxy 127.0.0.1:11500 rechaza conexión |
| F15 corpus 50 frases | `eval/.../medir_corpus.py` | **NOT RUN** — requiere modelo real (proxy caído) |

Conteos por archivo de guards (dentro de los 416): `test_f14_busqueda` 102,
`test_f15_intencion` 64, `test_g5_evidencia` 32, `test_g13_accion` 12.

**Revisión de no-debilitamiento de guards (regla no-negociable 1 y 2):** los diffs
de los archivos protegidos son aditivos:
- `app/guards.py`: +2 plantillas constantes de derivación clínica (G14). Ningún guard existente alterado.
- `app/tools.py`: `buscar_medicamento` se despacha vía adaptador UC-01 y devuelve el **mismo** dict (`to_tool_dict()`); la puerta única `_autorizar_busqueda` permanece en el motor. `finalizar_pedido` registra el intento best-effort sin afirmar aceptación del CRM (sostiene G13).
- `app/turn.py`: G14 insertado como prioridad máxima (clasificación determinista sobre texto del cliente, fail-closed, no cancelable por carrito activo); la partición de mensajes se movió a `wtext` conservando fachada `_partir_mensaje_largo`. No toca F14/F15/F15.1.
- `app/webhook.py`: +`_provider_mismatch` (F-1, defensa IDOR en analytics).

## 4. DB desechable + migraciones (puntos 4 y 9) — PASS (parcial)

Postgres 16-alpine en contenedor desechable (`nea-qa-pg`, puerto 55432, DB `neaqa`).
Migraciones aplicadas con el propio `PgStore.migrate()`:

- **PASS** — `MIGRATE_OK x2` (idempotente; re-aplicar es seguro, como declaran los `IF NOT EXISTS`).
- **PASS** — tablas nuevas presentes: `order_attempt`, `clinical_handoff`, `staff_user`, `staff_session`, `staff_audit`.
- **PASS** — piloto cerrado: `staff_user` con 0 filas.
- **PASS** — `staff_audit` acepta INSERT (append-only); rollback documentado en la cabecera de `018_staff_identity.sql` (DROP TABLE en orden inverso).
- **BLOCKED** — backup/restore: requiere BD/instancia de producción; fuera del entorno local, no se usan datos reales.

No se usaron datos de clientes reales (DB vacía recién creada).

## 5. E2E de flujos (punto 5) — PASS a nivel de wiring / BLOCKED en vivo

Las pruebas de cableado turn-level cubren el recorrido sin CRM/modelo en vivo:
`test_catalog_search_wiring`, `test_order_finalize_wiring`, `test_clinical_handoff_wiring`
→ **24 passed**. Cubren: consulta de producto/precio/disponibilidad (catálogo),
carrito→finalización (registro de intento), y derivación clínica (handoff).

- **BLOCKED** — E2E end-to-end real (consulta→carrito→confirmación→seguimiento→handoff sobre CRM sandbox + modelo real): proxy LLM caído y sin credenciales de CRM sandbox (`CRM_BASE_URL`/`CRM_BOT_API_KEY`). `eval/f14-busqueda/sonda_catalogo.py` y `simular_conversaciones.py` siguen BLOCKED por esto.

## 6. Aislamiento entre farmacias y permisos Staff (punto 6) — PASS

`tests/test_staff_agent.py` (31 casos) + `app/staff/*`. Negativos ejercitados:

| Caso | Test | Resultado |
|---|---|---|
| No autenticado (N1) | `test_N1_no_autenticado_401_auditado` | PASS (401, auditado) |
| Tenant-crossing (N2, farmacia A opera en B) | `test_N2_tenant_crossing_denegado`, `test_politica_deny_tenant_mismatch_N2` | PASS |
| Rol insuficiente (N3, clerk→crónicos) | `test_N3_clerk_no_puede_cronicos_403` | PASS (403) |
| Sesión expirada (N4) | `test_N4_sesion_expirada_401` | PASS |
| IDOR analytics (`provider_id` del query ignorado) | `test_provider_id_del_query_se_ignora_si_coincide` | PASS |
| Mutación sin confirmación | `test_mutacion_sin_confirmacion_409_no_muta` | PASS (409, no muta) |
| Replay de mutación | `test_mutacion_con_confirmacion_y_replay_idempotente` | PASS (idempotente) |
| Revocación | `test_logout_revoca_sesion` | PASS |
| Piloto cerrado (sin empleados) | `test_piloto_cerrado_sin_empleados_todo_denegado` | PASS (deny-by-default) |
| Token nunca en claro | `test_token_no_se_guarda_en_claro` | PASS (SHA-256 / pbkdf2) |
| Superficie no montada | `test_superficie_no_montada_503` | PASS (503) |

Identidad (`app/staff/identity.py`): pbkdf2-hmac-sha256 con sal por usuario,
token Bearer hasheado SHA-256, `pharmacy_id`/`role` fijados al emitir y
**comparados** (nunca adoptados del query/body).

## 7. Concurrencia, idempotencia, timeouts, duplicados, recuperación (punto 7) — PASS

- Pedido: `test_uc_es_idempotente_por_order_attempt_id`, `test_reserva_concurrente_mismo_id_converge`, `test_reserva_repetida_mismo_id_no_crea_otro`.
- Clínico: `test_doble_solicitud_no_duplica_handoff`, `test_atencion_y_reanudacion_de_conversacion`.
- CRM (Paso 10): `test_crm_errors.py` distingue **fallo de proveedor** (5xx) de **no encontrado** (404) de **no autorizado** (401/403) — 6/6 PASS.
- Webhook duplicado: `mark_processed` con `ON CONFLICT DO NOTHING` (dedup por `wa_message_id`), ejercitado en la suite.

## 8. Logs, secretos/PII, métricas (punto 8) — PASS (con observación menor)

- Barrido de `logger.*` sobre `app/` por token/secret/password/api_key/bearer/diagnóstico: **sin fugas**. `webhook.py:176` sólo registra "token inválido" (no el valor). `app/staff/api.py` sólo emite `logger.exception("… error")` genérico.
- `app/metrics.py` (Paso 10): fachada **no-op total** cuando `METRICS_ENABLED=false` (default); `test_metrics_noop.py` 6/6 PASS, incluido "un sink que lanza no rompe el flujo".
- **Observación menor (preexistente, informativa):** `app/followup.py` registra `conv.wa_identity` (identidad WhatsApp) en varias líneas. Es comportamiento AS-IS, **no** introducido por este release; considerar enmascararlo en una pasada futura de hardening de logs.

## 9. Migraciones / backup / restore / rollback (punto 9)

Ver §4. Migraciones **PASS** (idempotentes, schema verificado), rollback documentado.
Backup/restore **BLOCKED** (sin instancia de producción; fuera del entorno local).

## 10. Cambios no documentados / deps / compatibilidad (punto 10) — PASS

- **Sin dependencias nuevas** (`requirements.txt` idéntico a baseline).
- Cambios aditivos; archivos existentes tocados (crm, config, main, sender, state, tools, turn, webhook, guards) revisados: aditivos y documentados por ADR/PR-docs.
- `config.py`/`.env.example`: sólo añaden `METRICS_ENABLED=false` (apagado por defecto). **Ningún flag de producción cambiado.**
- Staff router se monta siempre pero **fail-closed** (piloto cerrado → 503/deny). Compatibilidad hacia atrás preservada (fachadas, mismo formato externo de herramientas).

---

## Hallazgos (punto 11) — ninguno nuevo del release; 2 gates de despliegue preexistentes

### F-2 — Verificación de firma Meta *fail-open* cuando `META_APP_SECRET` no está configurado — **Severidad: ALTA (gate de producción)**
- **Dónde:** `app/webhook.py:35-42` (`verify_signature` → `return True` si `secret` vacío) y `:296`.
- **Reproducción:** desplegar sin `META_APP_SECRET` ⇒ cualquier POST a `/webhook` sin `x-hub-signature-256` es aceptado (mensajes forjados).
- **Naturaleza:** comportamiento AS-IS preexistente (conveniencia dev), **no** introducido por Pasos 8–11. QA no lo corrige.
- **Mitigación requerida antes de producción:** exigir `META_APP_SECRET` en prod (idealmente fail-closed si falta en entorno productivo).

### F-1 — Binding de `provider_id` en analytics *no-op* cuando `settings.provider_id` está vacío — **Severidad: MEDIA (gate de configuración)**
- **Dónde:** `app/webhook.py:_provider_mismatch` (binding sólo si `provider_id` fijado).
- **Impacto:** si una instancia productiva deja `provider_id` sin fijar, la protección IDOR de `/analytics/*` queda inerte y sólo la X-API-Key global protege (su posesión permitiría leer analytics de cualquier farmacia cambiando el query).
- **Nota:** es mejora de defensa-en-profundidad añadida en este release (F-1 fix); el residuo es el caso "no fijado". **Mitigación:** fijar `provider_id` en prod.

### Limitaciones de entorno (BLOCKED, documentadas; no inventadas)
- `pip check` / `pip-audit`: `.venv-test` sin `pip`; sin autorización no se instaló nada.
- F15.1 modelo-real y F15 corpus: proxy LLM `127.0.0.1:11500` caído.
- E2E en vivo / CRM sandbox: sin credenciales `CRM_BASE_URL`/`CRM_BOT_API_KEY`.
- Backup/restore: sin instancia de producción.
- Aprobaciones/checks de PR en GitHub: no auditados desde el repo local.

---

## Matriz consolidada (punto 12)

| # | Criterio (Paso 12) | Estado | Evidencia |
|---|---|---|---|
| 1 | Cadena PRs/SHAs/ADR | **PASS** (aprobaciones GitHub NOT RUN) | §1 |
| 2 | Suite + compile | **PASS** | 416 passed; compileall OK |
| 2 | pip check / CVE audit | **BLOCKED** | sin pip en venv |
| 3 | F14/F15.1/G5/G6/G13/P1/P2/P3 | **PASS** | §3 |
| 3 | F15.1 modelo-real / F15 corpus | **BLOCKED / NOT RUN** | proxy caído |
| 4 | DB desechable + migraciones | **PASS** | migrate x2 idempotente; schema verificado |
| 5 | E2E wiring | **PASS** | 24 passed |
| 5 | E2E en vivo (CRM+modelo) | **BLOCKED** | sin CRM sandbox/proxy |
| 6 | Aislamiento farmacias + Staff | **PASS** | 31 casos, negativos N1–N4 |
| 7 | Concurrencia/idempotencia/timeouts/dup/recuperación | **PASS** | §7 |
| 8 | Logs secretos/PII + métricas | **PASS** (obs. menor wa_identity) | §8 |
| 9 | Migraciones/rollback | **PASS** | §4 |
| 9 | Backup/restore | **BLOCKED** | sin instancia prod |
| 10 | Cambios no doc / deps / back-compat | **PASS** | 0 deps nuevas; aditivo |
| 11 | Hallazgos | F-2 (ALTA, prod), F-1 (MEDIA, config) — preexistentes | §Hallazgos |

## Recomendación: **GO CONDICIONADO**

El código de los Pasos 7–11 integrado en `release/pharmacy-architecture-v1` está
**verde en todo lo ejecutable localmente** (416 tests + regresiones + migraciones +
seguridad Staff), sin regresiones vs la línea base y sin debilitar guards. Apto para
**merge/stage** de la rama de arquitectura.

El *go a producción* queda condicionado a que el propietario:
1. Configure `META_APP_SECRET` (cierra F-2) y `provider_id` (cierra F-1) en el entorno productivo.
2. Cierre los BLOCKED de entorno con infraestructura real: CRM sandbox + proxy LLM (E2E en vivo y F15 modelo-real), `pip-audit` (CVE), y una prueba de backup/restore sobre la BD destino.
3. Verifique en GitHub las aprobaciones/checks de PR de cada paso.

No se hizo deploy, merge ni cambio de flags. La aprobación final es del propietario.
