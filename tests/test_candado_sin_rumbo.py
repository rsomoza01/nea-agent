"""El candado de cierre mide AVANCE, no tamaño del hilo.

Caso real que lo motivó (provider 05 = FarmaHogar, 2026-11-17 19:00 UTC):

    cliente: «quiero 1 caja de la opcion 8»
    agente:  «✅ Agregado al carrito: OMEGA 3 X 45 CAP NOW ×1»
             ← el MISMO turno marcó stalled_at
    cliente: «listo» / «dame el resumen» / «hola»  → 24 h de silencio

La condición era `len(mensajes_del_lead) >= 14` sobre la ventana de historial:
eso no mide estancamiento, mide el largo de la conversación. Con el hilo en 14
mensajes del cliente, cualquier turno cerraba — incluso uno que acababa de
meter un producto al carrito.
"""

from app.stall import (
    MAX_MENSAJES_SIN_AVANCE,
    RACHA_VACIA,
    racha_vacia,
    sin_rumbo,
)

RELLENO = ["ok", "ya", "bueno", "?", ".", "jaja", "👍"]


# --------------------------------------------------------------- carrito ---


def test_con_carrito_activo_nunca_cierra():
    """El cliente está armando un pedido: callarse aquí es perder la venta."""
    del_lead = [f"mensaje {i}" for i in range(30)]
    assert sin_rumbo(del_lead, "descubrimiento", carrito_activo=True, sin_avance=30) is False


def test_con_carrito_activo_tampoco_por_racha_vacia():
    """Aunque el cliente solo diga 'ok', si hay carrito no se cierra."""
    assert (
        sin_rumbo(list(RELLENO), "descubrimiento", carrito_activo=True, sin_avance=6)
        is False
    )


# ------------------------------------------------- avance, no tamaño -------


def test_hilo_largo_con_avance_reciente_no_cierra():
    """30 mensajes de hilo pero solo 2 desde el último avance → sigue viva.

    Es el caso que rompía: antes cerraba por tener 30 mensajes.
    """
    del_lead = [f"mensaje {i}" for i in range(30)]
    assert sin_rumbo(del_lead, "descubrimiento", sin_avance=2) is False


def test_cierra_justo_al_llegar_al_umbral_de_sin_avance():
    assert sin_rumbo(["a"] * MAX_MENSAJES_SIN_AVANCE, "descubrimiento",
                     sin_avance=MAX_MENSAJES_SIN_AVANCE - 1) is False
    assert sin_rumbo(["a"] * MAX_MENSAJES_SIN_AVANCE, "descubrimiento",
                     sin_avance=MAX_MENSAJES_SIN_AVANCE) is True


def test_hilo_corto_sin_avance_no_cierra_por_conteo():
    """Tres mensajes que sí aportan algo: no hay nada que cerrar."""
    assert sin_rumbo(["atamel 500", "de 650", "y el precio"], "descubrimiento",
                     sin_avance=3) is False


# ------------------------------------------------------------- racha -------


def test_racha_vacia_sigue_cerrando_aunque_haya_poco_hilo():
    """Tres mensajes de relleno seguidos sí son un hilo muerto."""
    del_lead = ["atamel 500", "de 650"] + RELLENO[:RACHA_VACIA]
    assert racha_vacia(del_lead) >= RACHA_VACIA
    assert sin_rumbo(del_lead, "descubrimiento", sin_avance=1) is True


# --------------------------------------------------------------- fase -----


def test_fase_con_rumbo_no_se_toca():
    """Agendando o ya cerrada: el candado no opina."""
    for fase in ("agendando", "cerrada"):
        assert sin_rumbo(list(RELLENO) * 5, fase, sin_avance=40) is False
        assert sin_rumbo(list(RELLENO) * 5, fase, carrito_activo=False) is False


def test_sin_sello_de_avance_no_cierra_por_conteo():
    """Conversación que nunca selló avance (todas las previas a la migración 019).

    Si no hay sello, el turno pasa sin_avance=0 y el candado NO puede cerrar por
    número de mensajes: si no, la primera respuesta tras el despliegue habría
    vuelto a cerrar todas las charlas existentes de más de 14 mensajes — el mismo
    bug, con otro nombre.
    """
    del_lead = [f"consulta {i}" for i in range(40)]
    assert sin_rumbo(del_lead, "descubrimiento", sin_avance=0, carrito_activo=False) is False


# ------------------------------------------------- compatibilidad ----------


def test_sin_medida_de_avance_mantiene_el_comportamiento_anterior():
    """`sin_avance=None` (llamadores viejos) sigue midiendo el hilo.

    Se conserva para no romper a quien llame sin el dato nuevo; el turno real
    siempre lo pasa.
    """
    assert sin_rumbo(["x"] * (MAX_MENSAJES_SIN_AVANCE - 1), "descubrimiento") is False
    assert sin_rumbo(["x"] * MAX_MENSAJES_SIN_AVANCE, "descubrimiento") is True
