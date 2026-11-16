"""Paso 4 — el adaptador expone el `ToolRuntime` REAL como puerto, sin cambiarlo.

Construye un `ToolRuntime` real con un CRM falso (idiom de
`tests/test_comprension_termino.py:36-52`) y verifica que
`ToolRuntimeCatalogAdapter` + `BuscarCatalogoUseCase` producen el contrato tipado
correcto a partir del comportamiento real del handler, incluido el manejo de
`CrmError` que hereda de `ToolRuntime.execute`.

Los términos se eligen para pasar la autorización F15.1 por **evidencia de producto**
(dosis "500 mg"), de modo que la prueba es determinista y NO invoca al modelo
(`app/tools.py:2825-2832`, `_tiene_evidencia_de_producto`).
"""
from __future__ import annotations

import asyncio

import pytest

from app.application.adapters import ToolRuntimeCatalogAdapter
from app.application.catalog_search import BuscarCatalogoUseCase
from app.application.contracts import CatalogSearchRequest, CatalogSearchStatus
from app.crm import CrmError
from app.state import Conversation
from app.tools import ToolRuntime


class _Fake:
    """Doble permisivo: cualquier atributo ausente es un no-op async."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, _n):
        async def _noop(*_a, **_k):
            return None

        return _noop


class _CrmConProductos:
    async def get_products(self, provider_id, q="", limit=20):
        return {
            "products": [
                {
                    "producto": "PARACETAMOL 500 MG X 10 TAB",
                    "nombre": "PARACETAMOL 500 MG X 10 TAB",
                    "precio": 1.5,
                    "precioBs": 200.0,
                    "productId": "p1",
                }
            ],
            "provider": {"nombre": "QA"},
            "paymenType": None,
            "hours": None,
        }


class _CrmQueFalla:
    async def get_products(self, provider_id, q="", limit=20):
        raise CrmError("products devolvió 503")


def _runtime(crm) -> ToolRuntime:
    ctx = _Fake(crm=crm, store=_Fake())
    return ToolRuntime(ctx, Conversation(id=1, wa_identity="wa"), "cv1", provider_id="demo")


def _ejecutar(crm, termino):
    uc = BuscarCatalogoUseCase(ToolRuntimeCatalogAdapter(_runtime(crm)))
    return asyncio.run(uc.ejecutar(CatalogSearchRequest(termino=termino)))


def test_adaptador_exito_sobre_runtime_real():
    r = _ejecutar(_CrmConProductos(), "paracetamol 500 mg")
    assert r.status is CatalogSearchStatus.ENCONTRADO
    # La serialización observable del handler se conserva intacta en `raw`.
    assert r.raw["ok"] is True
    assert "instrucciones" in r.raw
    assert r.raw["products"][0]["nombre"] == "PARACETAMOL 500 MG X 10 TAB"


def test_adaptador_traduce_crm_error_a_proveedor_no_disponible():
    """`ToolRuntime.execute` captura `CrmError` → `crm_error`; el contrato lo tipa."""
    r = _ejecutar(_CrmQueFalla(), "paracetamol 500 mg")
    assert r.status is CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE
    assert r.raw["error"] == "crm_error"


def test_adaptador_termino_vacio_es_no_consulta():
    r = _ejecutar(_CrmConProductos(), "")
    assert r.status is CatalogSearchStatus.NO_CONSULTA
    assert r.raw["error"] == "nombre_vacio"


def test_adaptador_no_duplica_toolruntime():
    """El adaptador delega; no reimplementa el motor ni mantiene estado paralelo."""
    rt = _runtime(_CrmConProductos())
    adapter = ToolRuntimeCatalogAdapter(rt)
    assert adapter._runtime is rt
