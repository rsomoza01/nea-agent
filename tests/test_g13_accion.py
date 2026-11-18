"""Paquete 3 — G13: el agente no afirma una acción que no ocurrió.

Sigue `docs/SPEC-G13-afirmacion-sin-accion.md`. Tres niveles:
  1. Detector léxico sobre textos reales + negaciones + handoff real (G11).
  2. Decisión de veto (detector + señal de estado) con las funciones de producción.
  3. Punta a punta: carrito VACÍO + "Listo, quedó registrado." → el texto final
     deja de afirmar el registro.
Sin modelo real (coste 0).
"""
from __future__ import annotations

import asyncio

import pytest

from app import guards
from app import turn as turn_mod
from tests.harness_agente_p3 import correr, txt

TPL = guards.TPL_G13_ACCION


class _RuntimeStub:
    """Señales de estado del turno que lee G13 (spec §2.2)."""
    def __init__(self, pedido_finalizado=False, carrito_agregado_turno=False):
        self.pedido_finalizado = pedido_finalizado
        self.carrito_agregado_turno = carrito_agregado_turno


def _veta_g13(texto: str, runtime) -> bool:
    """Replica EXACTA del elif de G13 en turn.py, con las funciones reales."""
    fam = guards.familia_accion_afirmada(texto)
    return bool(fam and not turn_mod._g13_accion_ocurrio(runtime, fam))


# ======================================================================
# Nivel 1 — detector (1 positivo, 0 falsos positivos)
# ======================================================================

def test_detector_positivo_real():
    assert guards.familia_accion_afirmada("Listo, quedó registrado.") == "pedido"


NEGACIONES_Y_LEGITIMOS = [
    "No puedo dejarte esas 2 cajas ni cerrar el pedido.",   # negación léxica real
    "No puedo cerrar el pedido con el carrito vacío.",
    "¿Qué medicamento necesitas?",
    "Tengo LOSARTAN 50 mg disponible, ¿te lo agrego?",
]


@pytest.mark.parametrize("frase", NEGACIONES_Y_LEGITIMOS)
def test_detector_no_dispara_en_negaciones_ni_legitimos(frase):
    assert guards.familia_accion_afirmada(frase) is None


# Las 2 frases de pide_humano (handoff REAL) son de G11, NO disparan G13.
PIDE_HUMANO = [
    "Ya avisé al equipo, en breve te contactan.",
    "Ya le pasé tu caso al equipo.",
]


@pytest.mark.parametrize("frase", PIDE_HUMANO)
def test_handoff_real_no_dispara_g13(frase):
    assert guards.familia_accion_afirmada(frase) is None


# ======================================================================
# Nivel 2 — decisión de veto (frase de familia Y señal en False)
# ======================================================================

def test_frase_sin_accion_veta():
    # carrito vacío / pedido no finalizado → la afirmación es falsa → veto.
    assert _veta_g13("Listo, quedó registrado.", _RuntimeStub()) is True


def test_mismo_texto_con_pedido_finalizado_no_veta():
    # La acción SÍ ocurrió → no se veta.
    assert _veta_g13("Listo, quedó registrado.",
                     _RuntimeStub(pedido_finalizado=True)) is False


def test_agregado_con_carrito_agregado_no_veta():
    assert _veta_g13("Ya lo agregué a tu pedido.",
                     _RuntimeStub(carrito_agregado_turno=True)) is False


def test_negacion_carrito_vacio_no_veta():
    assert _veta_g13("No puedo cerrar el pedido con el carrito vacío.",
                     _RuntimeStub()) is False


# ======================================================================
# Nivel 3 — punta a punta (comprador_decidido, carrito vacío)
# ======================================================================

def test_e2e_carrito_vacio_no_afirma_registro():
    final = asyncio.run(correr(
        "comprador_decidido",
        [txt("Listo, quedó registrado. 👋")],
        "Sí, eso es todo, finaliza mi pedido",
    ))
    assert "quedó registrado" not in final.lower(), "afirmó un registro que no ocurrió"
    assert TPL in final, "no aplicó la plantilla G13"
