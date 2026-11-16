# ADR-0005 — Introducir puertos de aplicación sobre los contratos existentes, sin migración de datos

- **Estado:** Aceptada (diseño)
- **Fecha:** 2026-11-10
- **Paso del plan:** 3 (prepara Paso 4)
- **Relacionada con:** [`../TO-BE.md`](../TO-BE.md) §4, §8; [`../MIGRATION-ROADMAP.md`](../MIGRATION-ROADMAP.md); PLAN §4

## Contexto

`app/state.py` ya define `Store` como `Protocol` (`:181-327`) con dos
implementaciones: `PgStore` (`app/db.py`) y `MemoryStore` (tests). `CrmClient` y
`OpenAiLlm` son adaptadores concretos. `ToolRuntime` se construye uno por turno
con esas dependencias inyectadas (`app/turn.py:564`), y los tests ya inyectan
fakes vía `create_app(ctx)` (`app/main.py:50,113-115`).

Existe ya un hueco de paridad: `MemoryStore` implementa analytics con firmas más
simples que `PgStore` (DEPENDENCY-MAP R6).

## Decisión

En el Paso 4 se introducen **puertos mínimos** (interfaces que los casos de uso
requieren: catálogo, pedidos/repositorio, reloj) **reutilizando los contratos
existentes** (`Store`, `CrmClient`, `OpenAiLlm`) como adaptadores. **No** se crea
una segunda jerarquía de clientes HTTP ni de almacenamiento, y **no** se cambia el
esquema de base de datos en esta fase.

Los puertos se definen **solo cuando un caso de uso real los necesita** (PLAN §3.2,
§4), no de forma especulativa.

## Alternativas consideradas

| Alternativa | Por qué se descarta |
|---|---|
| Repositorios nuevos con su propio pool/HTTP | Duplica infraestructura y fuentes de verdad (DEPENDENCY-MAP R8). |
| Migrar DB para "ordenar" el modelo de pedido ahora | PLAN §6 regla 9: no cambiar esquema sin necesidad, migración y rollback; el pedido real depende de una decisión abierta (ver [ADR-0007](ADR-0007-fuente-verdad-pedido.md)). |
| Puertos exhaustivos por adelantado | Carpetas/interfaces vacías prohibidas (PLAN §3.2). |

## Consecuencias

- **Positivas:** el código nuevo se invoca con dependencias explícitas y los tests
  existentes siguen pasando (criterio de salida del Paso 4).
- **Negativas:** convivencia temporal de "contrato viejo" (`Store`) y "puerto
  nuevo" tras adaptadores; hay que evitar que diverjan.
- **Acción derivada:** cerrar la paridad `MemoryStore`↔`PgStore` cuando el puerto
  de analytics/pedidos se formalice (Paso 9/10).
