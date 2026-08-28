"""Observabilidad con Langfuse (self-hosted).

Cliente lazy y no-op: si no hay keys configuradas, todas las llamadas son
inofensivas (el agente funciona igual). Esto permite instrumentar el código
sin romper tests ni deploys que aún no tienen Langfuse.

Uso:
    from app.observability import get_langfuse, langfuse_enabled
    lf = get_langfuse(settings)
    if langfuse_enabled(settings):
        ...
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any

logger = logging.getLogger("nea.obs")

_client: Any = None
_client_settings: Any = None

# Traza activa del turno en curso (contextvar: seguro ante concurrencia).
_current_trace: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "nea_langfuse_trace", default=None
)


def langfuse_enabled(settings: Any) -> bool:
    """True si hay credenciales de Langfuse configuradas."""
    return bool(
        getattr(settings, "langfuse_public_key", "")
        and getattr(settings, "langfuse_secret_key", "")
        and getattr(settings, "langfuse_host", "")
    )


def get_langfuse(settings: Any) -> Any:
    """Devuelve el cliente Langfuse (singleton) o None si no está configurado."""
    global _client, _client_settings
    if not langfuse_enabled(settings):
        return None
    if _client is not None and _client_settings is settings:
        return _client
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        _client_settings = settings
        logger.info("Langfuse conectado (%s)", settings.langfuse_host)
        return _client
    except Exception as exc:  # nunca romper el turno por observabilidad
        logger.warning("Langfuse no disponible (%s) — observabilidad off", exc)
        return None


class Trace:
    """Traza de un turno. No-op si Langfuse está desactivado."""

    def __init__(self, client: Any, name: str, **kwargs: Any) -> None:
        self._client = client
        self._span = None
        if client is not None:
            try:
                self._span = client.trace(name=name, **kwargs)
            except Exception as exc:
                logger.debug("trace falló (%s)", exc)

    def generation(self, name: str, **kwargs: Any) -> "Generation":
        if self._span is not None:
            try:
                return Generation(self._span.generation(name=name, **kwargs))
            except Exception as exc:
                logger.debug("generation falló (%s)", exc)
        return Generation(None)

    def update(self, **kwargs: Any) -> None:
        if self._span is not None:
            try:
                self._span.update(**kwargs)
            except Exception as exc:
                logger.debug("trace.update falló (%s)", exc)


class Generation:
    """Generación LLM dentro de una traza. No-op si no hay span."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def end(self, **kwargs: Any) -> None:
        if self._span is not None:
            try:
                self._span.end(**kwargs)
            except Exception as exc:
                logger.debug("generation.end falló (%s)", exc)


def set_current_trace(trace: Any) -> None:
    """Fija la traza del turno en curso (para que complete() la use)."""
    _current_trace.set(trace)


def current_trace() -> Any:
    """Traza activa del turno, o None."""
    return _current_trace.get()
