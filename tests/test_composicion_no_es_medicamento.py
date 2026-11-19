"""La composición de la caja no es otro medicamento.

Caso real (provider 04 = Farmacia VIP, 2026-11-18). El cliente mandó la foto de una
caja de MIOVIT (compositum con lidocaína, vitaminas B1+B6+B12, solución inyectable,
3 ampollas). El agente respondió:

    MIOVIT
    💊 1. MIOVIT KIT X 3 AMP      $5,00
    💊 2. MIOVIT JBE S/AZUCAR      $12,00

    VITAMINA
    ⚠️ No tengo B12. De VITAMINAS B1 B6 hay esto:
    💊 3. VITAMINA C CHICLE X 10 TM LA SANTE
    💊 4. VITAMINA C 500MG TUTTI FRUTTI…
    💊 5. VITAMINA C 500 MG NARANJA…

El cliente pidió MIOVIT. Nadie pidió vitaminas: el OCR leyó el prospecto de la caja
—«Vitaminas B1+B6+B12»— y el parser lo trató como un segundo medicamento. El agente
buscó «VITAMINAS B1 B6», no lo encontró como tal (no existe como producto) y remató
con tres VITAMINA C que no tienen nada que ver.

Lo que se fija aquí: las líneas de COMPOSICIÓN y de CONTENIDO del envase no generan
búsquedas. Y lo que NO debe romperse: un cliente que pide «vitamina c» o «complejo b»
de verdad sigue siendo una consulta válida (esas líneas no empiezan por «vitaminas»
en composición pegado al nombre del producto... ver el último test).
"""

from app.turn import _es_composicion, _parsear_medicamentos_receta

# El OCR real de la caja de MIOVIT.
OCR_MIOVIT = """MIOVIT
COMPOSITUM CON LIDOCAÍNA
Vitaminas B1+B6+B12
SOLUCIÓN INYECTABLE VÍA INTRAMUSCULAR
CONTENIDO NETO
3 AMPOLLAS DE 3 mL
COMPRESAS CON ALCOHOL"""


def test_de_la_caja_de_miovit_queda_solo_miovit():
    medicamentos = _parsear_medicamentos_receta(OCR_MIOVIT)
    assert medicamentos == ["miovit"], medicamentos


def test_ninguna_linea_de_composicion_llega_al_catalogo():
    """El bug era buscar 'VITAMINAS B1 B6' como si el cliente lo hubiera pedido."""
    medicamentos = _parsear_medicamentos_receta(OCR_MIOVIT)
    prohibidos = ("vitamina", "b1", "b6", "b12", "lidocaina", "solucion",
                  "ampolla", "contenido", "compresa")
    for med in medicamentos:
        for p in prohibidos:
            assert p not in med, (med, p)


def test_las_lineas_de_composicion_se_reconocen():
    for linea in (
        "COMPOSITUM CON LIDOCAÍNA",
        "Composición",
        "COMPOSICIÓN: cada ampolla contiene tiamina",
        "Cada ampolla contiene 100 mg",
        "Vitaminas B1+B6+B12",
        "VITAMINAS B1 B6",
        "B1+B6+B12",
        "SOLUCIÓN INYECTABLE VÍA INTRAMUSCULAR",
        "VÍA INTRAMUSCULAR",
        "CONTENIDO NETO",
        "CONTENIDO NETO 3 AMPOLLAS DE 3 mL",
        "COMPRESAS CON ALCOHOL",
        "ingredientes",
        "excipientes: agua",
        "con lidocaína",
    ):
        assert _es_composicion(linea), linea


def test_un_medicamento_de_verdad_no_se_confunde_con_composicion():
    for linea in (
        "MIOVIT",
        "MIOVIT JBE S/AZUCAR 180 ML",
        "ATAMEL FORTE 650MG",
        "VITAMINA C CHICLE X 10 TM LA SANTE",
        "COMPLEJO B X 10 AMP",
        "BEDEX SOL TOPICA BUCAL",
        "COMPRIMIDO DE LOSARTAN",   # empieza por el producto, no por la forma
        "SULFATO FERROSO 200MG",
    ):
        assert not _es_composicion(linea), linea


def test_la_composicion_no_rompe_una_receta_de_varios():
    ocr = """MIOVIT
COMPOSITUM CON LIDOCAÍNA
Vitaminas B1+B6+B12
CONTENIDO NETO 3 AMPOLLAS DE 3 mL
ESOZ 40 MG
LEPRIT 25 MG"""
    medicamentos = _parsear_medicamentos_receta(ocr)
    assert len(medicamentos) == 3, medicamentos
    assert medicamentos[0].lower().startswith("miovit")
    assert medicamentos[1].lower().startswith("esoz")
    assert medicamentos[2].lower().startswith("leprit")
