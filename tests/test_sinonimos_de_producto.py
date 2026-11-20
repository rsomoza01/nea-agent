"""El cliente dice la palabra de su país y el catálogo guarda otra.

Caso real (provider 04 = Farmacia VIP, 2026-11-19 18:16). El cliente escribió
"Tapabocas" —lo que se dice en Venezuela— y el catálogo las tiene como MASCARILLA.
El agente respondió:

    "No tengo información confirmada sobre ese medicamento en este momento y no quiero
     darte datos incorrectos… ¿Cuál es el medicamento que buscas?"

Con cuatro mascarillas en el catálogo. El cliente no supo que sí había y la venta no se
cerró: el problema no era el catálogo, era el VOCABULARIO.

Esto no es el registro de alias de medicamentos (`app/aliases.py` + `med_aliases`, que
resuelve marca → principio activo con criterio clínico): aquí es el mismo objeto con otro
nombre según el país.
"""

from app.tools import _con_sinonimo_conocido


def test_tapabocas_es_mascarilla_y_se_avisa():
    termino, aviso = _con_sinonimo_conocido("Tapabocas")
    assert termino == "mascarilla"
    assert "MASCARILLA" in aviso
    # El aviso tiene que reconocer que se le entendió, no cambiar de tema en silencio.
    assert "a lo que te refieres" in aviso


def test_las_otras_formas_de_decirlo():
    for palabra in ("tapaboca", "Barbijo", "barbijos", "cubrebocas", "cubreboca", "nasobuco"):
        termino, aviso = _con_sinonimo_conocido(palabra)
        assert termino == "mascarilla", palabra
        assert aviso, palabra


def test_el_plural_no_rompe_la_busqueda():
    assert _con_sinonimo_conocido("mascarillas")[0] == "mascarilla"


def test_se_sustituye_solo_el_token_conocido():
    """'tapabocas quirurgico' no debe perder el calificativo."""
    termino, aviso = _con_sinonimo_conocido("tapabocas quirurgico")
    assert termino == "mascarilla quirurgico"
    assert aviso


def test_lo_que_no_es_sinonimo_no_se_toca():
    for palabra in ("Mascarilla", "Tensiometro", "atamel", "losartan potasico", ""):
        termino, aviso = _con_sinonimo_conocido(palabra)
        assert termino == palabra, palabra
        assert aviso == "", palabra


def test_no_se_confunde_un_sinonimo_dentro_de_otra_palabra():
    """'tapabocas' no debe dispararse dentro de un nombre más largo."""
    termino, aviso = _con_sinonimo_conocido("tapabocasplus")
    assert termino == "tapabocasplus"
    assert aviso == ""


def test_el_guard_de_otro_farmaco_respeta_el_sinonimo():
    """Buscar 'tapabocas' y cotizar 'MASCARILLA' NO es cotizar otra cosa.

    Sin esto, justo después de encontrar el producto por su otro nombre, el guard
    contestaba "⚠️ No tengo TAPABOCAS" (medido en producción el 2026-11-20 tras
    desplegar la tabla de sinónimos: el producto aparecía y el aviso lo negaba).
    """
    from app.turn import _farmaco_distinto

    class _Rt:
        busqueda_por_principio = False
        busqueda_por_sinonimo = True
        last_term = "mascarilla"
        last_products = [{"nombre": "MASCARILLA KN95 P/NIÑAS ROSADA"}]

    assert _farmaco_distinto("Tapabocas", _Rt()) is None

    class _Rt2(_Rt):
        busqueda_por_sinonimo = False

    # Sin la marca de sinónimo el guard sigue opinando (no se desactiva por accidente).
    assert _farmaco_distinto("Tapabocas", _Rt2()) == "tapabocas"
