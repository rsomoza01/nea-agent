"""Una receta manuscrita/dudosa se contesta a la RECETA, nunca al "envío".

Caso real (providerId 33, 2026-11-27 20:37). El cliente manda una receta escrita a mano
(OCR: "Alproloxilo 8 cc c/12 hrs", "Ceftrai 4 cc c/12 hrs", "Chidetamin"). El marcador de
`media` dice "NO prometas enviarle fotos"; ese "enviarle" hacía True
`_es_peticion_de_envio(user_text)` y el cliente recibía:

    "¡Claro que sí! 🛵 Hacemos entregas a domicilio.
     ¿Qué medicamento quieres que te enviemos y a qué dirección?"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import turn as T  # noqa: E402


def _marcador(marca: str) -> str:
    return (
        "[El lead mandó una imagen — la tienes adjunta, puedes verla."
        " NO ofrezcas ni prometas enviarle fotos: por este canal solo puedes enviar texto."
        " Responde con lo que leíste en la imagen."
        f" {marca} El cliente espera que un auxiliar humano lea la receta.]"
    )


DUDOSA = _marcador(
    "[RECETA DE LECTURA DUDOSA — NO la transcribas ni la interpretes: algún nombre o dosis"
    " puede leerse de otra manera y podrías cotizar un medicamento equivocado. NO busques"
    " nada en el catálogo con lo que se ve en ella.]"
)
MANO = _marcador(
    "[RECETA ESCRITA A MANO — NO la transcribas ni la interpretes: la letra a mano no se"
    " lee con fiabilidad y podrías cotizar un medicamento equivocado. NO busques nada en el"
    " catálogo con lo que se ve en ella.]"
)


def test_la_receta_ilegible_se_reconoce():
    assert T._receta_ilegible(DUDOSA) == "dudosa"
    assert T._receta_ilegible(MANO) == "mano"
    assert T._receta_ilegible("busco losartan 50") is None


def test_el_marcador_de_la_imagen_no_parece_peticion_de_envio():
    # La detección de envío debe mirar SOLO lo que dijo el cliente.
    for texto in (DUDOSA, MANO):
        assert not T._es_peticion_de_envio(T._texto_cliente_sin_marcadores(texto) or "")


def test_la_peticion_de_envio_de_verdad_sigue_detectandose():
    assert T._es_peticion_de_envio("quiero que me lo envíen por favor")
    assert T._es_peticion_de_envio("me lo manda a mi casa")
