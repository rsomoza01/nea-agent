"""Formas de pago: se contestan con las del tenant, no con un handoff.

Caso real (provider 04 = Farmacia VIP, 2026-11-18). El CRM servía las 8 formas de
pago en `providers/04.paymenType` y el endpoint que usa la herramienta
`info_provider` las devolvía correctamente:

    1. *Tarjeta de Débito*
    2. *Pago Móvil:* 0424-9542336 · J-502966056 · Banesco
    3. *Binance*  4. *Biopago*  5. *Cashea*  6. *Efectivo*
    7. *Tarjeta de Crédito*  8. *Divisa*

Y aun así el agente respondió:

    «Esa info la maneja el equipo directo — ya les paso tu consulta para que te
     digan los métodos disponibles. 🙌»

Un handoff por un dato que estaba en el CRM. Además de perder la venta por una
duda de pago, incumple la regla de no decir «no tengo» cuando sí se tiene. Ahora
hay un backstop determinista antes del LLM: si preguntan cómo pagar y el tenant
tiene las formas de pago cargadas, se citan tal cual.
"""

from app.turn import (
    _pregunta_formas_de_pago,
    _respuesta_formas_de_pago,
)

PAGOS_VIP = (
    "1. *Tarjeta de Débito*\n"
    "2. *Pago Móvil:* 0424-9542336 · J-502966056 · Banesco\n"
    "3. *Binance*\n"
    "4. *Biopago*\n"
    "5. *Cashea*\n"
    "6. *Efectivo*\n"
    "7. *Tarjeta de Crédito*\n"
    "8. *Divisa*"
)


# --------------------------------------------------- ¿pregunta por el pago? ---


def test_reconoce_las_formulas_reales():
    for texto in (
        "que formas de pago tienen?",
        "¿Qué formas de pago tienen?",
        "cuales son los metodos de pago",
        "¿Cuáles son los métodos de pago?",
        "como puedo pagar?",
        "cómo se puede pagar",
        "con que pagan?",
        "aceptan tarjeta?",
        "aceptan pago movil",
        "se puede pagar con zelle",
        "tienen pago movil?",
        "hay la opcion de pagar con cashea",
    ):
        assert _pregunta_formas_de_pago(texto) is True, texto


def test_no_dispara_con_menciones_sueltas():
    """'tarjeta' o 'efectivo' sueltos no son una pregunta de pago."""
    for texto in (
        "tienes atamel 650",
        "quiero 2 cajas de la opcion 3",
        "la tarjeta de regalo que venden",
        "me lo dejas en efectivo? no, mejor anotalo",
        "buenas tardes",
        "",
    ):
        # 'me lo dejas en efectivo' NO es forma de pago del negocio: no hay fórmula
        assert _pregunta_formas_de_pago(texto) is False, texto


# ------------------------------------------------------------- redacción ----


def test_la_respuesta_cita_las_formas_del_dueno_tal_cual():
    texto = _respuesta_formas_de_pago(PAGOS_VIP)
    assert PAGOS_VIP in texto
    assert "formas de pago" in texto.lower()
    # El dueño escribe `*negrita*` estilo WhatsApp y el dato llega intacto.
    assert "*Pago Móvil:*" in texto
    assert "0424-9542336" in texto
    assert "J-502966056" in texto


def test_soporta_el_salto_escapado_del_saas():
    """El panel puede mandar el markdown con `\\n` literal: hay que abrirlo."""
    texto = _respuesta_formas_de_pago("1. *Efectivo*\\n2. *Divisa*")
    assert "1. *Efectivo*\n2. *Divisa*" in texto
