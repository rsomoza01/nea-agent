"""Receta a mano de lectura CLARA: se cotiza completa (todos los fármacos).

Pedido (2026-11-27): "quiero que intentes cotizar recetas escritas a mano, pero solo
cuando la lectura sea clara por el modelo OCR".

Reglas:
  a mano + DUDOSA -> aviso al auxiliar (sin cambios)
  a mano + CLARA   -> se transcribe, se cotiza y se pide confirmar el nombre/dosis
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import turn as T  # noqa: E402

PROSA = (
    "Rp. Amoxicilina 500 mg capsulas. Tomar 1 capsula cada 8 horas por 7 dias. "
    "Ibuprofeno 400 mg tabletas. 1 tableta cada 8 horas por 3 dias."
)
MANO_REAL = (
    "- Alproloxilo: Dale 8 cc c/12 hrs por 7 dias.\n"
    "- Ceftrai: Dale 4 cc c/12 hrs por 7 dias.\n"
    "- Chidetamin: Coloca en la linea."
)
CAJA = (
    "TRIMIC FORTE L\nSULFAMETOXAZOL 800 MG\nTRIMETOPRIMA 160 MG\n"
    "10 TABLETAS RECUBIERTAS"
)


def test_la_pauta_se_corta_del_nombre():
    assert T._nucleo_receta("Rp. Amoxicilina 500 mg capsulas. Tomar 1 capsula cada 8 horas") == "Amoxicilina 500 mg"
    assert T._nucleo_receta("- Alproloxilo: Dale 8 cc c/12 hrs por 7 dias.") == "Alproloxilo"
    assert T._nucleo_receta("Chidetamin: Coloca en la linea.") == "Chidetamin"


def test_receta_en_un_parrafo_se_parte_en_renglones():
    lineas = [l for l in T._separar_receta_en_lineas(PROSA).splitlines() if l.strip()]
    assert len(lineas) >= 2
    assert T._parsear_medicamentos_receta(PROSA) == ["amoxicilina 500 mg", "ibuprofeno 400 mg"]


def test_receta_a_mano_con_pautas_da_todos_los_farmacos():
    assert T._parsear_medicamentos_receta(MANO_REAL) == ["alproloxilo", "ceftrai", "chidetamin"]
    assert [t for t, _ in T._medicamentos_con_dosis(MANO_REAL)] == ["alproloxilo", "ceftrai", "chidetamin"]


def test_la_caja_no_se_confunde_con_receta():
    # La caja no trae pautas posológicas: el discriminador no debe dispararse.
    assert T._pautas_en_ocr(CAJA) == 0
    assert T._pautas_en_ocr(PROSA) >= 2


def test_el_envase_solo_avisa_si_el_cliente_lo_escribio():
    assert not T._menciona_forma_farmaceutica("amoxicilina 500 mg")
    assert T._menciona_forma_farmaceutica("amoxicilina 500 mg tabletas")


def test_la_lista_cotidiana_no_se_rompe():
    assert T._parsear_medicamentos_receta("ESOZ 40 MG\nLEPRIT 25 MG\nBUMETIN RETARD 300 MG") == [
        "esoz 40 mg", "leprit 25 mg", "bumetin retard 300 mg"
    ]
