"""«Llamados», «se llama», «nombre» identifican el producto: no son el producto.

Caso real (providerId 33, 2026-11-27 20:26):
    cliente: Necesito unas ovulo
    cliente: Llamados ilana
    cliente: De 600mg
    agente : ⚠️ No disponibles en el catálogo: LLAMADOS ILANA, 600MG
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import turn as T  # noqa: E402


def test_llamados_no_es_parte_del_nombre():
    assert T._extraer_termino_medicamento(T._sin_motivo("Llamados ilana")) == "ilana"
    assert T._medicamentos_por_espacios("Llamados ilana") == ["ilana"]
    assert T._medicamentos_por_espacios("llamados ilana 600mg") == ["ilana 600mg"]


def test_se_llama_tampoco():
    assert T._extraer_termino_medicamento(T._sin_motivo("el medicamento se llama ilana")) == "ilana"


def test_el_nombre_real_sobrevive():
    assert "ilana" in (T._extraer_termino_medicamento(T._sin_motivo("necesito ilana 600 mg")) or "")
    assert "losartan" in (T._extraer_termino_medicamento(T._sin_motivo("busco losartan 50")) or "")


def test_no_se_come_palabras_que_contienen_llama():
    # «llamativo» no debe quedar vacío por el verbo «llama».
    assert "llamativo" in T._sin_motivo("un empaque llamativo").lower()
