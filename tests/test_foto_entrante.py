"""El cliente MANDA una foto: ni se promete enviarla ni se niega que llegó.

Caso real (providerId 33, 2026-11-27):
  19:36  cliente: [imagen]           agente: "¡En un momento le enviaremos la foto del
                                              producto!"   ← promesa imposible
  19:53  cliente: "Disculpa dime"    agente: "No me llegó ninguna imagen por este canal"
                                              ← el OCR había leído "Caféina al 10%"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import guards  # noqa: E402
from app import turn as T  # noqa: E402


def test_el_marcador_de_imagen_prohibe_prometer_fotos():
    # El texto que media.py inyecta cuando el cliente manda una imagen.
    from app import media  # noqa: F401
    import inspect

    fuente = inspect.getsource(media)
    assert "NO ofrezcas ni prometas enviarle fotos" in fuente


def test_la_plantilla_con_ocr_no_niega_la_imagen():
    assert "no me llegó" not in guards.TPL_SIN_MEDIA_CON_OCR.lower()
    assert "imagen que me enviaste" in guards.TPL_SIN_MEDIA_CON_OCR.lower()


def test_la_plantilla_sin_media_sigue_existiendo_para_el_caso_real():
    # Sin OCR y sin imagen: aquí sí es honesto decir que no llegó.
    assert "no me llegó ninguna imagen" in guards.TPL_SIN_MEDIA.lower()


def test_el_marcador_no_dispara_el_guard_de_pedir_foto():
    marcador = (
        "[El lead mandó una imagen — la tienes adjunta, puedes verla."
        ' OCR de la imagen: "Caféina al 10%".]'
    )
    assert not T._pide_foto(marcador)
