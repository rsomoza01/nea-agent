"""Taxonomía de errores del CRM (Paso 10, punto 7).

Distinguir **no autorizado** (401/403 → CrmUnauthorized) de **no encontrado**
(404 → None, sin excepción) y de **fallo de proveedor** (5xx/red → CrmError).
Se inyecta un `httpx.AsyncClient` con `MockTransport` (sin red, sin deps nuevas).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.crm import CrmClient, CrmError, CrmUnauthorized


def _cliente(handler) -> CrmClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://crm.test")
    return CrmClient(base_url="http://crm.test", api_key="k", client=http)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("status", [401, 403])
def test_401_403_se_tipan_como_unauthorized(status):
    crm = _cliente(lambda req: httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(CrmUnauthorized) as exc:
        _run(crm.get_context("5215512345678"))
    assert exc.value.status_code == status
    # Retrocompatible: sigue siendo un CrmError.
    assert isinstance(exc.value, CrmError)


def test_404_es_no_encontrado_no_excepcion():
    crm = _cliente(lambda req: httpx.Response(404))
    assert _run(crm.get_context("5215512345678")) is None


def test_5xx_es_fallo_de_proveedor_no_unauthorized():
    crm = _cliente(lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(CrmError) as exc:
        _run(crm.get_context("5215512345678"))
    assert not isinstance(exc.value, CrmUnauthorized)


def test_error_de_red_es_crm_error():
    def handler(req):
        raise httpx.ConnectError("sin ruta")

    crm = _cliente(handler)
    with pytest.raises(CrmError) as exc:
        _run(crm.get_context("5215512345678"))
    assert not isinstance(exc.value, CrmUnauthorized)


def test_unauthorized_tambien_en_mutacion():
    """401 en un POST (p. ej. handoff) también se tipa, no se trata como 5xx."""
    crm = _cliente(lambda req: httpx.Response(401))
    with pytest.raises(CrmUnauthorized):
        _run(crm.post_handoff("conv-1", "cliente"))
