"""Sin dosis, sin envase y sin sabor, el núcleo es una CATEGORÍA, no un producto.

Caso real (provider 04 = Farmacia VIP, 2026-11-18). El cliente escribió
"Tensiometro" y el agente ofreció UNO solo:

    TENSIOMETRO
    💊 1. TENSIOMETRO DIGITAL PORTATIL VERDE   $33,00

El catálogo tiene dos:

    TENSIOMETRO DIGITAL PORTATIL VERDE   $33,00
    TENSIOMETRO MANUAL AZUL              $36,05

Los dos daban la misma clave de presentación —('tensiometro', sin dosis, sin envase,
sin sabor)— así que el dedupe se quedaba con el más barato. El cliente nunca supo que
existía el manual: un tensiómetro manual y uno digital no son "la misma presentación
de otro laboratorio", son productos distintos.

Lo que se fija aquí:
  · dos equipos distintos se ofrecen los DOS;
  · la misma presentación de dos laboratorios SIGUE colapsando a la más barata (es el
    motivo por el que el dedupe existe);
  · dos sabores siguen siendo dos opciones;
  · el mismo equipo repetido literalmente sigue colapsando.
"""

from app.tools import _clave_de_presentacion, _dedupe_por_presentacion


def _p(nombre: str, precio: float = 10.0) -> dict:
    return {"producto": nombre, "precio": precio}


def test_dos_tensiometros_son_dos_opciones():
    productos = [
        _p("TENSIOMETRO DIGITAL PORTATIL VERDE", 33.0),
        _p("TENSIOMETRO MANUAL AZUL", 36.05),
    ]
    dedupe = _dedupe_por_presentacion(productos)
    nombres = [p["producto"] for p in dedupe]
    assert len(nombres) == 2, nombres
    assert "TENSIOMETRO DIGITAL PORTATIL VERDE" in nombres
    assert "TENSIOMETRO MANUAL AZUL" in nombres


def test_la_clave_de_un_equipo_incluye_sus_rasgos():
    a = _clave_de_presentacion(_p("TENSIOMETRO DIGITAL PORTATIL VERDE"))
    b = _clave_de_presentacion(_p("TENSIOMETRO MANUAL AZUL"))
    assert a != b
    assert "digital" in a[-1] and "manual" in b[-1]


def test_glucometros_de_marcas_distintas_son_dos_opciones():
    productos = [
        _p("GLUCOMETRO ACCU CHEK INSTANT", 25.0),
        _p("GLUCOMETRO ONE TOUCH SELECT", 28.0),
    ]
    assert len(_dedupe_por_presentacion(productos)) == 2


def test_el_mismo_equipo_repetido_sigue_colapsando():
    productos = [
        _p("TENSIOMETRO DIGITAL PORTATIL VERDE", 33.0),
        _p("TENSIOMETRO DIGITAL PORTATIL VERDE", 30.0),
    ]
    dedupe = _dedupe_por_presentacion(productos)
    assert len(dedupe) == 1
    assert dedupe[0]["precio"] == 30.0


def test_misma_presentacion_de_dos_laboratorios_sigue_colapsando():
    productos = [
        _p("HIDROCLOROTIAZIDA 12.5MG X 10 TABLETAS DROTAFARMA", 3.0),
        _p("HIDROCLOROTIAZIDA 12.5 MG X 10 COMP (SPEFAR)", 2.5),
    ]
    dedupe = _dedupe_por_presentacion(productos)
    assert len(dedupe) == 1, [p["producto"] for p in dedupe]
    assert dedupe[0]["precio"] == 2.5


def test_los_sabores_siguen_siendo_opciones_distintas():
    productos = [
        _p("BENZODIAZOL TAB MAST MIEL Y LIMON X 16", 7.0),
        _p("BENZODIAZOL TAB MAST TUTTI FRUTTI X 16", 7.0),
    ]
    assert len(_dedupe_por_presentacion(productos)) == 2


def test_un_medicamento_con_dosis_no_cambia_de_clave():
    """La rama degenerada NO debe tocar a los medicamentos con dosis o envase."""
    clave = _clave_de_presentacion(_p("LOSARTAN POTASICO 50MG X 30 TAB"))
    assert clave[1] == "50 mg"
    assert clave[2] == "30"
    assert len(clave) == 4
