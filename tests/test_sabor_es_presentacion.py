"""El sabor es parte de la presentación: dos sabores NO son la misma opción.

Caso real (provider 04 = Farmacia VIP, 2026-11-18). El agente ofreció:

    BENZODIAZOL
    💊 1. BENZODIAZOL TAB MAST TUTTI FRUTTI X 16   $7,00
    💊 2. ...

El cliente respondió «1» y el carrito recibió **BENZODIAZOL TAB MAST TUTTI
FRUTTI**, no la opción 1. La causa: `_clave_de_presentacion` reducía el título a
(núcleo, dosis, envase) y el dedupe colapsaba

    BENZODIAZOL TAB MAST MIEL Y LIMON X 16
    BENZODIAZOL TAB MAST TUTTI FRUTTI X 16   ← mismo núcleo, misma dosis, envase 16

quedándose con UNO (el orden del catálogo decide cuál). El cliente perdía la
elección y el índice de la lista dejaba de corresponder con el producto real.

El sabor entra ahora en la clave. Lo que NO cambia: la misma presentación de dos
laboratorios distintos sigue colapsando a la más barata, que es el motivo por el
que el dedupe existe.
"""

from app.tools import _clave_de_presentacion, _dedupe_por_presentacion


def _p(nombre: str, precio: float = 7.0) -> dict:
    return {"producto": nombre, "precio": precio}


def test_dos_sabores_son_dos_opciones():
    productos = [
        _p("BENZODIAZOL TAB MAST MIEL Y LIMON X 16"),
        _p("BENZODIAZOL TAB MAST TUTTI FRUTTI X 16"),
    ]
    dedupe = _dedupe_por_presentacion(productos)
    nombres = [p["producto"] for p in dedupe]
    assert len(nombres) == 2, nombres
    assert "BENZODIAZOL TAB MAST MIEL Y LIMON X 16" in nombres
    assert "BENZODIAZOL TAB MAST TUTTI FRUTTI X 16" in nombres


def test_el_sabor_aparece_en_la_clave():
    a = _clave_de_presentacion(_p("BENZODIAZOL TAB MAST MIEL Y LIMON X 16"))
    b = _clave_de_presentacion(_p("BENZODIAZOL TAB MAST TUTTI FRUTTI X 16"))
    assert a[-1] == "miel y limon"
    assert b[-1] == "tutti frutti"
    assert a != b


def test_sin_sabor_la_clave_queda_sin_sabor():
    clave = _clave_de_presentacion(_p("LOSARTAN POTASICO 50MG X 30 TAB"))
    assert clave[-1] == ""


def test_el_sabor_mas_largo_gana():
    """'MIEL Y LIMON' no debe quedarse en 'miel' cuando el título dice la mezcla."""
    assert _clave_de_presentacion(_p("CARAMELOS MIEL Y LIMON X 12"))[-1] == "miel y limon"


def test_tildes_y_mayusculas_no_importan():
    assert _clave_de_presentacion(_p("JARABE MIEL Y LIMÓN X 120 ML"))[-1] == "miel y limon"
    assert _clave_de_presentacion(_p("GOTAS PIÑA X 30 ML"))[-1] == "pina"


def test_misma_presentacion_de_dos_laboratorios_sigue_colapsando():
    """Sin sabor de por medio, el dedupe mantiene su comportamiento: la más barata."""
    productos = [
        _p("HIDROCLOROTIAZIDA 12.5MG X 10 TABLETAS DROTAFARMA", 3.0),
        _p("HIDROCLOROTIAZIDA 12.5 MG X 10 COMP (SPEFAR)", 2.5),
    ]
    dedupe = _dedupe_por_presentacion(productos)
    assert len(dedupe) == 1
    assert dedupe[0]["precio"] == 2.5
