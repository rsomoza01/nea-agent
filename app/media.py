"""Procesamiento de multimedia entrante (spec 002, US1).

Cada tipo se convierte en contenido para el turno: texto (transcripción,
contenido del PDF, marcadores) y, para imágenes, una parte visual que va al
modelo. Regla dura: cualquier fallo de descarga/transcripción produce un
marcador HONESTO — jamás rompe el turno ni finge haber entendido.

Los marcadores van entre corchetes; el system prompt le explica a Nea cómo
usarlos sin exponer nada técnico al lead.
"""
from __future__ import annotations

import base64
import io
import logging
import re

from dataclasses import dataclass

# Prompt de la SEGUNDA lectura: pide las dos líneas que identifican el medicamento y
# prohíbe explícitamente el laboratorio y las etiquetas de la caja.
PROMPT_OCR_ENFOCADO = (
    "Mira la caja del medicamento. Devuelve SOLO dos cosas, en una línea:\n"
    "1) el NOMBRE del medicamento: el texto más grande de la caja, normalmente dos "
    "palabras (p. ej. \"Fexofenadina Clorhidrato\", \"Amoxicilina Trihidrato\").\n"
    "2) la concentración con su unidad (p. ej. \"120 mg\", \"250 mg/5 ml\").\n"
    "NO incluyas: el laboratorio (CALOX, GENVEN, OFTALMI...), la categoría "
    "(\"Antialérgico\", \"Antibiótico\", \"Analgésico\"), la vía (\"Vía Oral\"), "
    "\"Medicamento Genérico\", ni la presentación (\"10 Tabletas Recubiertas\").\n"
    "Si no distingues el nombre del medicamento, responde NO_APLICA."
)

_RE_DOSIS_EN_TEXTO = re.compile(
    r"\d+\s*(?:mg|ml|mcg|g|gr|ui)\b|\d+\s*mg\s*/\s*\d+\s*ml", re.IGNORECASE
)


def _lectura_incompleta(texto: str) -> bool:
    """¿La lectura se dejó el nombre del medicamento?

    Incompleta = no trae dosis (mg/ml) y no deja más de una palabra que pueda ser fármaco.
    'nombre del medicamento calox' → incompleta (0 dosis, 1 token).
    'fexofenadina clorhidrato 120 mg' → completa.
    'fexofenadina clorhidrato calox' → completa (2 tokens, aunque no traiga dosis).
    """
    if not texto:
        return False
    if _RE_DOSIS_EN_TEXTO.search(texto):
        return False
    return len(tokens_farmaco(texto)) <= 1


def _mejor_lectura(nueva: str, vieja: str) -> bool:
    """¿La segunda lectura es mejor? Más tokens de fármaco, o iguales y con dosis."""
    t_nueva, t_vieja = len(tokens_farmaco(nueva)), len(tokens_farmaco(vieja))
    if t_nueva != t_vieja:
        return t_nueva > t_vieja
    return bool(_RE_DOSIS_EN_TEXTO.search(nueva)) and not _RE_DOSIS_EN_TEXTO.search(vieja)


from app.state import AppContext, InboundMessage
from app.relevancia import parece_ocr_sin_medicamento, tokens_farmaco

logger = logging.getLogger("nea.media")

MAX_MEDIA_BYTES = 16 * 1024 * 1024  # límite de adjuntos de la Cloud API
MAX_IMAGE_BYTES = 6 * 1024 * 1024  # tope razonable para visión
PDF_MAX_PAGES = 10
PDF_MAX_CHARS = 8000

_AUDIO_EXT = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/wav": "wav",
}


@dataclass
class MediaPart:
    """Render de un ítem multimedia para el turno."""

    text: str | None = None
    image_data_uri: str | None = None


# ── RECETA ESCRITA A MANO ─────────────────────────────────────────────────────────
# Una receta manuscrita NO se transcribe: la tasa de acierto es baja y el agente acaba
# cotizando fármacos que nadie recetó. Caso real (provider 32, 2026-10-28): receta
# manuscrita de ISTRILAB ("Amonicilina… Budicort gotas… Nistatina… Vit D3…").
#
# El OCR marca su respuesta con `MANUSCRITA`; ese marcador se convierte en esta nota y
# `turn._receta_manuscrita()` responde con el aviso al auxiliar, sin consultar nada.
MARCA_MANUSCRITA = (
    "[RECETA ESCRITA A MANO — NO la transcribas ni la interpretes: la letra a mano no se "
    "lee con fiabilidad y podrías cotizar un medicamento equivocado. NO busques nada en el "
    "catálogo con lo que se ve en ella.]"
)

