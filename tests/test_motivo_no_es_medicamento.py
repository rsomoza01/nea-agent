"""Cortesía y motivo no son medicamentos.

Casos reales (2026-11-27):
  "Buenas noches. Me gustaría conocer el precio de nifedipina de 10 y 30 MG"
     → el cliente leyó "⚠️ No disponibles en el catálogo: GUSTARIA" y solo vio la de 10.
  "Hola xfavor para saber disponibilidad y precio de Diosmina tabletas de 500mg"
     → "⚠️ No disponibles en el catálogo: XFAVOR DISPONIBILIDAD".

`_VERBOS_MOTIVO` cubría el motivo en VERBO ("tienes", "cuánto cuesta") pero no en
nombre ("disponibilidad") ni las fórmulas de cortesía ("me gustaría", "xfavor").
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import turn as T  # noqa: E402

CASO_1 = "Buenas noches.\nMe gustaría conocer el precio de nifedipina de 10 y 30 MG"
CASO_2 = "Hola xfavor para saber disponibilidad y precio de \nDiosmina tabletas de 500mg"


def _terminos(texto):
    return T._medicamentos_enumerados(texto) + T._medicamentos_por_espacios(texto)


def test_gustaria_no_es_un_medicamento():
    terminos = " ".join(_terminos(CASO_1)).lower()
    assert "gustaria" not in terminos
    assert "conocer" not in terminos
    assert "nifedipina" in terminos


def test_xfavor_disponibilidad_no_son_un_medicamento():
    terminos = " ".join(_terminos(CASO_2)).lower()
    assert "xfavor" not in terminos
    assert "disponibilidad" not in terminos
    assert "diosmina" in terminos


def test_el_termino_queda_limpio():
    assert T._sin_motivo(CASO_1).lower().count("gustaria") == 0
    assert "disponibilidad" not in T._sin_motivo(CASO_2).lower()


def test_las_palabras_del_motivo_no_rompen_un_medicamento_de_verdad():
    # El vocabulario nuevo no debe comerse nombres reales.
    for texto, esperado in (
        ("Me gustaría saber el precio de losartán 50 mg", "losartán"),
        ("xfavor disponibilidad de metformina 850", "metformina"),
    ):
        terminos = " ".join(_terminos(texto)).lower()
        # El término se normaliza sin tildes: se compara la raíz.
        assert esperado.split()[0][:6] in terminos, terminos
