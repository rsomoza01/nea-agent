"""Una sola presentación + cantidad dicha = se agrega, no se pregunta.

Caso real (provider 04 = Farmacia VIP, 2026-11-18). El cliente escribió:

    "quiero 1 caja de atamel forte"

El catálogo tenía UNA sola presentación (ATAMEL FORTE 650MG X 10). El agente se la
mostró como lista para que eligiera, y su siguiente "listo" recibió:

    "Tu pedido todavía está vacío — la caja de ATAMEL FORTE no llegó a agregarse."

Cierto, pero inútil: el cliente ya había dicho qué quería y cuántas. Y en el turno
siguiente, un "si" suelto le preguntó "¿Me confirmas que son 2 cajas…?" — una cantidad
que nunca dijo, sobre un carrito vacío.

Lo que se fija aquí:
  · la cantidad de compra se detecta en frase libre ("quiero 1 caja de X"), no solo al
    principio del mensaje;
  · preguntar el precio NO autoriza agregar nada;
  · elegir de una lista ("1 caja de la opción 2") lo resuelve el backstop del carrito,
    no esto;
  · con carrito vacío, un "si" no inventa cantidades.
"""

from app.turn import (
    MENSAJE_CARRITO_VACIO_AL_CONFIRMAR,
    _cantidad_de_compra,
)


def test_la_cantidad_se_detecta_con_el_verbo_delante():
    assert _cantidad_de_compra("quiero 1 caja de atamel forte") == 1
    assert _cantidad_de_compra("dame 2 cajas de aflamax") == 2
    assert _cantidad_de_compra("necesito 3 frascos de loratadina") == 3
    assert _cantidad_de_compra("agregame 2 cajas de glymar de 4 mg") == 2
    assert _cantidad_de_compra("mandame 1 pomo de crema") == 1


def test_la_cantidad_sola_al_principio_tambien_cuenta():
    assert _cantidad_de_compra("1 caja de atamel forte") == 1
    assert _cantidad_de_compra("2 blisters de losartan") == 2


def test_preguntar_el_precio_no_es_comprar():
    for texto in (
        "cuanto cuesta el atamel forte",
        "cuanto cuestan 3 cajas de atamel",
        "quiero saber el precio de 2 cajas",
        "dame el precio del atamel",
        "cual es el precio de 1 caja de loratadina",
    ):
        assert _cantidad_de_compra(texto) == 0, texto


def test_comprar_y_preguntar_precio_a_la_vez_si_cuenta():
    assert _cantidad_de_compra("dame el precio de 2 cajas y mandame 2") == 2


def test_elegir_de_una_lista_no_es_esto():
    """'opción N' lo resuelve el backstop del carrito: aquí no se toca."""
    assert _cantidad_de_compra("quiero 1 caja de la opcion 2") == 0
    assert _cantidad_de_compra("2 cajas de la opción 3") == 0


def test_el_envase_y_la_dosis_no_son_cantidad_de_compra():
    assert _cantidad_de_compra("de 1 caja de 30 pastillas") == 0
    assert _cantidad_de_compra("tienes atamel de 650 mg x 10") == 0
    assert _cantidad_de_compra("") == 0


def test_rangos_absurdos_se_ignoran():
    assert _cantidad_de_compra("quiero 0 cajas") == 0
    assert _cantidad_de_compra("quiero 150 cajas de atamel") == 0


def test_el_mensaje_de_carrito_vacio_no_promete_nada_raro():
    assert "Todavía no tienes nada en el pedido" in MENSAJE_CARRITO_VACIO_AL_CONFIRMAR
    assert "cuántas cajas" in MENSAJE_CARRITO_VACIO_AL_CONFIRMAR
    # No debe hablar de confirmar ni de cantidades inventadas.
    assert "confirma" not in MENSAJE_CARRITO_VACIO_AL_CONFIRMAR.lower()
    assert "2 cajas" not in MENSAJE_CARRITO_VACIO_AL_CONFIRMAR