MARCA_DUDOSA = (
    "[RECETA DE LECTURA DUDOSA — NO la transcribas ni la interpretes: algún nombre o dosis "
    "puede leerse de otra manera y podrías cotizar un medicamento equivocado. NO busques "
    "nada en el catálogo con lo que se ve en ella.]"
)

# Señales de que el OCR es una RECETA (y no la foto de un producto suelto): solo cuando
# parece receta se paga la llamada de legibilidad.
_RE_SENAL_RECETA = re.compile(
    r"\b(rp|receta|indicaciones|paciente|fecha|dx|gotas|susp|jarabe|s\s*/\s*f|"
    r"cada\s+\d+\s*(?:h|hrs|horas|d[ií]as)|por\s+\d+\s+d[ií]as|tabletas?|c[aá]psulas?)\b",
    re.IGNORECASE,
)


def parece_receta(ocr_text: str) -> bool:
    """¿El texto del OCR parece una receta (varias líneas de medicamentos)?"""
    t = (ocr_text or "").strip()
    if not t:
        return False
    if _RE_SENAL_RECETA.search(t):
        return True
    lineas = [ln.strip(" -–•*·.:") for ln in t.splitlines() if ln.strip()]
    candidatas = [
        ln for ln in lineas
        if len(ln) >= 4 and re.match(r"^[A-Za-zÁÉÍÓÚÑáéíóúñ][\w\s./%+-]{3,}$", ln)
    ]
    # Sin palabras propias de receta, se exige además VARIAS dosis: una caja de producto
    # trae su nombre y su mg, pero no una lista de fármacos con concentraciones distintas.
    dosis = sum(
        1 for ln in lineas if re.search(r"\d+\s*(?:mg|ml|mcg|g|ui)\b", ln, re.IGNORECASE)
    )
    return len(candidatas) >= 3 and dosis >= 2


def _caption_line(msg: InboundMessage, junto_a: str) -> str:
    if msg.media_caption:
        return f' Nota del lead junto a {junto_a}: "{msg.media_caption}".'
    return ""


def _honest_failure(msg: InboundMessage) -> MediaPart:
    nombres = {
        "audio": "una nota de voz",
        "image": "una imagen",
        "document": "un documento",
        "video": "un video",
        "sticker": "un sticker",
    }
    que = nombres.get(msg.type, "contenido")
    return MediaPart(
        text=(
            f"[El lead mandó {que} que NO pudiste abrir/procesar."
            f"{_caption_line(msg, 'ese contenido')} Sé honesta: dile que no lo"
            " pudiste abrir y pídele el contenido en texto o nota de voz.]"
        )
    )


