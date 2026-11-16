# ADR-0009 — Fronteras de infraestructura, resiliencia y observabilidad

- **Estado:** Aceptada (robustez incremental)
- **Fecha:** 2026-11-13
- **Paso del plan:** 10 (Fronteras de infraestructura, resiliencia y observabilidad)
- **Relacionada con:** [`../RESILIENCE.md`](../RESILIENCE.md), [`../DEPENDENCY-MAP.md`](../DEPENDENCY-MAP.md),
  [`../AS-IS.md`](../AS-IS.md) §7, [`ADR-0002`](ADR-0002-llm-sin-autoridad.md), PLAN §10

## Contexto

Los adaptadores de NEA (PostgreSQL, CRM, LLM, media, workers relay/sender) ya tienen
una resiliencia **madura pero no documentada**: timeouts por dependencia, reintentos
con backoff en los workers, degradación en silencio del LLM (`LlmExhausted`), colas
persistentes con tope de 24 h e idempotencia por gates `ON CONFLICT`. El Paso 10 pide
formalizar contratos (timeout/error/degradado), métricas útiles y propagación de
correlación, **sin introducir complejidad ni dependencias injustificadas**.

El modo es **robustez incremental**: medir antes de añadir, y mantener no-op cuando no
haya proveedor de observabilidad (como ya hace `observability.py` con Langfuse).

## Decisión

1. **Documentar los contratos, no reescribir los adaptadores.** Se publica
   [`RESILIENCE.md`](../RESILIENCE.md) con: contrato de timeout y taxonomía de error por
   dependencia, política de reintento/idempotencia, modo degradado + recuperación,
   catálogo de métricas y reglas de redacción de logs. El comportamiento vigente se
   conserva; los cambios de código son **aditivos y de bajo riesgo**.

2. **Taxonomía de error explícita (punto 7):** se distingue **fallo de proveedor**
   (`CrmError`, 5xx/red) de **no encontrado** (404 → los métodos devuelven `None`, no
   lanzan) y de **no autorizado** (`CrmUnauthorized`, 401/403 — **nuevo subtipo**,
   subclase de `CrmError` para no romper `except CrmError`). El 409 ya era `CrmConflict`.
   El vocabulario (`provider_failure`/`not_found`/`not_authorized`/`conflict`/`timeout`/
   `malformed`) se comparte con las etiquetas de métricas.

3. **Métricas como fachada no-op (puntos 6 y 10):** se añade `app/metrics.py`, mismo
   patrón lazy no-op que `observability.py`. **Sin sink configurado, no cuesta nada**;
   con `METRICS_ENABLED=true` emite líneas estructuradas al logger `nea.metrics`. Define
   el catálogo (latencia/error-rate por dependencia, turnos sin respuesta, autorización
   de búsqueda, resultado parcial, handoff, pedidos). Es la **costura**: el cableado en
   los call-sites es incremental y opcional.

4. **NO se añade circuit breaker (punto 3).** No hay un caso medido que lo justifique:
   el LLM ya degrada con reintentos acotados y los workers ya hacen backoff con tope de
   24 h. Un breaker sería complejidad especulativa. Se reconsiderará **con métricas de
   error-rate reales** (para eso está el catálogo del punto 3).

5. **NO se introducen dependencias ni plataformas nuevas.** Prometheus/statsd/OTel
   quedan fuera: enchufarlos requerirá un ADR propio con medición de coste/beneficio. El
   sink por defecto usa solo `logging` de la stdlib. `httpx.MockTransport` (ya en la
   dependencia `httpx`) cubre las pruebas de error del CRM sin red.

## Alternativas consideradas

| Alternativa | Por qué se descarta / aplaza |
|---|---|
| Añadir Prometheus/OpenTelemetry ya | Dependencia + plataforma nuevas sin medición; viola el guardarraíl del paso. La fachada no-op deja el camino listo sin pagar el coste. |
| Circuit breaker en CRM/LLM | Sin caso medido; los reintentos acotados + backoff + degradación ya cubren el fallo observado. Complejidad especulativa. |
| Reintentar el envío no idempotente del sender siempre | Riesgo de doble entrega. Se documenta el *at-least-once* existente como contrato, no se agrava. |
| Cablear métricas en todos los adaptadores ahora | Es "hardening ampliado": toca runtime de un bot en producción. Se entrega la costura; el cableado se hace medido e incremental. |

## Consecuencias

- **Positivas:** contratos explícitos y verificables; error "no autorizado" ya separable
  de "fallo"/"no encontrado" (mejor diagnóstico y futura política de reintento);
  observabilidad de métricas lista para encender sin tocar deploys; cero dependencias
  nuevas; +12 pruebas, 386 en verde.
- **Deuda registrada (en `RESILIENCE.md`):**
  - **R-10.1:** el `SenderWorker` reintenta en `CrmError` de red → *at-least-once*
    (posible doble envío si el ACK se perdió). `send_message` del CRM no es idempotente.
  - **R-10.2:** `correlation_id`/`turn_id`/`pharmacy_id` aún no se propagan en los logs
    planos de los adaptadores (solo en la traza Langfuse del turno). Diseño del patrón en
    `RESILIENCE.md` §4; cableado = hardening ampliado (requiere aprobación).
  - **R-10.3:** `CAPTURE_PAYLOADS` vuelca el payload crudo completo a logs; es flag de
    dev (default `false`), documentado como "apagar en producción".
