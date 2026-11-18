"""Paso 4 — contrato tipado de la búsqueda de catálogo (UC-01).

Prueba la traducción del dict del handler a `CatalogSearchResult` (función pura
`mapear_resultado`) y el caso de uso `BuscarCatalogoUseCase` con dobles de puerto,
cubriendo: éxito, no encontrado, parcial, error externo y cancelación.

No requiere modelo, CRM, DB ni red: solo contratos. El idiom de dobles sigue
`tests/test_comprension_termino.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from app.application.catalog_search import BuscarCatalogoUseCase
from app.application.contracts import (
    CatalogSearchRequest,
    CatalogSearchResult,
    CatalogSearchStatus,
    ProveedorNoDisponibleError,
    mapear_resultado,
)

# --------------------------------------------------------------------------
# Dobles de puerto
# --------------------------------------------------------------------------
class _PuertoFijo:
    """Puerto que devuelve un dict fijo y un `aviso_forma` fijo."""

    def __init__(self, raw: dict, aviso_forma: str = "") -> None:
        self._raw = raw
        self.aviso_forma = aviso_forma
        self.llamadas: list[str] = []

    async def buscar(self, termino: str) -> dict:
        self.llamadas.append(termino)
        return dict(self._raw)


class _PuertoQueLanza:
    """Puerto que lanza una excepción al buscar."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.aviso_forma = ""

    async def buscar(self, termino: str) -> dict:
        raise self._exc


# ======================================================================
# mapear_resultado — función pura, un caso por estado
# ======================================================================
def test_mapea_encontrado():
    raw = {"ok": True, "products": [{"nombre": "PANADOL 500 MG"}], "instrucciones": "..."}
    r = mapear_resultado(raw, termino="panadol")
    assert r.status is CatalogSearchStatus.ENCONTRADO
    assert r.productos == [{"nombre": "PANADOL 500 MG"}]
    assert r.por_principio_activo is False
    assert r.aviso_forma is None


def test_mapea_parcial_por_aviso_de_forma():
    raw = {"ok": True, "products": [{"nombre": "OVUMET 500MG X 10OVU"}], "instrucciones": "..."}
    r = mapear_resultado(raw, aviso_forma="⚠️ No tengo X en óvulos con ese nombre.", termino="metronidazol ovulos")
    assert r.status is CatalogSearchStatus.PARCIAL
    assert r.aviso_forma.startswith("⚠️")


def test_mapea_parcial_por_principio_activo():
    raw = {"ok": True, "products": [{"nombre": "METILPREDNISOLONA"}], "principio_activo": True}
    r = mapear_resultado(raw, termino="depomedrol")
    assert r.status is CatalogSearchStatus.PARCIAL
    assert r.por_principio_activo is True


def test_mapea_no_encontrado():
    raw = {"ok": False, "error": "sin_resultados", "detalle": "no encontrado", "busqueda": "xyz"}
    r = mapear_resultado(raw, termino="xyz")
    assert r.status is CatalogSearchStatus.NO_ENCONTRADO
    assert r.detalle == "no encontrado"


def test_mapea_no_autorizado():
    raw = {"ok": False, "error": "no_autorizado_por_comprension", "detalle": "...", "busqueda": "ladrones"}
    r = mapear_resultado(raw, termino="ladrones")
    assert r.status is CatalogSearchStatus.NO_AUTORIZADO


def test_mapea_proveedor_no_disponible():
    raw = {"ok": False, "error": "crm_error", "detalle": "no pude completar la acción"}
    r = mapear_resultado(raw, termino="losartan 50 mg")
    assert r.status is CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE


@pytest.mark.parametrize(
    "error",
    ["nombre_vacio", "solo_sal", "sin_medicamento", "solo_saludo", "no_medicamento"],
)
def test_familia_no_consulta_se_agrupa(error):
    r = mapear_resultado({"ok": False, "error": error, "detalle": "..."}, termino="hola")
    assert r.status is CatalogSearchStatus.NO_CONSULTA


def test_error_desconocido_se_tipa_como_error():
    """Un error no reconocido NO se asume NO_CONSULTA: se tipa como ERROR explícito
    para no enmascarar un fallo real como 'el cliente no pidió un medicamento'."""
    r = mapear_resultado({"ok": False, "error": "algo_raro"}, termino="x")
    assert r.status is CatalogSearchStatus.ERROR
    assert r.status is not CatalogSearchStatus.NO_CONSULTA