async def _audio(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    # Canal Evolution: el CRM inyecta el audio como base64 (audio_base64).
    # Canal Meta: se descarga por /api/bot/media/{mediaId}.
    if msg.audio_base64:
        try:
            data = base64.b64decode(msg.audio_base64)
        except Exception:
            logger.warning("media audio: base64 inválido — fallback honesto")
            return _honest_failure(msg)
        mime_clean = (msg.audio_mime or msg.media_mime or "audio/ogg").split(";")[0].strip()
    else:
        if not msg.media_id:
            return _honest_failure(msg)
        data, mime = await ctx.crm.get_media(msg.media_id)
        if len(data) > MAX_MEDIA_BYTES:
            return _honest_failure(msg)
        mime_clean = (mime or msg.media_mime or "audio/ogg").split(";")[0].strip()
    if len(data) > MAX_MEDIA_BYTES:
        return _honest_failure(msg)
    ext = _AUDIO_EXT.get(mime_clean, "ogg")
    transcript = await ctx.llm.transcribe(
        data, mime_clean, filename=f"nota-de-voz.{ext}"
    )
    kind = "Nota de voz" if msg.media_voice else "Audio"
    return MediaPart(
        text=(
            f"[{kind} del lead, transcrita]: \"{transcript}\"."
            " Es una CONSULTA del lead: interpreta la transcripción, "
            "extrae el/los medicamento(s) que pide y consúltalos en el "
            "catálogo (buscar_medicamento). No inventes disponibilidad."
        )
    )


_RE_PALABRA_LECTURA = re.compile(r"[a-záéíóúüñ]{5,}")
def _palabras_de_lectura(texto: str) -> set[str]:
    return {
        w
        for w in _RE_PALABRA_LECTURA.findall((texto or "").lower())
        if w not in {"tableta", "tabletas", "capsula", "capsulas", "gotas", "oftalmicas",
                     "suspension", "solucion", "recubiertas", "recubierta", "por", "para"}
    }


def _lecturas_coinciden(a: str, b: str) -> bool:
    """¿Las dos lecturas hablan de los MISMOS medicamentos? (Jaccard de palabras)"""
    pa, pb = _palabras_de_lectura(a), _palabras_de_lectura(b)
    if not pa or not pb:
        return False
    return len(pa & pb) / len(pa | pb) >= 0.5


async def _image(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    caption = _caption_line(msg, "la imagen")
    # Canal Evolution: el puente del CRM ya descargó la imagen y la inyecta
    # como base64 (Evolution no expone mediaId de Meta). Si no viene, se
    # descarga por /api/bot/media/{mediaId} (canal Meta).
    if msg.image_base64:
        try:
            data = base64.b64decode(msg.image_base64)
        except Exception:
            logger.warning("media image: base64 inválido — fallback honesto")
            return _honest_failure(msg)
        mime_clean = (msg.image_mime or msg.media_mime or "image/jpeg").split(";")[0].strip()
    else:
        if not msg.media_id:
            return _honest_failure(msg)
        data, mime = await ctx.crm.get_media(msg.media_id)
        mime_clean = (mime or msg.media_mime or "image/jpeg").split(";")[0].strip()
    if len(data) > MAX_IMAGE_BYTES:
        return MediaPart(
            text=(
                "[El lead mandó una imagen demasiado pesada para verla."
                f"{caption} Sé honesta si hace falta.]"
            )
        )
    uri = f"data:{mime_clean};base64,{base64.b64encode(data).decode()}"
    # OCR con visión: si la imagen es de un medicamento/receta, extraemos su
    # texto (nombre, dosis, presentación) para que el backstop anti-alucinación
    # pueda consultar el catálogo con un término real. Nunca rompe el turno.
    ocr_text = ""
    manuscrita = False
    ocr = getattr(ctx.llm, "ocr_image", None)
    if ocr is not None:
        try:
            ocr_text = (await ocr(data, mime_clean)).strip()
        except Exception:
            logger.warning("media image: OCR no disponible/falló — imagen va sola")
    # PUERTA DE LEGIBILIDAD: si el OCR parece una RECETA se comprueba que se pueda leer
    # con seguridad. Una receta manuscrita no se transcribe: medido con la receta de
    # ISTRILAB del provider 32, el OCR inventó "Nifedipino" y "Pancreatina" que no estaban
    # en el papel. Esta llamada extra solo se paga cuando el texto parece receta.
    dudosa = False
    if ocr_text and parece_receta(ocr_text):
        comprobar = getattr(ctx.llm, "lectura_dudosa", None)
        if comprobar is not None:
            try:
                dudosa = bool(await comprobar(data, mime_clean))
            except Exception:
                dudosa = True  # no se pudo comprobar → no se arriesga
        if dudosa:
            preg = getattr(ctx.llm, "es_manuscrita", None)
            if preg is not None:
                try:
                    manuscrita = bool(await preg(data, mime_clean))
                except Exception:
                    manuscrita = False
            logger.info(
                "media image: receta de LECTURA DUDOSA (manuscrita=%s) — no se transcribe: %r",
                manuscrita, ocr_text[:60],
            )
    # OCR que NO describe un medicamento: el cliente mandó otra cosa (un
    # COMPROBANTE DE PAGO, una foto de un producto no medicinal). El prompt del OCR
    # asume que la imagen ES un medicamento, así que ante un pago el modelo
    # responde con una frase de negación:
    #
    #   "No se puede extraer texto relacionado con medicamentos o recetas de la
    #    imagen proporcionada, ya que no contiene dicha información."
    #
    # Esa frase es PEOR que no tener OCR: pasa el blindaje anti-prompt y el backstop
    # la usa como TÉRMINO DE BÚSQUEDA, y el catálogo difuso devuelve cualquier cosa
    # (medido: FREEGELLS EXTRA FUERTE, JABON ANITA PEARL) ante un cliente que solo
    # mandó su pago. Se trata como OCR vacío: el marcador queda honesto y el agente
    # responde al TEXTO del cliente ("este es el pago"), no a la frase.
    if ocr_text:
        # Traza del OCR: sin esto, un fallo de clasificación obliga a adivinar qué leyó
        # el modelo (las "4 cajas" se cotizaron como una y no había forma de ver el texto).
        logger.info("media image: OCR = %r", ocr_text[:400])
    if ocr_text and parece_ocr_sin_medicamento(ocr_text):
        logger.info(
            "media image: el OCR no describe un medicamento (%r) — lo descarto",
            ocr_text[:80],
        )
        ocr_text = ""
    # SEGUNDA LECTURA ENFOCADA. El OCR a veces devuelve solo el rótulo y la marca
    # ('nombre del medicamento calox'), perdiendo la línea del principio activo — la que
    # el cliente SÍ ve en la caja. Medido en provider 32 (2026-10-27): de cinco pasadas de
    # la misma foto, dos perdieron 'Fexofenadina'. Pedirle "extrae el texto" no lo arregla;
    # pedirle EXACTAMENTE el nombre y la dosis, sí.
    if ocr_text and not dudosa and _lectura_incompleta(ocr_text) and ocr is not None:
        try:
            enfocado = (await ocr(data, mime_clean, PROMPT_OCR_ENFOCADO)).strip()
        except Exception:
            enfocado = ""
        if enfocado and parece_ocr_sin_medicamento(enfocado):
            enfocado = ""
        if enfocado and _mejor_lectura(enfocado, ocr_text):
            logger.info(
                "media image: segunda lectura enfocada mejoró el OCR (%r → %r)",
                ocr_text[:60], enfocado[:60],
            )
            ocr_text = enfocado

    # DOBLE LECTURA DE LAS RECETAS IMPRESAS. Medido en provider 32 (2026-10-30): la MISMA
    # foto de una lista de 5 medicamentos se leyó, en una llamada, como
    #     'ciprofloxacina 500 / diclofenac 50 / quinomax / ocupred / lacrifort'
    # y, en la siguiente, como
    #     'probiotico 1cp / famotidina 40 mg / desobstruir crema / jabon neutro'
    # (el agente cotizó FAMOTIDINA y JABON NEUTRO a un cliente que llevaba una receta de
    # antibiótico y gotas oftálmicas). Si las dos lecturas no coinciden, NO se transcribe:
    # es preferible pedir los nombres a inventar fármacos.
    if ocr_text and not dudosa and parece_receta(ocr_text) and ocr is not None:
        try:
            segunda = (await ocr(data, mime_clean)).strip()
        except Exception:
            segunda = ""
        if segunda and not parece_ocr_sin_medicamento(segunda):
            if not _lecturas_coinciden(ocr_text, segunda):
                logger.warning(
                    "media image: dos lecturas NO coinciden (%r vs %r) — no se transcribe",
                    ocr_text[:60], segunda[:60],
                )
                dudosa = True  # entra por el camino de "no se transcribe"
                manuscrita = False
                ocr_text = ""

    extra = ""
    if dudosa:
        # No se transcribe ni se consulta: el turno responde con el aviso al auxiliar.
        extra = (
            " " + (MARCA_MANUSCRITA if manuscrita else MARCA_DUDOSA)
            + " El cliente espera que un auxiliar humano lea la receta."
        )
        return MediaPart(
            text=(
                "[El lead mandó una imagen — la tienes adjunta, puedes verla."
                f"{caption}{extra}]"
            ),
            image_data_uri=uri,
        )
    if ocr_text:
        extra = (
            f" OCR de la imagen: \"{ocr_text}\"."
            " Si es un medicamento/receta, consúltalo en el catálogo "
            "(buscar_medicamento) y no inventes precios ni disponibilidad."
        )
    else:
        # Sin OCR de medicamento: dejar claro que la imagen NO trae texto de
        # medicamento, para que el LLM no busque en el catálogo y atienda el
        # contexto real (un pago, un comprobante, una consulta administrativa).
        extra = (
            " No se reconoce texto de medicamento en la imagen:"
            " NO busques en el catálogo por lo que se ve en ella."
            " Atiende lo que el cliente dice en su mensaje."
        )
    return MediaPart(
        text=(
            "[El lead mandó una imagen — la tienes adjunta, puedes verla."
            f"{caption}{extra}]"
        ),
        image_data_uri=uri,
    )


def _pdf_text(data: bytes) -> str | None:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    chunks: list[str] = []
    for page in reader.pages[:PDF_MAX_PAGES]:
        chunks.append(page.extract_text() or "")
        if sum(len(c) for c in chunks) > PDF_MAX_CHARS:
            break
    return "\n".join(chunks)


async def _document(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    filename = msg.media_filename or "documento"
    if not msg.media_id:
        return _honest_failure(msg)
    data, mime = await ctx.crm.get_media(msg.media_id)
    if len(data) > MAX_MEDIA_BYTES:
        return _honest_failure(msg)
    mime_clean = (mime or msg.media_mime or "").split(";")[0].strip()
    text: str | None = None
    if mime_clean == "application/pdf" or filename.lower().endswith(".pdf"):
        text = _pdf_text(data)
    elif mime_clean.startswith("text/"):
        text = data.decode("utf-8", errors="replace")
    if not text or not text.strip():
        return MediaPart(
            text=(
                f"[El lead mandó el documento '{filename}' pero no pudiste leer"
                f" su contenido.{_caption_line(msg, 'el documento')} Sé honesta"
                " y pídele lo importante en texto.]"
            )
        )
    return MediaPart(
        text=(
            f"[Documento '{filename}' del lead — contenido extraído]:"
            f"{_caption_line(msg, 'el documento')}\n{text.strip()[:PDF_MAX_CHARS]}"
        )
    )


async def _sticker(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    return MediaPart(
        text=(
            "[El lead mandó un sticker — tómalo como gesto/emoción y sigue la"
            " conversación natural; no expliques nada técnico.]"
        )
    )


async def _location(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    loc = msg.location or {}
    detalles = ", ".join(
        str(loc[k]) for k in ("name", "address") if loc.get(k)
    )
    coords = f"lat {loc.get('latitude')}, long {loc.get('longitude')}"
    prefix = f"{detalles} — " if detalles else ""
    return MediaPart(
        text=(
            f"[El lead compartió su ubicación: {prefix}{coords}. Si te dice de"
            " dónde es o ya lo sabes, guarda la zona en la ficha (geo).]"
        )
    )


async def _contacts(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    nombres = ", ".join(msg.contact_names) or "alguien"
    return MediaPart(
        text=(
            f"[El lead compartió una tarjeta de contacto de: {nombres}."
            " Agradécelo y pregunta qué relación tiene con su negocio si aporta.]"
        )
    )


async def _video(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    return MediaPart(
        text=(
            "[El lead mandó un video — NO puedes ver videos todavía."
            f"{_caption_line(msg, 'el video')} Sé honesta y ofrécele que te lo"
            " cuente en texto o con una nota de voz (esas sí las entiendes).]"
        )
    )


async def _fallback(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    return MediaPart(
        text=(
            f"[El lead mandó contenido que no puedes ver (tipo: {msg.type})."
            " Sé honesta: pídele que te lo mande como texto o nota de voz.]"
        )
    )


_HANDLERS = {
    "audio": _audio,
    "image": _image,
    "document": _document,
    "sticker": _sticker,
    "location": _location,
    "contacts": _contacts,
    "video": _video,
    "unsupported": _fallback,
}


async def describe_item(ctx: AppContext, msg: InboundMessage) -> MediaPart:
    """Convierte un ítem no-texto en contenido del turno. Nunca lanza."""
    # El puente Evolution ingesta la imagen como type="text" con image_base64
    # inyectada (foto de receta sin caption) o el audio como audio_base64.
    # Enrutar al handler correcto aunque type sea "text".
    if msg.image_base64 or msg.media_id:
        handler = _HANDLERS.get(msg.type, _image)
    elif msg.audio_base64:
        handler = _audio
    else:
        handler = _HANDLERS.get(msg.type, _fallback)
    try:
        return await handler(ctx, msg)
    except Exception:
        logger.exception(
            "media %s (id=%s) no se pudo procesar — fallback honesto",
            msg.type,
            msg.media_id,
        )
        return _honest_failure(msg)
