# PR — Paso 11: Staff Agent (identidad de empleado + superficie interna, piloto cerrado)

> **Rama:** `feat/pharmacy-staff-agent` · **Base:** `dc38a54`
> (`refactor/infrastructure-boundaries`) · **Fecha:** 2026-11-14 · **Modo:** nueva
> superficie funcional; requiere seguridad aprobada (Pasos 8-10 + modelo de identidad).
>
> ## ✅ Resultado: **IMPLEMENTADO** (desbloquea ADR-0008)

## 1. Resumen ejecutivo

El Paso 11 estaba **bloqueado** por [`ADR-0008`](ADR/ADR-0008-identidad-staff-bloqueada.md):
NEA no tenía identidad verificable de empleados. El propietario **aprobó el modelo de
identidad** (sesión de empleado con token emitido). Este PR lo implementa y construye el
**Staff Agent como piloto cerrado**, reutilizando los casos de uso de dominio y aplicando
la autorización server-side diseñada en el Paso 9.

Decisión de identidad: **sesión + token emitido** (ver
[`ADR-0010`](ADR/ADR-0010-identidad-staff-sesion.md)), elegida sobre "API-key por
empleado" porque es la única que satisface las 3 condiciones de desbloqueo de
`SECURITY-ACTOR-POLICIES.md` §8 y hace **ejercitable** la negativa N4 (identidad
expirada).

## 2. Operaciones internas autorizadas, clasificadas por riesgo (punto 1)

Catálogo **cerrado** (`app/staff/policy.OPERACIONES`). El piloto expone un subconjunto
mínimo; ampliarlo exige clasificar y añadir política explícita.

| Operación | Riesgo | Rol mínimo | Mutación | Confirmación | Reutiliza |
|---|---|---|---|---|---|
| `analytics.top_meds` | bajo | clerk | no | no | `store.top_med_queries` |
| `order.lookup` | bajo | clerk | no | no | `store.obtener_intento` |
| `analytics.chronic_patients` | medio | pharmacist | no | no | `store.chronic_patients` |
| `chronic.mark_consent` | medio | pharmacist | **sí** | **sí** | `store.marcar_consent` |

## 3. Cómo se cumplen los 10 puntos del prompt

| # | Punto | Cómo se cumple |
|---|---|---|
| 1 | Operaciones internas por riesgo | §2 (catálogo cerrado clasificado) |
| 2 | Reutiliza casos de uso; no duplica lógica Customer | Las 4 operaciones delegan en el `Store` de dominio; `app/staff/` solo añade identidad/política/auditoría |
| 3 | Comprobar actor/rol/farmacia/recurso/precondición en servidor | `policy.autorizar` deny-by-default; `AuthorizationContext` resuelto en backend desde la sesión |
| 4 | Mutaciones con confirmación explícita cuando el riesgo lo amerita | `chronic.mark_consent` → 409 `confirmation_required` sin `confirm:true` |
| 5 | El LLM no construye SQL ni invoca operaciones arbitrarias | Catálogo cerrado + consultas parametrizadas; el LLM no participa en `/staff/*` |
| 6 | Auditoría de consultas sensibles y mutaciones | `staff_audit` append-only; se auditan **autorizados y denegados** |
| 7 | Separación de herramientas Customer/Staff + schemas mínimos por rol | Router `/staff/*` aparte; `StaffStore` separado del `Store`; rol mínimo por operación |
| 8 | Pruebas de escalamiento/tenant/identidad/replay/no-permitidas | `tests/test_staff_agent.py` (30 pruebas) — ver §5 |
| 9 | No acceso a datos clínicos de pacientes sin control reforzado | No se expone handoff ni texto clínico; crónicos (medio) exigen rol farmacéutico; auditoría sin PII/clínico |
| 10 | Piloto cerrado; no habilitar a todos por defecto | Sin empleados sembrados → fail-closed total; provisión manual |

## 4. Alcance y archivos

| Archivo | Tipo | Contenido |
|---|---|---|
| `migrations/018_staff_identity.sql` | nuevo | `staff_user`, `staff_session`, `staff_audit` (append-only) |
| `app/staff/identity.py` | nuevo | Modelos, hashing pbkdf2 (stdlib), sesión TTL/revocable, `StaffStore` + impl en memoria |
| `app/staff/policy.py` | nuevo | `AuthorizationContext`, decisión tipada deny-by-default, catálogo de operaciones por riesgo |
| `app/staff/pg.py` | nuevo | `PgStaffStore` (sobre el pool del `PgStore`) |
| `app/staff/api.py` | nuevo | Router `/staff/*`: login/logout + piloto cerrado; auditoría de todo |
| `app/state.py` | editado | `AppContext.staff: StaffStore \| None` |
| `app/main.py` | editado | Monta `PgStaffStore` + router `/staff` en el lifespan |
| `app/webhook.py` | editado | **Fix F-1**: `_provider_mismatch` liga `provider_id` a `settings.provider_id` en `/analytics/*` (mono-tenant) |
| `tests/test_staff_agent.py` | nuevo | 30 pruebas (identidad, política, superficie HTTP, negativas N1-N4) |
| `docs/architecture/ADR/ADR-0010-*.md` | nuevo | ADR del modelo de identidad (desbloquea ADR-0008) |

Sin dependencias nuevas (hashing con `hashlib`/`secrets`/`hmac` de stdlib).

## 5. Pruebas (puntos 8-9) — todas verdes

`tests/test_staff_agent.py`: **30 passed**. Suite completa: **416 passed** (386 previos +
30; cero regresiones). Entorno: `.venv-test`, `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`.

Negativas cubiertas (punto 8):
- **Escalamiento de privilegios** → `test_N3_clerk_no_puede_cronicos_403`, `test_politica_deny_rol_insuficiente_N3`.
- **Tenant crossing** → `test_N2_tenant_crossing_denegado` (empleado de A con `provider_id=B` → 403; datos no cruzan).
- **Identidad no autenticada** → `test_N1_no_autenticado_401_auditado`.
- **Identidad expirada / revocada** → `test_N4_sesion_expirada_401`, `test_logout_revoca_sesion`.
- **Replay** → `test_mutacion_con_confirmacion_y_replay_idempotente` (mutación idempotente, ambos intentos auditados).
- **Operaciones no permitidas** → `test_politica_deny_operacion_no_permitida` (clave fuera del catálogo → denegado).
- **Piloto cerrado** → `test_piloto_cerrado_sin_empleados_todo_denegado` (sin empleados, login 401).

## 6. Provisión de empleados (fuera de runtime — piloto cerrado)

El esquema no siembra empleados. Para habilitar el piloto, el propietario crea usuarios
explícitamente (p. ej. en una sesión administrativa controlada):

```python
from app.staff.identity import construir_secret_hash, Role
hash_ = construir_secret_hash("<secreto-del-empleado>")
await ctx.staff.crear_staff_user(
    pharmacy_id="<provider_id>", username="<usuario>", role=Role.PHARMACIST, secret_hash=hash_,
)
```

Luego el empleado obtiene su token con `POST /staff/login`.

## 7. Recomendación

**Mergear como piloto cerrado.** Acciones del propietario:
1. Provisionar los empleados del piloto (ninguno está habilitado por defecto).
2. Decidir si se aborda **F-2** (exigir firma Meta en producción) como hardening aparte.
3. Validar en QA independiente (Paso 12) antes de ampliar el catálogo de operaciones.
