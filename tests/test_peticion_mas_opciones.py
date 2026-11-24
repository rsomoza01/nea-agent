"""«¿Podrías mostrarme más opciones?» no es un medicamento.

Caso real (WhatsApp, 2026-11-23 09:33):

    cliente: valproico de 500mg
    agente : ACIDO VALPROICO 500 MG · 💊 1. ... VASTRON CLEO  $17,76 | Bs 16.235,31
    cliente: podrias mostrame mas opciones por favor
    agente : «⚠️ No tengo MOSTRAME. No te ofrezco otro medicamento parecido para no
              confundirte: son cosas distintas.»

El troceador tomaba el VERBO («mostrame») como nombre de producto, el guard de «otro
fármaco» lo comparaba con 'valproico 500mg' y negaba un medicamento que nunca se pidió.

Y el turno siguiente, «acido valproico 500mg y 250mg», contestaba «No tengo de 500 mg.
Lo tengo en: 250 mg.» — negando la concentración que acababa de cotizar, porque el
término llevaba DOS dosis y el aviso se calculaba con la primera.

Piezas que fija este archivo:
  1. `_pide_mas_opciones` reconoce la petición (y no se traga una consulta real).
  2. `_extraer_termino_medicamento` no devuelve verbos de mostrar ni cuantificadores de
     variedad, y con DOS dosis devuelve el fármaco sin dosis.
  3. `_respuesta_mas_opciones` re-muestra la lista REAL ya cotizada (con los precios y
     el bloque de carrito) en vez de negar un fármaco.
"""
from __future__ import annotations

from app import turn as turn_mod
from app.turn import (
    _extraer_termino_medicamento,
    _pide_mas_opciones,
    _respuesta_mas_opciones,
)

PRODUCTO = {
    "producto": "ACIDO VALPROICO 500 MG X 30 TAB VASTRON CLEO",
    "nombre": "ACIDO VALPROICO",
    "precio": 17.76,
    "precioBs": 16235.31,
}


# --------------------------------------------------------------------------
# 1. El verbo es una petición, no un fármaco
# --------------------------------------------------------------------------
def test_peticion_de_mas_opciones_se_reconoce() -> None:
    for frase in (
        "podrias mostrame mas opciones por favor",
        "podrias mostrarme más opciones por favor",
        "muestrame mas opciones",
        "dame otras opciones",
        "hay mas opciones?",
        "tienes otras alternativas",
        "¿y otras presentaciones?",
    ):
        assert _pide_mas_opciones(frase), frase


def test_consulta_de_medicamento_no_es_peticion_de_opciones() -> None:
    for frase in (
        "tienes atamel forte?",
        "muestrame el precio del losartan 50",
        "valproico de 500mg",
        "quiero 2 cajas de la opción 3",
    ):
        assert not _pide_mas_opciones(frase), frase


# --------------------------------------------------------------------------
# 2. El término de búsqueda no arrastra la petición
# --------------------------------------------------------------------------
def test_el_verbo_de_mostrar_no_es_termino_de_busqueda() -> None:
    for frase in (
        "podrias mostrame mas opciones por favor",
        "muestrame mas opciones",
        "dame otras opciones",
    ):
        assert _extraer_termino_medicamento(frase) is None, frase


def test_la_consulta_real_conserva_su_termino() -> None:
    assert _extraer_termino_medicamento("tienes atamel forte?") == "atamel forte"
    assert _extraer_termino_medicamento("valproico de 500mg") == "valproico 500mg"


def test_dos_dosis_en_el_mensaje_busca_la_familia_sin_dosis() -> None:
    # Con 'acido valproico 500mg 250mg' la dosis leída es 500 y el aviso dice
    # «No tengo de 500 mg» sobre una lista que acaba de cotizar 500 mg.
    assert _extraer_termino_medicamento("acido valproico 500mg y 250mg") == "acido valproico"
    assert (
        _extraer_termino_medicamento("acido valproico 500 mg y 250 mg") == "acido valproico"
    )


def test_una_sola_dosis_se_conserva() -> None:
    assert _extraer_termino_medicamento("losartan 50mg") == "losartan 50mg"
    assert _extraer_termino_medicamento("metformina 500 mg") == "metformina 500 mg"


# --------------------------------------------------------------------------
# 3. La respuesta es la lista real, no un «No tengo»
# --------------------------------------------------------------------------
def test_una_sola_presentacion_se_dice_honestamente() -> None:
    texto = _respuesta_mas_opciones([PRODUCTO], "valproico 500mg")
    assert texto.startswith("Es la única presentación que tengo en este momento:")
    assert "VASTRON CLEO" in texto
    assert "$17,76" in texto and "Bs 16.235,31" in texto
    assert "Para agregar al carrito" in texto
    assert "No tengo" not in texto


def test_varias_presentaciones_se_relistan_todas() -> None:
    otro = {
        "producto": "ACIDO VALPROICO 250 MG JBE X 120 ML DROTAFARMA",
        "nombre": "ACIDO VALPROICO",
        "precio": 3.96,
        "precioBs": 3618.15,
    }
    texto = _respuesta_mas_opciones([PRODUCTO, otro], "acido valproico")
    assert "todas las que tengo" in texto
    assert "VASTRON CLEO" in texto and "DROTAFARMA" in texto
    # Menor a mayor, como el resto de las listas.
    assert texto.index("DROTAFARMA") < texto.index("VASTRON CLEO")


def test_sin_lista_previa_no_se_inventa_nada() -> None:
    assert _respuesta_mas_opciones([], "valproico 500mg") == ""
