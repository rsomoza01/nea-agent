"""Paso 5 — cableado de la búsqueda de catálogo como caso de uso (UC-01).

Verifica que enrutar `buscar_medicamento` por `BuscarCatalogoUseCase` **preserva el
comportamiento**: la salida de `ToolRuntime.execute("buscar_medicamento", ...)` es
idéntica a la del motor `ToolRuntime._buscar_medicamento(...)` para un corpus de
términos (éxito, typo, forma/presentación, varios términos, sin resultados, vacío).

Además comprueba:
- que el caso de uso se invoca **una sola vez** por búsqueda (no duplica el motor);
- que la puerta única F15.1 (`_autorizar_busqueda`) sigue dentro del motor y veta
  *antes* de tocar el catálogo, también a través del cableado;
- que el resultado tipado expone `fuente`, `campos_disponibles` y `limitaciones`
  sin cambiar la serialización observable (`to_tool_dict()` = dict del motor);
- las derivaciones puras de esos metadatos en `mapear_resultado`.

Todo determinista: los términos llevan dosis/forma ("500 mg", "grageas") para pasar
F15.1 por **evidencia de producto** (`_tiene_evidencia_de_producto`), de modo que NO
se invoca al modelo (`app/tools.py`).
"""
from __future__ import annotations

import asyncio

import pytest

from app.application.adapters import ToolRuntimeEngineCatalogAdapter
from app.application.catalog_search import BuscarCatalogoUseCase
from app.application.contracts import (
    CatalogSearchRequest,
    CatalogSearchStatus,
    FUENTE_CATALOGO,
    mapear_resultado,
)
from app.state import Conversation
from app.tools import ToolRuntime


# --------------------------------------------------------------------------
# Dobles de prueba (idiom de tests/test_comprension_termino.py)
# --------------------------------------------------------------------------
class _Fake:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, _n):
        async def _noop(*_a, **_k):
            return None

        return _noop


class _CrmFake:
    """CRM falso: devuelve un catálogo fijo y CUENTA cada consulta de productos."""

    def __init__(self, productos):
        self.productos = productos
        self.llamadas = 0

    async def get_products(self, provider_id, q="", limit=20):
        self.llamadas += 1
        return {
            "products": [dict(p) for p in self.productos],
            "provider": {"nombre": "QA"},
            "paymenType": None,
            "hours": None,
        }


CATALOGO = [
    {"producto": "PARACETAMOL 500 MG X 10 TAB", "nombre": "PARACETAMOL 500 MG X 10 TAB",
     "precio": 1.5, "precioBs": 200.0, "productId": "p1", "disponible": True},
    {"producto": "OMEPRAZOL 20 MG X 30 CAP", "nombre": "OMEPRAZOL 20 MG X 30 CAP",
     "precio": 3.0, "precioBs": 400.0, "productId": "p2", "disponible": True},
    {"producto": "BUSCAPINA 10 MG X 20 TAB", "nombre": "BUSCAPINA 10 MG X 20 TAB",
     "precio": 2.0, "precioBs": 260.0, "productId": "p3", "disponible": True},
    {"producto": "LOSARTAN 50 MG X 30 TAB", "nombre": "LOSARTAN 50 MG X 30 TAB",
     "precio": 2.5, "precioBs": 330.0, "productId": "p4", "disponible": True},
    {"producto": "AMOXICILINA 500 MG X 21 CAP", "nombre": "AMOXICILINA 500 MG X 21 CAP",
     "precio": 4.0, "precioBs": 520.0, "productId": "p5", "disponible": True},
]

# Corpus determinista: cada término lleva evidencia de producto (dosis/forma) para
# pasar F15.1 sin modelo, salvo el vacío (que no llega a la puerta).
CORPUS = [
    "paracetamol 500 mg",                     # éxito (ENCONTRADO)
    "omeprasol 20 mg",                        # typo → OMEPRAZOL
    "buscapina 10 mg grageas",                # forma/presentación (aviso_forma)
    "losartan 50 mg y amoxicilina 500 mg",    # varios términos
    "zzzzz 50 mg",                            # sin resultados (NO_ENCONTRADO)
    "",                                       # vacío (NO_CONSULTA)
]