def test_familia_no_consulta_no_se_contamina_con_error_desconocido():
    """Los mapeos existentes se conservan pese al nuevo estado ERROR."""
    assert mapear_resultado({"ok": False, "error": "solo_saludo"}, termino="h").status is CatalogSearchStatus.NO_CONSULTA
    assert mapear_resultado({"ok": False, "error": "sin_resultados"}, termino="z").status is CatalogSearchStatus.NO_ENCONTRADO
    assert mapear_resultado({"ok": False, "error": "no_autorizado_por_comprension"}, termino="l").status is CatalogSearchStatus.NO_AUTORIZADO
    assert mapear_resultado({"ok": False, "error": "crm_error"}, termino="c").status is CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE


def test_to_tool_dict_es_identidad():
    """La serialización observable NO cambia: el dict original se devuelve intacto."""
    raw = {"ok": True, "products": [{"nombre": "X"}], "provider": {"nombre": "QA"}, "instrucciones": "abc"}
    r = mapear_resultado(raw, termino="x")
    assert r.to_tool_dict() is raw
    assert r.to_tool_dict() == raw


# ======================================================================
# BuscarCatalogoUseCase — con dobles de puerto
# ======================================================================
def test_use_case_exito():
    puerto = _PuertoFijo({"ok": True, "products": [{"nombre": "PANADOL"}], "instrucciones": "..."})
    uc = BuscarCatalogoUseCase(puerto)
    r = asyncio.run(uc.ejecutar(CatalogSearchRequest(termino="panadol")))
    assert r.status is CatalogSearchStatus.ENCONTRADO
    assert puerto.llamadas == ["panadol"]


def test_use_case_no_encontrado():
    puerto = _PuertoFijo({"ok": False, "error": "sin_resultados", "detalle": "no"})
    r = asyncio.run(BuscarCatalogoUseCase(puerto).ejecutar(CatalogSearchRequest(termino="zzz")))
    assert r.status is CatalogSearchStatus.NO_ENCONTRADO


def test_use_case_parcial_lee_aviso_del_puerto():
    puerto = _PuertoFijo(
        {"ok": True, "products": [{"nombre": "OVUMET"}]},
        aviso_forma="⚠️ No tengo METRONIDAZOL en óvulos con ese nombre.",
    )
    r = asyncio.run(BuscarCatalogoUseCase(puerto).ejecutar(CatalogSearchRequest(termino="metronidazol ovulos")))
    assert r.status is CatalogSearchStatus.PARCIAL
    assert r.aviso_forma.startswith("⚠️")


def test_use_case_error_externo_se_normaliza():
    """Si el puerto señala la caída de infra con la excepción de aplicación, el caso
    de uso la convierte en PROVEEDOR_NO_DISPONIBLE sin filtrar la excepción."""
    puerto = _PuertoQueLanza(ProveedorNoDisponibleError("CRM caído"))
    r = asyncio.run(BuscarCatalogoUseCase(puerto).ejecutar(CatalogSearchRequest(termino="losartan 50 mg")))
    assert r.status is CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE
    assert r.raw["error"] == "crm_error"


def test_use_case_timeout_es_proveedor_no_disponible():
    """Un timeout de una dependencia externa es 'proveedor no disponible', NO una
    cancelación del turno ni, mucho menos, un NO_CONSULTA silencioso."""
    puerto = _PuertoQueLanza(TimeoutError("el CRM no respondió a tiempo"))
    r = asyncio.run(BuscarCatalogoUseCase(puerto).ejecutar(CatalogSearchRequest(termino="losartan 50 mg")))
    assert r.status is CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE
    assert r.status is not CatalogSearchStatus.NO_CONSULTA
    assert r.detalle == "el CRM no respondió a tiempo"


def test_use_case_timeout_de_asyncio_tambien_es_proveedor_no_disponible():
    """`asyncio.TimeoutError` es alias de `TimeoutError` en 3.11: mismo trato."""
    puerto = _PuertoQueLanza(asyncio.TimeoutError())
    r = asyncio.run(BuscarCatalogoUseCase(puerto).ejecutar(CatalogSearchRequest(termino="x")))
    assert r.status is CatalogSearchStatus.PROVEEDOR_NO_DISPONIBLE


def test_use_case_propaga_cancelacion():
    """Una CANCELACIÓN del turno (distinta de un timeout) NO se convierte en resultado
    de negocio: se propaga tal cual (CancelledError es BaseException, no Exception)."""
    puerto = _PuertoQueLanza(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(BuscarCatalogoUseCase(puerto).ejecutar(CatalogSearchRequest(termino="x")))


def test_resultado_es_inmutable():
    r = CatalogSearchResult(status=CatalogSearchStatus.ENCONTRADO, termino="x")
    with pytest.raises(Exception):
        r.status = CatalogSearchStatus.NO_ENCONTRADO  # type: ignore[misc]
