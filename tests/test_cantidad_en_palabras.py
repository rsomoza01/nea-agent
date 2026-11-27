"""La cantidad dicha en palabras («dos cajas») se aplica UNA vez y con el número dicho.

Caso real (providerId 36, 2026-11-27 19:24-19:26):
    cliente: Quiero dos cajas de la opción 1
    agente : (menú de entrega; el «✅ Agregado» no se vio)
    agente : resumen con Cantidad: 1
    cliente: SI
    agente : resumen con Cantidad: 3   ← y volvía a preguntar ¿confirmas?

El parser de opciones exigía dígitos: «dos cajas» se aplicaba como 1 y el modelo, que sí
entiende «dos», volvía a agregar 2 → 1 + 2 = 3.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import turn as T  # noqa: E402


def test_cantidad_en_palabras():
    assert T._extraer_eleccion_multiple("Quiero dos cajas de la opción 1") == [(2, 1)]
    assert T._extraer_eleccion_multiple("quiero dos de la opción 1") == [(2, 1)]
    assert T._extraer_eleccion_multiple("una caja de la opción 2") == [(1, 2)]
    assert T._extraer_eleccion_multiple("tres de la 1") == [(3, 1)]
    # El formato múltiple con «y» solo se resuelve por la vía de lista de números
    # (limitación preexistente del parser, fuera del alcance de este arreglo).
    assert T._extraer_eleccion_multiple("2 cajas de la opción 3") == [(2, 3)]


def test_los_digitos_siguen_igual():
    assert T._extraer_eleccion_multiple("quiero 2 cajas de la opción 1") == [(2, 1)]
    assert T._extraer_eleccion_multiple("2") == [(1, 2)]


def test_no_inventa_seleccion_donde_no_la_hay():
    assert T._extraer_eleccion_multiple("candesartan 16 mg") is None
    assert T._extraer_eleccion_multiple("adios") is None
    assert T._extraer_eleccion_multiple("tienes losartan de 50?") is None


def test_no_toca_palabras_que_contienen_un_numero():
    # «adios» contiene «dos», «unico» contiene «un»: no deben convertirse.
    assert T._numeros_en_palabra("adios") == "adios"
    assert T._numeros_en_palabra("unico") == "unico"
    assert T._numeros_en_palabra("dos cajas") == "2 cajas"


def test_varios_grupos_con_cantidad():
    """«4 cajas de la opción 2 y 2 de la 3»: dos grupos, cada uno con su cantidad.

    Antes se devolvía solo el último grupo: el primero se agregaba con la cantidad
    por defecto (1) o no se agregaba.
    """
    assert T._extraer_eleccion_multiple("4 cajas de la opción 2 y 2 de la 3") == [(4, 2), (2, 3)]
    assert T._extraer_eleccion_multiple("Quiero 4 cajas de la opción 2 y 2 de la 3") == [(4, 2), (2, 3)]
    assert T._extraer_eleccion_multiple("quiero 2 cajas de la opción 1 y 3 de la 2") == [(2, 1), (3, 2)]


def test_la_lista_con_comas_sigue_siendo_una_lista():
    """Con comas es UNA cantidad para varias opciones, no varios grupos."""
    assert T._extraer_eleccion_multiple("2 cajas de 1 y 1 caja de 3,4,5 y 7") == [
        (2, 1), (1, 3), (1, 4), (1, 5), (1, 7)
    ]
