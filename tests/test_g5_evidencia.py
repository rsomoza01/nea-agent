"""Paquete 2 — G5: ninguna afirmación de composición, principio activo o
equivalencia genérica llega al cliente sin respaldo del catálogo para ESE
producto — sin bloquear la información comercial respaldada.

Dos niveles: (1) unitario sobre los detectores de `app/guards.py` (redacciones y
falsos positivos, con/sin tildes); (2) punta a punta con LLM guionado y catálogo
de mentira (con y sin campo de composición). Sin modelo real (coste 0).
"""
from __future__ import annotations

import asyncio

import pytest

from app import guards
from tests.harness_agente_p2 import (
    CATALOGO_CON_COMPOSICION,
    correr,
    tc,
    txt,
)

TPL = guards.TPL_SIN_COMPOSICION


def _run(nombre, guion, mensaje, catalogo=None):
    return asyncio.run(correr(nombre, guion, mensaje, catalogo=catalogo))


# ======================================================================
# Nivel 1 — detector (D-B2, D-B3, D-B4)
# ======================================================================

# B7 — todas las redacciones afirmativas de composición/equivalencia se detectan.
B7_REDACCIONES = [
    "Su principio activo es el paracetamol.",
    "Contiene ibuprofeno 400 mg.",
    "Es el generico de Panadol.",
    "Es el genérico del Panadol.",
    "El genérico es Panadol.",
    "Tiene el mismo principio activo que el Panadol.",
    "Es equivalente al Panadol.",
    "Es equivalente a Panadol, mismo efecto.",
    "Panadol equivale al paracetamol.",
    "Tiene el mismo efecto que el Panadol.",
    "Se puede reemplazar por Panadol.",
    "Es el mismo medicamento que el Panadol.",
    "Su composición incluye paracetamol.",
    "La composición del Daflon es diosmina.",
    "Su composicion incluye paracetamol.",   # sin tilde (D-B3)
    "Su ingrediente activo es la diosmina.",
]


@pytest.mark.parametrize("frase", B7_REDACCIONES)
def test_b7_redacciones_afirmativas_se_detectan(frase):
    assert guards.afirma_composicion(frase), f"no detectó: {frase!r}"


# B8 — preguntas, negaciones y limitaciones honestas NO se detectan.
B8_NO_DEBEN_BLOQUEAR = [
    "No tengo informacion sobre su composicion.",
    "No puedo confirmar el principio activo sin datos del catalogo.",
    "¿Cuál es el principio activo que buscas?",
    "Cual es el principio activo que buscas?",
    "¿Buscamos su composicion en el catalogo?",
    "No sé si es equivalente al Panadol; prefiero no afirmarlo.",
    "Te muestro las opciones disponibles y sus precios.",
    "El losartan de 50 mg esta disponible en 2 presentaciones.",
]


@pytest.mark.parametrize("frase", B8_NO_DEBEN_BLOQUEAR)
def test_b8_preguntas_y_negaciones_no_se_detectan(frase):
    assert not guards.afirma_composicion(frase), f"falso positivo: {frase!r}"


# B1 (fail-closed) — sin payload de composición no hay evidencia.
def test_fail_closed_sin_campo_de_composicion():
    productos_catalogo_hoy = [
        {"producto": "DAFLON 500", "nombre": "DAFLON", "productId": "p",
         "precio": 12.0, "precioBs": 410.0, "presentacion": "30 comprimidos", "laboratorio": "SERVIER"},
    ]
    assert not guards.hay_evidencia_composicion(
        productos_catalogo_hoy, "Su principio activo es la diosmina."
    )


# B3/B6 (evidencia por producto) — un payload que expone la composición y la
# respalda SÍ cuenta como evidencia; un principio activo distinto NO.
def test_evidencia_por_producto_respaldada_y_no_respaldada():
    prod = [{"producto": "DAFLON 500", "nombre": "DAFLON", "productId": "p",
             "composicion": "DIOSMINA 450 mg"}]
    assert guards.hay_evidencia_composicion(prod, "Su principio activo es la diosmina.")
    # Afirma otro principio activo que el payload NO respalda.
    assert not guards.hay_evidencia_composicion(prod, "Su principio activo es el paracetamol.")


# ======================================================================
# Nivel 2 — punta a punta (D-B1 + conservación de info comercial)
# ======================================================================

# B1 — composición sin evidencia (ninguna herramienta): no se afirma al cliente.
def test_b1_composicion_sin_evidencia_no_llega():
    final = _run("b1", [txt("Su principio activo es la diosmina.")],
                 "¿qué principio activo tiene el daflon?")
    assert "principio activo es la diosmina" not in final.lower()


# B2 — equivalencia sin evidencia: no se afirma al cliente.
def test_b2_equivalencia_sin_evidencia_no_llega():
    final = _run("b2", [txt("Es equivalente al Panadol, mismo efecto.")],
                 "¿cuál es el genérico del panadol?")
    assert "equivalente al panadol" not in final.lower()


# B3 — payload CON composición que respalda: se permite SOLO lo respaldado.
def test_b3_con_evidencia_se_permite():
    guion = [
        tc(1, "buscar_medicamento", {"nombre": "daflon 500"}),
        txt("El Daflon 500 tiene como principio activo la diosmina."),
    ]
    final = _run("b3", guion, "busco daflon 500", catalogo=CATALOGO_CON_COMPOSICION)
    assert "diosmina" in final.lower(), "se bloqueó una afirmación SÍ respaldada por el catálogo"
    assert TPL not in final, "vetó pese a haber evidencia de composición"


# B4 — resultados irrelevantes (sin campo de composición): sigue bloqueado.
def test_b4_resultados_irrelevantes_bloqueado():
    guion = [
        tc(1, "buscar_medicamento", {"nombre": "daflon 500"}),
        txt("El Daflon tiene el mismo principio activo que el Panadol."),
    ]
    final = _run("b4", guion, "busco daflon 500")  # catálogo sin daflon → irrelevante
    assert "mismo principio activo que el panadol" not in final.lower()
    assert TPL in final


# B5 — evidencia parcial (precio sí, composición no): bloqueado; lista conservada.
def test_b5_evidencia_parcial_bloquea_pero_conserva_precio():
    guion = [
        tc(1, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        txt("El losartan tiene el mismo principio activo que el Panadol."),
    ]
    final = _run("b5", guion, "busco losartan 50 mg")
    assert "mismo principio activo que el panadol" not in final.lower(), "la afirmación sin respaldo llegó"
    assert TPL in final, "no se vetó la composición sin respaldo"
    # La información comercial respaldada se conserva (no se vacía la respuesta).
    assert "LOSARTAN" in final and "$3,50" in final


# B9 — info comercial respaldada: G5 no interfiere; el texto queda intacto.
def test_b9_info_comercial_no_interfiere():
    guion = [
        tc(1, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        txt("Tengo LOSARTAN 50 mg x 30 tabletas a 3.5 USD (120 Bs)."),
    ]
    final = _run("b9", guion, "busco losartan 50 mg")
    assert "Tengo LOSARTAN 50 mg x 30 tabletas a 3.5 USD (120 Bs)." in final
    assert TPL not in final
