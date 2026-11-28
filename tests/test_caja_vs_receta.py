"""Foto de UNA caja: el medicamento es el nombre comercial, no sus principios activos.

Caso real (providerId 33, 2026-11-28). Imagen de FLEBOSMIN 450 mg - 50 mg. El OCR leyó

    Flebosmin® 450 mg - 50 mg
    Fracción flavonoica micronizada y purificada
    Diosmina 450 mg - Hesperidina 50 mg
    Tabletas recubiertas

y el agente cotizó FLEBOSMIN *y* DIOSMINA *y* HESPERIDINA como tres medicamentos pedidos,
con un "⚠️ No tengo HESPERIDINA" que el cliente nunca pidió.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import media  # noqa: E402
from app import turn as T  # noqa: E402

CAJA = (
    "Flebosmin® 450 mg - 50 mg\n"
    "Fracción flavonoica micronizada y purificada\n"
    "Diosmina 450 mg - Hesperidina 50 mg\n"
    "Tabletas recubiertas"
)
RECETA = (
    "Rp. Amoxicilina 500 mg capsulas. Tomar 1 capsula cada 8 horas por 7 dias. "
    "Ibuprofeno 400 mg tabletas. 1 tableta cada 8 horas por 3 dias."
)


def _buscar(_t):
    async def _b(term):
        return ["FLEBOSMIN 500 MG X 30 TABLETAS RECUBIERT"] if "flebosmin" in term else []
    return _b


def test_la_caja_declara_su_composicion():
    assert media.es_caja_con_composicion(CAJA)
    assert not media.es_caja_con_composicion(RECETA)


def test_la_marca_es_el_nombre_sin_dosis_ni_registro():
    assert media.marca_de_caja(CAJA) == "flebosmin"


def test_la_caja_da_un_solo_medicamento():
    terminos, motivo = asyncio.run(T._medicamentos_de_ocr(CAJA, _buscar(CAJA)))
    assert terminos == ["flebosmin"], (terminos, motivo)
    assert "composición" in motivo


def test_no_se_confunde_con_varios_envases():
    lineas = [l for l in CAJA.splitlines() if l.strip()]
    assert not T._son_varios_envases(lineas)


def test_la_receta_sigue_troceandose():
    terminos, _ = asyncio.run(T._medicamentos_de_ocr(RECETA, _buscar(RECETA)))
    assert len(terminos) >= 2
    assert any("amoxicilina" in t for t in terminos)
    assert any("ibuprofeno" in t for t in terminos)
