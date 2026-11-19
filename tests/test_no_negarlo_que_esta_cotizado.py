"""Nunca decir "no tengo X" cuando X está en la lista que se acaba de cotizar.

Casos reales del provider 04 (Farmacia VIP), 2026-11-19:

18:16  cliente: "Tapabocas"   → el catálogo no tiene esa palabra (0 resultados)
18:20  cliente: "Mascarilla"  → el catálogo devuelve DOS mascarillas
       agente : "⚠️ No tengo MASCARILLA. No te ofrezco otro medicamento parecido para
                 no confundirte: son cosas distintas."

El aviso salía con las dos mascarillas cotizadas en la misma llamada. La causa es que
`_farmaco_distinto` decidía si "lo cotizado es otro fármaco" comparando el término del
cliente con `last_term` —la ÚLTIMA búsqueda—, y esa variable no siempre es la del
producto que está delante: el camino de cero resultados NO la limpia a propósito (el
backstop de refinamiento la necesita), así que un turno que busca dos cosas deja
`last_term` apuntando a la primera.

La comprobación correcta no depende del orden de las búsquedas: si la palabra que pidió
el cliente aparece en los títulos cotizados, hay producto y no se puede negar. Este test
fija eso y también que el guard SIGA avisando cuando el producto realmente no está.
"""

from app.turn import _farmaco_distinto


class _Runtime:
    busqueda_por_principio = False
    last_term = ""
    last_products: list[dict] = []


def _rt(term: str, titulos: list[str]) -> _Runtime:
    r = _Runtime()
    r.last_term = term
    r.last_products = [{"nombre": t} for t in titulos]
    return r


def test_mascarilla_no_se_niega_aunque_last_term_sea_otro():
    """El caso reportado: last_term quedó en la búsqueda anterior ('tapabocas')."""
    r = _rt("tapabocas", ["MASCARILLA INFANTIL NIÑA VALEMEDIC", "MASCARILLA KN95 P/NIÑAS ROSADA"])
    assert _farmaco_distinto("Mascarilla", r) is None


def test_mascarilla_no_se_niega_ni_con_el_termino_correcto():
    r = _rt("mascarilla", ["MASCARILLA KN95 P/NIÑAS ROSADA"])
    assert _farmaco_distinto("Mascarilla", r) is None


def test_el_guard_sigue_avisando_cuando_el_producto_no_esta():
    r = _rt("amlodipina", ["AMLODIPINA 10MG X 10 BLISTER"])
    assert _farmaco_distinto("Mascarilla", r) == "mascarilla"


def test_tambien_avisa_si_lo_cotizado_es_un_equipo_distinto():
    r = _rt("tensiometro", ["TENSIOMETRO MANUAL AZUL"])
    assert _farmaco_distinto("Mascarilla", r) == "mascarilla"


def test_sin_productos_no_hay_nada_que_corregir():
    r = _rt("tapabocas", [])
    assert _farmaco_distinto("Mascarilla", r) is None
