# Runbook de migraciones — merge a `desarrollo`

> **Estado:** PREPARADO, **NO aplicado**. La base de datos **no se ha tocado** y el
> **backup todavía no se ha hecho**. No ejecutar nada de esto hasta tener el backup.
> **Fecha:** 2026-11-15. **Rama consolidada:** `origin/prototipo` (merge de la
> arquitectura v1 Pasos 7–11 + QA + caducidad de contexto).

## Qué le falta a `desarrollo`

`origin/desarrollo` (`6559228`) **ya tiene** las migraciones `001`–`015`, incluidas
`010_last_options_at`, `011_contexto_at` (caducidad de contexto) y `014`/`015`
(comprensión/intención). Por tanto, al mergear la arquitectura v1 a `desarrollo`
solo hay que aplicar **tres migraciones nuevas**:

| Migración | Crea | Tipo | Efecto sobre datos existentes |
|---|---|---|---|
| `016_order_attempt.sql` | tabla `order_attempt` + índice | `CREATE TABLE IF NOT EXISTS` | ninguno (tabla nueva) |
| `017_clinical_handoff.sql` | tabla `clinical_handoff` + índices (incl. único parcial anti-duplicado) | `CREATE TABLE IF NOT EXISTS` | ninguno (tabla nueva) |
| `018_staff_identity.sql` | tablas `staff_user`, `staff_session`, `staff_audit` + índices | `CREATE TABLE IF NOT EXISTS` | ninguno (tablas nuevas) |

**Todas son puramente aditivas:** crean tablas/índices nuevos con `IF NOT EXISTS`,
referencian `bot_conversation(id)` con `ON DELETE CASCADE`. **No** hay `ALTER` de
tablas existentes, **no** hay cambios de columnas, **no** hay backfill ni reescritura
de datos. Idempotentes: re-ejecutarlas es seguro. Compatibilidad hacia atrás total.

El **piloto Staff es cerrado**: `018` no siembra ningún empleado → mientras
`staff_user` esté vacía, la superficie `/staff/*` queda *fail-closed* (todo denegado).
Crear las tablas no habilita a nadie.

## Precondición obligatoria — Backup (PENDIENTE)

Antes de aplicar nada, respaldar la base de datos destino:

```bash
# Ajustar host/puerto/db/usuario reales de desarrollo.
pg_dump --no-owner --format=custom \
  -h <HOST> -p <PORT> -U <USER> -d <DB> \
  -f nea-desarrollo-$(date +%Y%m%d-%H%M%S).dump
```

Verificar que el `.dump` existe y tiene tamaño > 0 antes de continuar.

## Cómo aplicar (dos opciones; elegir una)

### Opción A — Automática al desplegar (recomendada, es el mecanismo actual)
El propio arranque aplica todas las `migrations/*.sql` en orden, idempotentes
(`app/db.py` → `PgStore.migrate()`). Al desplegar el código mergeado en
`desarrollo`, las tres migraciones nuevas se aplican solas. **No requiere paso SQL
manual.** Requiere, eso sí, backup previo (arriba) y una ventana de despliegue.

### Opción B — Manual antes del despliegue
Si se prefiere aplicar el esquema por separado (p. ej. para validar antes de subir
el código), ejecutar el bundle ya preparado **después del backup**:

```bash
psql -h <HOST> -p <PORT> -U <USER> -d <DB> \
  -v ON_ERROR_STOP=1 \
  -f docs/architecture/pending-migrations-desarrollo-016-018.sql
```

El bundle `docs/architecture/pending-migrations-desarrollo-016-018.sql` es una copia
VERBATIM de `migrations/016..018`, envuelta en una transacción. Vive bajo `docs/`
(no bajo `migrations/`) a propósito, para que `migrate()` no la recorra. Como es
idempotente, aplicar el bundle a mano y que luego el arranque vuelva a correr los
canónicos es inofensivo.

## Verificación (post-aplicación)

```sql
SELECT tablename FROM pg_tables WHERE schemaname='public'
  AND tablename IN ('order_attempt','clinical_handoff','staff_user','staff_session','staff_audit');
-- esperado: 5 filas

SELECT count(*) FROM staff_user;   -- esperado: 0 (piloto cerrado)
```

## Rollback

Solo tablas nuevas → rollback = `DROP` en orden inverso (seguro: nada preexistente
depende de ellas):

```sql
-- 018
DROP TABLE IF EXISTS staff_audit;
DROP TABLE IF EXISTS staff_session;
DROP TABLE IF EXISTS staff_user;
-- 017
DROP TABLE IF EXISTS clinical_handoff;
-- 016
DROP TABLE IF EXISTS order_attempt;
```

Si se usó la Opción A y algo falla en el despliegue, el rollback de esquema de
arriba + restaurar el `.dump` del backup devuelve el estado previo.

## Validación ya realizada (en entorno desechable, sin tocar datos reales)

- Las tres migraciones (y el set completo fusionado) aplican limpio y **×2 idempotente**
  contra un Postgres 16 desechable; tablas presentes; `staff_user` con 0 filas.
- Esto se hizo en contenedores de usar y tirar; **ninguna base de datos de
  desarrollo/producción fue tocada.**
