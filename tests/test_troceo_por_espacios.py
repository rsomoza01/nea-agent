"""Troceo de una lista pegada con dobles espacios.

Caso real (2026-11-27, 16:00): «Precio del jabón  íntimo  por  favor» se partió en
"jabon" e "intimo" → el agente buscó dos veces, respondió dos bloques con el MISMO
producto repetido, encabezó con «⚠️ No disponibles en el catálogo: INTIMO» y gastó 8
búsquedas más en permutaciones del typo (nitimo, itnimo, initmo…).

Caso que motivó el troceo (misma fecha, 07:31): «…farma  D2000 K2D3  alodipina 10
vitamina B12 centrum y magnesio» → 4 términos y se consultaban todos.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import turn as T  # noqa: E402


def test_jabon_intimo_es_un_solo_producto():
    assert T._medicamentos_por_espacios("Precio del jabón  íntimo  por  favor") == ["jabon intimo"]


def test_crema_hidratante_para_bebe_es_un_solo_producto():
    assert T._medicamentos_por_espacios("crema  hidratante  para  bebe") == ["crema hidratante"]


def test_lista_de_medicamentos_sigue_partiendo():
    texto = ("Buenos días saludos por favor precio de estos medicamentos farma  D2000 K2D3  "
             "alodipina 10  vitamina B12 centrum y magnesio gracias")
    assert T._medicamentos_por_espacios(texto) == [
        "d2000 k2d3", "alodipina 10", "vitamina b12 centrum", "magnesio",
    ]


def test_dos_productos_con_dosis_no_se_unen():
    assert T._medicamentos_por_espacios("JABON DE BAÑO REXONA 110GR  PALMOLIVE 90GR") == [
        "jabon baño rexona 110gr", "palmolive 90gr",
    ]


def test_un_calificador_suelto_no_es_un_medicamento():
    # "intimo" solo no debe llegar al catálogo como si fuera un fármaco.
    assert "intimo" not in T._medicamentos_por_espacios("jabón  íntimo")


def test_la_deteccion_de_lista_reconoce_el_caso():
    assert T._parece_lista_medicamentos("Precio del jabón  íntimo  por  favor") is False