def _runtime(productos=CATALOGO):
    crm = _CrmFake(productos)
    ctx = _Fake(crm=crm, store=_Fake())
    rt = ToolRuntime(ctx, Conversation(id=1, wa_identity="wa"), "cv1", provider_id="demo")
    return rt, crm


# estado del runtime que CONSUMEN los backstops del turno tras una búsqueda
_ESTADO_OBSERVABLE = (
    "aviso_forma", "last_options", "last_products", "last_products_term",
    "es_laboratorio", "es_sal", "busqueda_por_principio",
)


def _snapshot(rt):
    return {k: getattr(rt, k, "<sin>") for k in _ESTADO_OBSERVABLE}


# ======================================================================
# Equivalencia motor ↔ execute (antes/después) para el corpus
# ======================================================================
@pytest.mark.parametrize("termino", CORPUS)
def test_execute_equivale_al_motor(termino):
    """`execute("buscar_medicamento")` debe producir EXACTAMENTE lo que producía el
    motor: mismo dict observable y mismo estado del runtime que ven los backstops."""
    rt_motor, _ = _runtime()
    rt_exec, _ = _runtime()

    via_motor = asyncio.run(rt_motor._buscar_medicamento({"nombre": termino}))
    via_execute = asyncio.run(rt_exec.execute("buscar_medicamento", {"nombre": termino}))

    assert via_execute == via_motor
    assert _snapshot(rt_exec) == _snapshot(rt_motor)


# ======================================================================
# El caso de uso se invoca UNA sola vez (no duplica el motor)
# ======================================================================
def test_execute_invoca_el_motor_una_sola_vez():
    rt, _ = _runtime()
    llamadas = {"n": 0}
    original = rt._buscar_medicamento

    async def _contar(args):
        llamadas["n"] += 1
        return await original(args)

    rt._buscar_medicamento = _contar  # type: ignore[assignment]
    asyncio.run(rt.execute("buscar_medicamento", {"nombre": "paracetamol 500 mg"}))
    assert llamadas["n"] == 1


# ======================================================================
# F15.1 — la puerta única sigue dentro del motor y veta ANTES del catálogo
# ======================================================================
def test_f15_1_veto_por_termino_a_traves_del_cableado():
    """Un término que el backstop ya declaró 'no es consulta' se rechaza por la puerta
    F15.1 sin tocar el catálogo, también cuando se entra por `execute` (el caso de uso
    NO autoriza; solo tipa el veredicto del motor)."""
    rt, crm = _runtime()
    # Veredicto previo del backstop (no-LLM) para ESTE término → rama determinista.
    rt.intent_fuente = "backstop"
    rt.intent_termino = "ladrones"
    rt.intent_hay_consulta = False

    out = asyncio.run(rt.execute("buscar_medicamento", {"nombre": "ladrones"}))

    assert out["ok"] is False
    assert out["error"] == "no_autorizado_por_comprension"
    assert crm.llamadas == 0  # F15.1 vetó ANTES de consultar el catálogo

    # El contrato tipa ese veredicto como NO_AUTORIZADO (frontera de autorización),
    # distinto de NO_CONSULTA, y sin `fuente` (no se consultó el catálogo).
    r = mapear_resultado(out, termino="ladrones")
    assert r.status is CatalogSearchStatus.NO_AUTORIZADO
    assert r.fuente is None


def test_termino_vacio_es_no_consulta_sin_tocar_catalogo():
    rt, crm = _runtime()
    out = asyncio.run(rt.execute("buscar_medicamento", {"nombre": ""}))
    assert out["error"] == "nombre_vacio"
    assert crm.llamadas == 0
    assert mapear_resultado(out, termino="").status is CatalogSearchStatus.NO_CONSULTA


