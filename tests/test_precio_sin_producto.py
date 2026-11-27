"""«Precio» a secas en el primer mensaje: no hay producto, no se cotiza.

El cliente ve el producto en un ESTADO de WhatsApp y escribe solo «cuál es el precio?».
En una sesión nueva el agente no sabe de qué producto habla: o cotiza cualquier cosa o
inventa el precio. La respuesta es determinista: aviso de auxiliar + handoff.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import turn as T  # noqa: E402


def test_reconoce_la_pregunta_de_precio_sin_producto():
    for texto in ("precio", "Precio", "precio?", "cuál es el precio?", "cual es el precio",
                  "cuanto cuesta", "cuánto vale?", "en cuanto sale", "a cuanto el precio",
                  "buenas, en cuanto sale?"):
        assert T._pregunta_precio_sin_producto(texto), texto


def test_no_dispara_si_nombra_el_producto():
    for texto in ("precio del atamel", "cuanto cuesta la insulina", "precio de la losartán",
                  "hola buenas tardes", "quisiera el precio de metformina 850"):
        assert not T._pregunta_precio_sin_producto(texto), texto


def test_solo_dispara_en_el_primer_turno():
    un_user = [{"role": "system", "content": "…"}, {"role": "user", "content": "precio"}]
    con_historial = [
        {"role": "system", "content": "…"},
        {"role": "user", "content": "tienes losartan?"},
        {"role": "assistant", "content": "Sí, $3,50"},
        {"role": "user", "content": "precio"},
    ]
    assert T._es_primer_turno(un_user)
    assert not T._es_primer_turno(con_historial)


def test_el_mensaje_es_el_pedido():
    assert T.RESPUESTA_PRECIO_SIN_PRODUCTO == "En un momento un auxiliar te atiende y te cotiza. ✅"
