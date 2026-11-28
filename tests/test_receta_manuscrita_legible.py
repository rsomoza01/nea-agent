"""Receta manuscrita de LECTURA CLARA: se cotiza, avisando que se leyó de la foto.

Regla pedida (2026-11-27): "quiero que intentes cotizar recetas escritas a mano, pero solo
cuando la lectura sea clara por el modelo OCR".

  a mano + lectura DUDOSA  -> aviso al auxiliar, no se transcribe (como antes)
  a mano + lectura CLARA   -> se transcribe y se cotiza, pero el marcador avisa al modelo
                              que es su lectura de la foto y que pida confirmar
  impresa + clara          -> igual que antes, sin aviso de manuscritura
"""
import asyncio
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import media  # noqa: E402

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()

RECETA_CLARA = (
    "Rp. Amoxicilina 500 mg capsulas. Tomar 1 capsula cada 8 horas por 7 dias. "
    "Ibuprofeno 400 mg tabletas. 1 tableta cada 8 horas por 3 dias."
)


def _msg(caption: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        image_base64=PNG, image_mime="image/jpeg", media_mime="image/jpeg",
        media_id=None, text=caption, caption=caption, media_caption=caption,
        media_filename=None, media_kind=None, type="image",
    )


def _ctx(ocr: str, dudosa: bool, manuscrita: bool) -> SimpleNamespace:
    llm = SimpleNamespace(
        ocr_image=lambda data, mime, prompt=None: _await(ocr),
        lectura_dudosa=lambda data, mime: _await(dudosa),
        es_manuscrita=lambda data, mime: _await(manuscrita),
        vision_model="v", vision_api_key="k", vision_base_url=None,
    )
    return SimpleNamespace(llm=llm, crm=None, settings=None)


async def _await(v):
    return v


def _marcador(ocr: str, dudosa: bool, manuscrita: bool) -> str:
    part = asyncio.run(media._image(_ctx(ocr, dudosa, manuscrita), _msg()))
    return part.text


def test_manuscrita_legible_se_cotiza_con_aviso():
    texto = _marcador(RECETA_CLARA, dudosa=False, manuscrita=True)
    assert "OCR de la imagen" in texto            # se transcribió
    assert "Amoxicilina" in texto
    assert "ESCRITA A MANO" in texto              # avisa al modelo
    assert "RECETA ESCRITA A MANO — NO" not in texto   # NO entra por la puerta que Bloquea
    assert "confirme" in texto.lower()


def test_manuscrita_dudosa_no_se_transcribe():
    texto = _marcador(RECETA_CLARA, dudosa=True, manuscrita=True)
    assert "RECETA ESCRITA A MANO — NO" in texto
    assert "OCR de la imagen" not in texto


def test_dudosa_impresa_no_se_transcribe():
    texto = _marcador(RECETA_CLARA, dudosa=True, manuscrita=False)
    assert "RECETA DE LECTURA DUDOSA" in texto
    assert "OCR de la imagen" not in texto


def test_impresa_clara_sigue_igual():
    texto = _marcador(RECETA_CLARA, dudosa=False, manuscrita=False)
    assert "OCR de la imagen" in texto
    assert "ESCRITA A MANO" not in texto
