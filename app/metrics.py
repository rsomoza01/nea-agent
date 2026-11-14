"""Métricas de dependencia y de negocio: fachada **no-op por defecto**.

Mismo contrato que `observability.py`: **sin proveedor configurado, cada llamada
es inofensiva** y el agente funciona igual (Paso 10, punto 10). No introduce
dependencias nuevas (ADR-0009): el *sink* por defecto es `None` (no-op total) y,
si `METRICS_ENABLED=true`, se instala un sink que emite líneas estructuradas al
logger `nea.metrics`. Un proveedor real (Prometheus/statsd/OTel) se enchufa
después con `set_sink(...)` **sin tocar los call-sites**, previa ADR + medición
de coste/beneficio.

Esta es la **costura** (seam): define el catálogo de métricas (punto 6) y la
taxonomía de errores (punto 7). El cableado en los adaptadores es incremental y
opcional; mientras no haya sink, no cuesta nada.

Uso previsto (cuando se cablee):

    from app import metrics
    async with metrics.measure_async(metrics.DEP_CRM):
        await crm.get_context(...)
    metrics.record(metrics.HANDOFF, reason="cliente")
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger("nea.metrics")

# --- Dependencias instrumentables (etiqueta `dependency`) -------------------
DEP_POSTGRES = "postgres"
DEP_CRM = "crm"
DEP_LLM = "llm"
DEP_MEDIA = "media"
DEP_WORKER = "worker"

# --- Taxonomía de errores (punto 7): distingue fallo de proveedor de "no
# encontrado" y de "no autorizado". Es el vocabulario compartido con RESILIENCE.md
# y con las excepciones tipadas (crm.CrmError/CrmConflict/CrmUnauthorized).
ERR_PROVIDER_FAILURE = "provider_failure"  # 5xx, red, excepción inesperada
ERR_NOT_FOUND = "not_found"                # 404 / recurso ausente (no es fallo)
ERR_NOT_AUTHORIZED = "not_authorized"      # 401 / 403
ERR_CONFLICT = "conflict"                  # 409 (ai_paused, window_closed, slot_taken)
ERR_TIMEOUT = "timeout"                    # expiró el contrato de timeout
ERR_MALFORMED = "malformed"                # respuesta ilegible / contrato roto

# --- Catálogo de métricas (punto 6) -----------------------------------------
DEP_LATENCY_MS = "dep.latency_ms"   # labels: dependency, ok  (latencia por dependencia)
DEP_CALLS = "dep.calls"             # labels: dependency, ok  (error rate = errores/llamadas)
DEP_ERRORS = "dep.errors"           # labels: dependency, kind (taxonomía de arriba)
TURN_NO_REPLY = "turn.no_reply"     # turnos que terminaron sin responder al cliente
SEARCH_AUTHZ = "search.authz"       # labels: decision=authorized|rejected (puerta F15.1)
RESULT_PARTIAL = "result.partial"   # labels: dependency (resultado parcial/degradado)
HANDOFF = "handoff"                 # labels: reason (catálogo canónico del CRM)
ORDER = "order"                     # labels: outcome=intentado|confirmado|...

# Sink activo. `None` = no-op total. Firma: (name, value, labels) -> None.
_sink: Callable[[str, float, dict[str, Any]], None] | None = None


def set_sink(sink: Callable[[str, float, dict[str, Any]], None] | None) -> None:
    """Instala (o quita con `None`) el sink de métricas. Idempotente."""
    global _sink
    _sink = sink


def _logging_sink(name: str, value: float, labels: dict[str, Any]) -> None:
    """Sink de referencia sin dependencias: una línea estructurada a `nea.metrics`."""
    if labels:
        etiquetas = " ".join(f"{k}={v}" for k, v in sorted(labels.items()))
        logger.info("METRIC %s=%s %s", name, value, etiquetas)
    else:
        logger.info("METRIC %s=%s", name, value)


def configure(settings: Any) -> None:
    """Instala el sink de logging si `METRICS_ENABLED`; si no, deja no-op.

    Llamar una vez al arranque (opcional). No falla si `settings` no trae el flag.
    """
    if bool(getattr(settings, "metrics_enabled", False)):
        set_sink(_logging_sink)
        logger.info("métricas habilitadas (sink: logging nea.metrics)")
    else:
        set_sink(None)


def enabled() -> bool:
    """¿Hay un sink instalado? (para evitar trabajo caro de etiquetado)."""
    return _sink is not None


def record(name: str, value: float = 1.0, **labels: Any) -> None:
    """Registra una métrica. No-op si no hay sink. Nunca propaga excepciones."""
    sink = _sink
    if sink is None:
        return
    try:
        sink(name, float(value), labels)
    except Exception as exc:  # las métricas JAMÁS rompen el flujo
        logger.debug("record(%s) falló (%s)", name, exc)


def timing(dependency: str, ms: float, ok: bool) -> None:
    """Atajo: latencia de una dependencia + conteo de llamada (para error rate)."""
    record(DEP_LATENCY_MS, ms, dependency=dependency, ok=ok)
    record(DEP_CALLS, 1.0, dependency=dependency, ok=ok)


def error(dependency: str, kind: str) -> None:
    """Atajo: incrementa el contador de errores con su taxonomía (punto 7)."""
    record(DEP_ERRORS, 1.0, dependency=dependency, kind=kind)


@contextmanager
def measure(dependency: str) -> Iterator[None]:
    """Mide latencia de un bloque SÍNCRONO y marca ok/fallo. No-op sin sink."""
    if _sink is None:
        yield
        return
    inicio = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        timing(dependency, (time.perf_counter() - inicio) * 1000.0, ok)


@asynccontextmanager
async def measure_async(dependency: str):
    """Versión async de `measure` para los adaptadores (CRM/LLM/DB). No-op sin sink."""
    if _sink is None:
        yield
        return
    inicio = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        timing(dependency, (time.perf_counter() - inicio) * 1000.0, ok)