# ======================================================================
# Resultado estructurado sobre el runtime real (fuente/campos/limitaciones)
# ======================================================================
def test_resultado_estructurado_encontrado_sobre_runtime_real():
    rt, _ = _runtime()
    uc = BuscarCatalogoUseCase(ToolRuntimeEngineCatalogAdapter(rt))
    r = asyncio.run(uc.ejecutar(CatalogSearchRequest(termino="paracetamol 500 mg")))

    assert r.status is CatalogSearchStatus.ENCONTRADO
    assert r.fuente == FUENTE_CATALOGO
    # Campos de catálogo realmente presentes (la evidencia disponible).
    assert {"nombre", "precio", "precioBs", "productId", "disponible"} <= set(r.campos_disponibles)
    # Todos los productos traen Bs → sin limitación de precio.
    assert "precio_bs_incompleto" not in r.limitaciones
    # La serialización observable NO cambia: es el dict del motor por identidad.
    assert r.to_tool_dict() is r.raw
    assert r.raw["ok"] is True


# ======================================================================
# Derivaciones puras de los metadatos (mapear_resultado) — prompt punto 5/6
# ======================================================================
def test_fuente_solo_cuando_se_consulto_el_catalogo():
    assert mapear_resultado({"ok": True, "products": [{"nombre": "X"}]}, termino="x").fuente == FUENTE_CATALOGO
    assert mapear_resultado({"ok": False, "error": "sin_resultados"}, termino="x").fuente == FUENTE_CATALOGO
    assert mapear_resultado({"ok": False, "error": "crm_error"}, termino="x").fuente == FUENTE_CATALOGO
    # No se consultó el catálogo:
    assert mapear_resultado({"ok": False, "error": "no_autorizado_por_comprension"}, termino="x").fuente is None
    assert mapear_resultado({"ok": False, "error": "solo_saludo"}, termino="x").fuente is None
    assert mapear_resultado({"ok": False, "error": "algo_raro"}, termino="x").fuente is None


def test_limitacion_equivalencia_por_principio_activo():
    """El resultado por principio activo es MITIGACIÓN, no evidencia de equivalencia
    (DOMAIN-MODEL §6): debe marcarse como limitación (prompt punto 6)."""
    r = mapear_resultado(
        {"ok": True, "products": [{"nombre": "METILPREDNISOLONA 40 MG", "precioBs": 10.0}],
         "principio_activo": True},
        termino="depomedrol",
    )
    assert r.por_principio_activo is True
    assert "equivalencia_no_confirmada" in r.limitaciones


def test_limitacion_forma_pedida_no_disponible():
    r = mapear_resultado(
        {"ok": True, "products": [{"nombre": "OVUMET 500MG X 10 OVU", "precioBs": 5.0}]},
        aviso_forma="⚠️ No tengo X en óvulos con ese nombre.",
        termino="metronidazol ovulos",
    )
    assert r.status is CatalogSearchStatus.PARCIAL
    assert "forma_pedida_no_disponible" in r.limitaciones


def test_limitacion_precio_bs_incompleto():
    r = mapear_resultado(
        {"ok": True, "products": [
            {"nombre": "A", "precioBs": 10.0},
            {"nombre": "B"},  # sin Bs
        ]},
        termino="a",
    )
    assert "precio_bs_incompleto" in r.limitaciones


def test_campos_disponibles_es_union_ordenada():
    r = mapear_resultado(
        {"ok": True, "products": [
            {"nombre": "A", "precio": 1},
            {"nombre": "B", "precioBs": 2, "disponible": True},
        ]},
        termino="a",
    )
    assert r.campos_disponibles == ("disponible", "nombre", "precio", "precioBs")


def test_encontrado_sin_limitaciones_cuando_hay_bs_completo():
    r = mapear_resultado(
        {"ok": True, "products": [{"nombre": "A", "precioBs": 10.0}]},
        termino="a",
    )
    assert r.status is CatalogSearchStatus.ENCONTRADO
    assert r.limitaciones == ()
