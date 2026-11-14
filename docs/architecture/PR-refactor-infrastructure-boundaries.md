# PR — Paso 10: Fronteras de infraestructura, resiliencia y observabilidad

> **Rama:** `refactor/infrastructure-boundaries` · **Base:** `9642142`
> (`feat/pharmacy-actor-policies`) · **Fecha:** 2026-11-13 · **Modo:** robustez
> incremental.
>
> **Decisión del propietario (esta sesión):** alcance **docs + seam seguro no-op**
> — documentar contratos + ADR y añadir solo código **aditivo y de bajo riesgo**
> (fachada de métricas no-op + subtipo `CrmUnauthorized`), con pruebas. **No** se
> cablean métricas/logs en los adaptadores (eso es "hardening ampliado", queda
> como deuda R-10.2 pendiente de aprobación).

## 1. Resumen ejecutivo

La auditoría confirma que la resiliencia de NEA ya es madura (timeouts por
dependencia, backoff en workers, `LlmExhausted`, colas persistentes 24 h,
idempotencia `ON CONFLICT`). El Paso 10 **formaliza** eso y cierra tres huecos reales
con el mínimo riesgo:

1. **Contratos documentados** en [`RESILIENCE.md`](RESILIENCE.md): timeout + taxonomía
   de error por dependencia, reintento/idempotencia, modo degradado + recuperación,
   catálogo de métricas, reglas de redacción de logs.
2. **Taxonomía de error (punto 7):** nuevo `CrmUnauthorized` (401/403) que separa **no
   autorizado** de **fallo de proveedor** (`CrmError`) y de **no encontrado** (404→None).
   Subclase de `CrmError` → retrocompatible.
3. **Métricas no-op (puntos 6, 10):** `app/metrics.py`, patrón de `observability.py`.
   Inerte sin `METRICS_ENABLED`; catálogo completo (latencia/error-rate por dependencia,
   turnos sin respuesta, autorización de búsqueda, resultado parcial, handoff, pedidos).
4. **Sin circuit breaker (punto 3)** ni dependencias nuevas: no hay caso medido; se
   decide con ADR y métricas reales ([ADR-0009](ADR/ADR-0009-fronteras-infra-resiliencia.md)).

## 2. Archivos

| Archivo | Tipo | Contenido |
|---|---|---|
| `app/metrics.py` | nuevo | Fachada de métricas no-op + catálogo + taxonomía de error |
| `app/crm.py` | editado | `CrmUnauthorized(CrmError)` + tipado 401/403 centralizado en `_request` |
| `app/config.py` | editado | flag `metrics_enabled: bool = False` |
| `.env.example` | editado | `METRICS_ENABLED=false` documentado |
| `tests/test_metrics_noop.py` | nuevo | 7 pruebas: no-op total, flag, sink, latencia, fallo, sink roto |
| `tests/test_crm_errors.py` | nuevo | 5 pruebas: 401/403→Unauthorized, 404→None, 5xx/red→CrmError |
| `docs/architecture/RESILIENCE.md` | nuevo | Contratos completos + deuda R-10.1/2/3 |
| `docs/architecture/ADR/ADR-0009-...md` | nuevo | Decisiones del paso |
| `docs/architecture/PR-refactor-infrastructure-boundaries.md` | nuevo | Este documento |

**Runtime tocado:** solo aditivo (nuevo módulo + subtipo de excepción + flag). Sin
cambios de comportamiento con la config por defecto (`METRICS_ENABLED=false`, sink no-op).

## 3. Cumplimiento de los 10 puntos

| # | Punto | Cómo |
|---|---|---|
| 1 | Contratos de timeout/error por dependencia | `RESILIENCE.md` §1-§2 |
| 2 | No reintentar mutación no idempotente a ciegas | §3 (sender R-10.1 documentado; 409→abandona) |
| 3 | Backoff/breaker solo si hay caso medido | breaker **no** añadido; backoff ya existe (§3, ADR §4) |
| 4 | Propagar correlation_id/turn_id/pharmacy_id | §4: traza Langfuse hoy; logs planos = R-10.2 (diseñado) |
| 5 | No loguear tokens/keys/payloads/PII | §6 reglas; métricas sin texto libre del cliente |
| 6 | Métricas útiles | `app/metrics.py` + §5 catálogo completo |
| 7 | Distinguir fallo/no-encontrado/no-autorizado | `CrmUnauthorized` + §2 |
| 8 | Modo degradado y recuperación | §7 |
| 9 | Probar timeout/caída/malformado/reintento/idempotencia | §8: +12 pruebas; socket-timeout real = 🔸 diseñado |
| 10 | Observabilidad no-op sin proveedor | `metrics.py` no-op + `METRICS_ENABLED=false` por defecto |

## 4. Verificación

```
pytest tests/ -q   →  386 passed  (374 previas + 12 nuevas), 0 regresiones
pytest tests/test_metrics_noop.py tests/test_crm_errors.py -q  →  12 passed
```

## 5. Recomendación

**APPROVE** como robustez incremental de bajo riesgo. Seguimiento (deuda en §9 de
`RESILIENCE.md`), cada uno opcional y con su propia decisión:

- **R-10.2** (cablear correlación en logs) y métricas en call-sites → "hardening
  ampliado": requiere tu OK (toca runtime en producción).
- **R-10.1** (idempotencia del sender) y **PG `command_timeout`** → vigilar con las
  métricas antes de actuar.
- **Backend de métricas real** (Prometheus/OTel) → ADR propio + medición.
