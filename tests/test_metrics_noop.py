"""Fachada de métricas no-op (Paso 10, puntos 6 y 10).

Sin sink configurado, toda la API es inofensiva y no mide nada. Con sink, emite
el catálogo esperado. Un sink que lanza NUNCA rompe el flujo.
"""
from __future__ import annotations

import asyncio

import pytest

from app import metrics


@pytest.fixture(autouse=True)
def _sink_limpio():
    """Aísla el estado global del sink entre tests (no-op por defecto)."""
    metrics.set_sink(None)
    yield
    metrics.set_sink(None)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- no-op ---

def test_sin_sink_es_noop_total():
    assert metrics.enabled() is False
    # Ninguna de estas llamadas hace nada ni lanza.
    metrics.record(metrics.HANDOFF, reason="cliente")
    metrics.timing(metrics.DEP_CRM, 12.3, ok=True)
    metrics.error(metrics.DEP_LLM, metrics.ERR_TIMEOUT)
    with metrics.measure(metrics.DEP_POSTGRES):
        pass

    async def _usa_async():
        async with metrics.measure_async(metrics.DEP_CRM):
            return 1

    assert _run(_usa_async()) == 1


def test_configure_respeta_el_flag():
    class _S:
        metrics_enabled = False

    metrics.configure(_S())
    assert metrics.enabled() is False

    _S.metrics_enabled = True
    metrics.configure(_S())
    assert metrics.enabled() is True


# ---------------------------------------------------------- con sink ---

def test_record_llega_al_sink_con_etiquetas():
    capturado: list[tuple] = []
    metrics.set_sink(lambda n, v, labels: capturado.append((n, v, labels)))

    metrics.record(metrics.HANDOFF, reason="cliente")
    metrics.error(metrics.DEP_CRM, metrics.ERR_NOT_AUTHORIZED)

    assert capturado[0] == (metrics.HANDOFF, 1.0, {"reason": "cliente"})
    assert capturado[1] == (
        metrics.DEP_ERRORS,
        1.0,
        {"dependency": metrics.DEP_CRM, "kind": metrics.ERR_NOT_AUTHORIZED},
    )


def test_measure_async_registra_latencia_y_llamada():
    nombres: list[str] = []
    metrics.set_sink(lambda n, v, labels: nombres.append(n))

    async def _ok():
        async with metrics.measure_async(metrics.DEP_LLM):
            await asyncio.sleep(0)

    _run(_ok())
    assert metrics.DEP_LATENCY_MS in nombres
    assert metrics.DEP_CALLS in nombres


def test_measure_marca_fallo_y_propaga_excepcion():
    etiquetas: list[dict] = []
    metrics.set_sink(lambda n, v, labels: etiquetas.append(labels))

    with pytest.raises(ValueError):
        with metrics.measure(metrics.DEP_POSTGRES):
            raise ValueError("x")

    # La latencia se registró marcada como ok=False pese a la excepción.
    assert any(l.get("ok") is False for l in etiquetas)


def test_sink_que_lanza_no_rompe_el_flujo():
    def _sink_roto(n, v, labels):
        raise RuntimeError("sink caído")

    metrics.set_sink(_sink_roto)
    # No debe propagar: las métricas jamás rompen el flujo.
    metrics.record(metrics.TURN_NO_REPLY)
    metrics.timing(metrics.DEP_CRM, 1.0, ok=True)
