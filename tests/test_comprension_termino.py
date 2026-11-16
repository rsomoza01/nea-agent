"""Comprender ANTES de buscar + inferir el medicamento desde la comprensión.

Dos cambios deterministas (sin modelo):
  A. `ToolRuntime._termino_inferido_si_mejora` + su cableo en `_buscar_medicamento`:
     cuando la capa autorizó e infirió un término más limpio (extrajo el fármaco de
     una frase), la búsqueda usa ESE término, no la frase cruda.
  B. `_termino_es_medicamento_plausible` rechaza las meta-frases de composición
     ('principio activo del daflon'), que nunca son un nombre para buscar.

Ver `docs/TAREA-PENDIENTE-comprension-antes-de-busqueda.md`.
"""
from __future__ import annotations

import asyncio

import pytest

from app.state import Conversation
from app.tools import ToolRuntime, _termino_es_medicamento_plausible


# --------------------------------------------------------------------------
# Dobles de prueba
# --------------------------------------------------------------------------
class _Fake:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, _n):
        async def _noop(*_a, **_k):
            return None
        return _noop


class _CrmRecord:
    """CRM falso que REGISTRA cada término consultado y devuelve `productos`."""
    def __init__(self, productos):
        self.productos = productos
        self.consultas: list[str] = []

    async def get_products(self, provider_id, q="", limit=20):
        self.consultas.append((q or "").lower())
        return {"products": list(self.productos), "provider": {"nombre": "QA"},
                "paymenType": None, "hours": None}


def _runtime(productos):
    crm = _CrmRecord(productos)
    ctx = _Fake(crm=crm, store=_Fake())
    rt = ToolRuntime(ctx, Conversation(id=1, wa_identity="wa"), "cv1", provider_id="demo")
    return rt, crm


# ======================================================================
# Cambio B — meta-frase de composición no es término de búsqueda
# ======================================================================
@pytest.mark.parametrize("meta", [
    "principio activo del daflon",
    "principio activo de panadol",
    "ingrediente activo del losartan",
    "cual es el principio activo",
])
def test_b_meta_composicion_no_es_plausible(meta):
    assert _termino_es_medicamento_plausible(meta) is False


@pytest.mark.parametrize("med", [
    "losartan 50 mg", "panadol", "amoxicilina", "daflon", "acido folico 10 mg",
])
def test_b_medicamentos_reales_siguen_plausibles(med):
    assert _termino_es_medicamento_plausible(med) is True


# ======================================================================
# Cambio A — helper de término inferido (guardas)
# ======================================================================
def _mk():
    rt, _ = _runtime([])
    return rt


def test_a_extrae_el_farmaco_de_la_frase():
    rt = _mk()
    rt.intent_hay_consulta = True
    rt.intent_termino = "daflon"
    assert rt._termino_inferido_si_mejora("principio activo del daflon") == "daflon"


def test_a_no_pierde_la_dosis():
    rt = _mk()
    rt.intent_hay_consulta = True
    rt.intent_termino = "losartan"
    assert rt._termino_inferido_si_mejora("losartan 50 mg") is None


def test_a_ignora_termino_stale_de_otro_farmaco():
    rt = _mk()
    rt.intent_hay_consulta = True
    rt.intent_termino = "ibuprofeno"  # veredicto viejo de otra búsqueda
    assert rt._termino_inferido_si_mejora("omeprazol 20 mg") is None


def test_a_no_aplica_si_la_capa_no_concluyo():
    rt = _mk()
    rt.intent_hay_consulta = None
    rt.intent_termino = "daflon"
    assert rt._termino_inferido_si_mejora("principio activo del daflon") is None


def test_a_no_aplica_si_ya_es_limpio():
    rt = _mk()
    rt.intent_hay_consulta = True
    rt.intent_termino = "daflon"
    assert rt._termino_inferido_si_mejora("daflon") is None


# ======================================================================
# Cambio A — cableo en _buscar_medicamento (busca con el término inferido)
# ======================================================================
def test_a_buscar_usa_el_termino_inferido_no_la_frase():
    rt, crm = _runtime([
        {"producto": "DAFLON 500 MG X 30 COMP", "nombre": "DAFLON 500 MG X 30 COMP",
         "precio": 12.0, "precioBs": 410.0, "productId": "p-daf"},
    ])

    async def _auth_capa(nombre):
        # Simula que la capa autorizó y extrajo 'daflon' de la frase.
        rt.intent_hay_consulta = True
        rt.intent_termino = "daflon"
        rt.intent_fuente = "llm"
        return None

    rt._autorizar_busqueda = _auth_capa
    asyncio.run(rt._buscar_medicamento({"nombre": "principio activo del daflon"}))

    assert crm.consultas, "no consultó el catálogo"
    # Consultó con el término inferido, nunca con la frase cruda completa.
    assert any("daflon" in q for q in crm.consultas)
    assert not any("principio activo del daflon" in q for q in crm.consultas)
