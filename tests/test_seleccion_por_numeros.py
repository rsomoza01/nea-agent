"""El cliente marca opciones con números: "1, 10, 17, 19,21,23,30,33,".

Caso real (WhatsApp, 2026-11-24 20:43). Tras recibir una receta con 36 opciones
enumeradas, el cliente respondió con los números de las que quería y el agente
contestó:

    ⚠️ No tengo 1 10 17 19.21 23.30 33. No te ofrezco otro medicamento parecido…

El parser de selección exigía la palabra «opción», la unidad «cajas» o un verbo
(«quiero 1,4,7»), así que una lista de números pelada no era elección: el texto
caía al extractor de término y el guard de «otro fármaco» lo trataba como el
nombre de un medicamento.

Piezas que fija este archivo:
  1. `_extraer_eleccion_multiple` reconoce la lista de números como selección.
  2. Un mensaje sin una sola letra NO produce término de búsqueda.
  3. `_farmaco_distinto` no niega nada cuando el cliente solo escribió números.
"""
from __future__ import annotations

from app.turn import (
    _extraer_eleccion_multiple,
    _extraer_termino_medicamento,
    _farmaco_distinto,
)


# --------------------------------------------------------------------------
# 1. La lista de números es una selección de opciones
# --------------------------------------------------------------------------
def test_lista_de_numeros_del_caso_real() -> None:
    seleccion = _extraer_eleccion_multiple("1, 10, 17, 19,21,23,30,33,")
    assert seleccion == [
        (1, 1), (1, 10), (1, 17), (1, 19), (1, 21), (1, 23), (1, 30), (1, 33),
    ]


def test_variantes_de_lista_de_numeros() -> None:
    assert _extraer_eleccion_multiple("1 10 17") == [(1, 1), (1, 10), (1, 17)]
    assert _extraer_eleccion_multiple("1, 4 y 8") == [(1, 1), (1, 4), (1, 8)]
    assert _extraer_eleccion_multiple("2; 5; 9") == [(1, 2), (1, 5), (1, 9)]
    # Sin repetidos, mismo orden en que los escribió.
    assert _extraer_eleccion_multiple("3,3,1") == [(1, 3), (1, 1)]


def test_las_formas_de_siempre_siguen_igual() -> None:
    assert _extraer_eleccion_multiple("2 cajas de 1,4") == [(2, 1), (2, 4)]
    assert _extraer_eleccion_multiple("quiero 2 cajas de la opción 3") == [(2, 3)]
    assert _extraer_eleccion_multiple("2") == [(1, 2)]


def test_no_es_seleccion_cuando_hay_texto_de_producto() -> None:
    for texto in ("vitamina c 500 mg", "500 mg", "tienes losartan?", "ninguna", ""):
        assert _extraer_eleccion_multiple(texto) is None, texto


# --------------------------------------------------------------------------
# 2. Sin letras no hay medicamento
# --------------------------------------------------------------------------
def test_lista_de_numeros_no_es_termino_de_busqueda() -> None:
    assert _extraer_termino_medicamento("1, 10, 17, 19,21,23,30,33,") is None


def test_los_terminos_con_letras_se_conservan() -> None:
    assert _extraer_termino_medicamento("vitamina 12") == "vitamina 12"
    assert _extraer_termino_medicamento("losartan 50mg") == "losartan 50mg"


# --------------------------------------------------------------------------
# 3. El guard de «otro fármaco» no niega una selección
# --------------------------------------------------------------------------
class _Runtime:
    busqueda_por_principio = False
    busqueda_por_sinonimo = False
    last_term = "acido folico 10 mg"
    last_products = [{"producto": "ACIDO FOLICO 10 MG X 30 TAB PLUSANDEX"}]


def test_una_seleccion_numerica_no_es_otro_farmaco() -> None:
    # Antes devolvía '1 10 17 19.21 23.30 33' y el turno lo convertía en
    # «⚠️ No tengo 1 10 17 19.21 23.30 33».
    assert _farmaco_distinto("1, 10, 17, 19,21,23,30,33,", _Runtime()) is None


def test_un_farmaco_de_verdad_si_se_detecta() -> None:
    assert _farmaco_distinto("espironolactona 100 mg", _Runtime()) is not None
