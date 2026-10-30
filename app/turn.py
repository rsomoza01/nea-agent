"""Orquestación del turno conversacional.

Gate → contexto del CRM → LLM con tools → envío vía CRM → ficha/fase/seguimiento.
Degradación silenciosa: cualquier fallo termina en silencio + log (y handoff
`error` si el LLM se agotó) — jamás texto roto al lead (Constitución IV).
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from app import media
from app.config import canonical_identity
from app.crm import CrmConflict, CrmError
from app.hostility import ALERT as HOSTILITY_ALERT, hostile_streak
from app import guards
from app.llm import LlmExhausted
from app.relevancia import (
    APELATIVOS as _APELATIVOS,
    es_relevante,
    tokens_posible_farmaco,
)
from app.stall import ALERTA as STALL_ALERT, racha_vacia, sin_rumbo
from app.profile import resolve_profile
from app.prompt import build_system_prompt
from app.wtext import a_formato_whatsapp
from app.state import (
    AppContext,
    Conversation,
    InboundMessage,
    CartItem,
    utcnow,
)
from app.tools import (
    _es_letra_de_vitamina,
    ToolRuntime,
    active_tool_schemas,
    _dosis_sin_unidad,
    _aviso_forma,
    _es_nombre_de_laboratorio,
    _es_solo_saludo,
    _extraer_dosis,
    _filtrar_por_forma,
    _forma_pedida,
    _formatear_lista_productos,
    _fmt_ve,
    _limpiar_termino_medicamento,
    _normalizar_tildes,
    _normalizar_unidad,
    _nucleo_ausente,
    _CATEGORIAS_TERAPEUTICAS,
    _VIAS_ADMINISTRACION,
    _sin_frase_de_indicacion,
    _termino_es_medicamento_plausible,
    _termino_sin_forma,
    _titulo_normalizado,
    _tokens_de_titulo,
    _PALABRAS_FUNCIONALES,
)

logger = logging.getLogger("nea.turn")

# Bloque de cierre de las listas de resultados (carrito): indica cómo agregar
# por número de opción, pedir otro medicamento o finalizar con LISTO.
MENSAJE_SUGERIDO_CARRITO = (
    "👉 Para agregar al carrito: quiero X cajas de la opción Z\n"
    "   Ejemplo: quiero 2 cajas de la opción 3\n"
    "🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.\n"
    "✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido."
)

MAX_TOOL_ROUNDS = 5

# Bloque de farmacia inyectado al system prompt cuando hay providerId. Guía al
# LLM a distinguir consultas/recetas de medicamentos de otros mensajes del
# negocio (contratos, página web, chat, comparador, horarios, etc.). Sin esto,
# el chasis genérico de agendamiento no sabe que este negocio es una farmacia y
# el LLM puede responder con una búsqueda de catálogo a un mensaje sobre un
# contrato o un tema administrativo.
_FARMACIA_BLOCK = """ERES EL AGENTE DE UNA FARMACIA. Tu trabajo principal es atender consultas y recetas de medicamentos: buscar disponibilidad y precio en el catálogo, armar el pedido y cerrar la venta.

CLASIFICA LA INTENCIÓN DEL MENSAJE ANTES DE ACTUAR:
- CONSULTA DE MEDICAMENTO (usa buscar_medicamento): el cliente nombra un medicamento concreto o describe un síntoma/condición que requiere un fármaco. Ej: "tienes losartán", "busco daflon 500", "necesito paracetamol", "me duele la cabeza, ¿qué me recomiendas?".
- RECETA (usa el flujo de receta): el cliente manda una foto o lista de 2+ medicamentos.
- OTRO TEMA DEL NEGOCIO (NO uses buscar_medicamento): contratos, la página web, el chat, el comparador, horarios, ubicación, facturación, proveedores, empleo, alianzas, o cualquier asunto administrativo o comercial que NO sea pedir un medicamento. Responde de forma natural y útil, o deriva al humano si no es tu área. NUNCA busques en el catálogo con palabras como "contrato", "página", "chat", "comparador", "web", "horario".
- NOTIFICACIÓN INTERNA DEL SISTEMA (NO uses buscar_medicamento ni el flujo de receta): mensajes con campos etiquetados como "*Fecha:*", "*Nombre:*", "*Farmacia:*", "*Teléfono:*", o avisos de "se ha realizado una reserva / nueva cita / reserva de demo / pedido confirmado". NO son del cliente: son avisos automáticos. NO busques FECHA, NOMBRE, FARMACIA ni TELÉFONO en el catálogo, ni respondas con una lista de productos. Responde con UNA línea breve de acuse (p. ej. "✅ Recibido: reserva de demo para FARMAUNO el 2/10/2026 a las 10:00 AM.") y nada más.

DATOS DEL NEGOCIO (dirección, horario, teléfono):
- Dalos SOLO si el cliente los pide, y UNA sola vez en la conversación. Si te pide el TELÉFONO y no lo tienes en los datos del negocio, dilo en UNA línea ("no manejo el teléfono desde aquí") y ofrécele pasar a un humano. NO contestes con la dirección ni el horario: no es lo que preguntó.
- Si ya dijiste que NO TIENES un medicamento, no vuelvas a pedirle el nombre de la caja ni a ofrecer "otra presentación": ya lo preguntaste. Propón algo DISTINTO o cierra con naturalidad.

REGLAS:
- Si el cliente NO nombra un medicamento concreto, NO llames buscar_medicamento. Responde directamente.
- Un saludo, una pregunta general o un tema administrativo NO es una consulta de medicamento.
- Si el cliente pide hablar con una persona o plantea un tema que no es de tu competencia (contratos, página web, etc.), ofrécele pasarlo a un humano con naturalidad.

CUANDO NO ENCUENTRAS UN MEDICAMENTO (importante — NO seas repetitivo ni redundante):
- Antes de rendirte, intenta recuperar el mensaje: si el nombre tiene un error de tipeo ("lupripiu", "paracetmol", "lozartan"), REINTENTA buscar con la grafía más probable del fármaco real que crees que quiere decir. Si el cliente dio una FORMA (óvulos, crema, jarabe, gotas) pero no el fármaco, ofrécele decirte el nombre exacto que está en la caja o búscalo por presentación.
- Aprovecha el DATO NUEVO del cliente en cada mensaje. Si el primer mensaje no dio resultados y el cliente responde con más detalle ("similar", "vaginales", "genérico", "de marca"), REINTENTA la búsqueda con ESA pista nueva — no lo repitas ni lo ignores.
- JAMÁS repitas la MISMA frase o estructura de una respuesta tuya anterior. Si ya dijiste "No tengo información sobre X", en la siguiente respuesta NO digas de nuevo 'no tengo información' ni 'no encontré alternativas'. En su lugar: di UNA cosa distinta y útil (una grafía corregida, un fármaco parecido real, una pregunta NUEVA y concreta que haga avanzar, p. ej. "¿traes el nombre que está en la caja?" o "¿te sirve alguno de estas presentaciones?").
- Una vez que agotaste reintentos legítimos (grafía + presentación + dato nuevo del cliente), informa honestamente que no lo tienes, con EMPATÍA y una sola pregunta abierta y no repetida. NO pases a un humano automáticamente por un medicamento agotado."""


# El indicador "composing" de Evolution GO dura solo ~25 s (007). Una consulta
# de medicamento pasa por varias rondas LLM + búsqueda en Firebase y lo supera,
# así que los 3 puntitos desaparecen antes de llegar las opciones. Este es el
# intervalo de reenvío del heartbeat para mantenerlos vivos mientras se procesa.
TYPING_HEARTBEAT_SECONDS = 12.0


async def _typing_heartbeat(ctx: AppContext, crm_conv_id: str, stop: asyncio.Event) -> None:
    """Reenvía \"escribiendo…\" cada TYPING_HEARTBEAT_SECONDS hasta que se cancele.

    Best-effort absoluto: un fallo aquí jamás afecta el turno (mismo contrato
    que el typing inicial). Mantiene vivos los 3 puntitos durante las consultas
    de medicamento que exceden la vida del composing individual de Evolution.
    """
    try:
        while not stop.is_set():
            try:
                await ctx.crm.post_typing(crm_conv_id)
            except Exception as exc:
                logger.debug("typing heartbeat de %s falló (%s) — sigo", crm_conv_id, exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        pass


# WhatsApp: límite duro por mensaje (el CRM valida ≤4096 y WhatsApp corta/da
# error por encima). Margen de seguridad para encabezados de partes.
WA_MAX_CHARS = 4000


def _partir_mensaje_largo(texto: str, max_chars: int) -> list[str]:
    """Divide un mensaje >max_chars en partes enviables por WhatsApp.

    Corta por PÁRRAFOS (líneas vacías) para no romper una opción 💊 a la
    mitad: acumula párrafos mientras entren; un párrafo individual más largo
    que max_chars se corta duro por líneas. Nunca devuelve partes vacías.
    """
    texto = (texto or "").strip()
    if len(texto) <= max_chars:
        return [texto] if texto else []
    partes: list[str] = []
    actual = ""
    for parrafo in re.split(r"\n\s*\n", texto):
        parrafo = parrafo.strip()
        if not parrafo:
            continue
        candidato = f"{actual}\n\n{parrafo}" if actual else parrafo
        if len(candidato) <= max_chars:
            actual = candidato
            continue
        if actual:
            partes.append(actual)
        # Párrafo individual demasiado largo: cortar por líneas.
        while len(parrafo) > max_chars:
            corte = parrafo.rfind("\n", 0, max_chars)
            if corte <= 0:
                corte = max_chars
            partes.append(parrafo[:corte].rstrip())
            parrafo = parrafo[corte:].lstrip()
        actual = parrafo
    if actual:
        partes.append(actual)
    return partes


# Cuánto calla el agente tras cerrar por falta de rumbo. Un lead que vuelve al
# día siguiente merece respuesta; el que insiste en el mismo hilo muerto, no.
STALL_COOLDOWN = timedelta(hours=24)
# Mensajes que se traen para contar el hilo del lead (el LLM ve menos).
STALL_LOOKBACK = 40
CONTEXT_ATTEMPTS = 3  # el relay puede tardar un instante en aterrizar en el CRM

# Comando de pruebas: reinicia la memoria de ESA conversación. Disponible SOLO
# para identidades de TESTER_WA_IDS (vacía = comando apagado).
RESET_COMMANDS = frozenset({"/reset", "#reset"})


def _agent_tz(settings: Any) -> ZoneInfo:
    try:
        return ZoneInfo(getattr(settings, "agent_timezone", "") or "America/Mexico_City")
    except Exception:
        logger.warning("AGENT_TIMEZONE inválida %r — uso America/Mexico_City",
                       getattr(settings, "agent_timezone", None))
        return ZoneInfo("America/Mexico_City")


@asynccontextmanager
async def conversation_lock(ctx: AppContext, identity: str) -> AsyncIterator[None]:
    """Serializa los turnos de UNA conversación.

    El coalescer agrupa ráfagas por debounce, pero nada le impide disparar un
    turno nuevo mientras el anterior sigue corriendo: el mensaje que llega
    tarde abre su propio turno con el contexto de ANTES de que el turno vivo
    actuara. Así se reserva una cita sin haber leído el mensaje que la
    corregía, y salen dos respuestas pisándose.

    Con el candado, el turno tardío espera, y al arrancar re-lee el contexto
    del CRM y el historial — que ya incluyen lo que hizo el turno anterior.
    """
    lock = ctx.turn_locks.get(identity)
    if lock is None:
        lock = ctx.turn_locks[identity] = asyncio.Lock()
    # El conteo sube ANTES del await: quien ya tiene el objeto en mano queda
    # contado, así que el candado nunca se recicla debajo de un turno que
    # espera (y el diccionario no crece sin fin con cada lead histórico).
    ctx.turn_lock_users[identity] = ctx.turn_lock_users.get(identity, 0) + 1
    if lock.locked():
        logger.info(
            "turno de %s en vuelo — el mensaje nuevo espera su turno", identity
        )
    try:
        async with lock:
            yield
    finally:
        remaining = ctx.turn_lock_users.get(identity, 1) - 1
        if remaining <= 0:
            ctx.turn_lock_users.pop(identity, None)
            ctx.turn_locks.pop(identity, None)
        else:
            ctx.turn_lock_users[identity] = remaining


async def handle_flush(ctx: AppContext, identity: str, items: list[Any]) -> None:
    """Callback del coalescer — nunca propaga excepciones."""
    try:
        async with conversation_lock(ctx, identity):
            await run_turn(ctx, identity, items)
    except Exception:
        logger.exception("turno de %s reventó — silencio", identity)


async def run_turn(
    ctx: AppContext, identity: str, inbound: list[InboundMessage]
) -> None:
    settings = ctx.settings

    # --- Observabilidad (Langfuse): traza del turno ------------------------
    # No-op si Langfuse no está configurado. La traza agrupa las generaciones
    # LLM de este turno (vía contextvar) y se cierra al final.
    from app.observability import get_langfuse, set_current_trace, Trace

    lf = get_langfuse(settings)
    trace = Trace(
        lf,
        name="turno",
        user_id=identity,
        input={"identity": identity, "inbound": [m.text for m in inbound]},
    )
    set_current_trace(trace)

    # --- Gate 1: allowlist de pruebas (Constitución V) --------------------
    # En modo laboratorio (/api/chat) la identidad es sintética (persona de
    # prueba del CRM): se salta la allowlist para que el Lab evalúe el
    # comportamiento aunque la identidad no sea un lead real.
    allowed = settings.allowed_identities
    if allowed and canonical_identity(identity) not in allowed and ctx.lab_outbox is None:
        logger.info(
            "allowlist: %s fuera de ALLOWED_WA_IDS — relay sí, respuesta no", identity
        )
        return

    conv = await ctx.store.get_or_create_conversation(identity)

    # --- Comando /reset (líneas de prueba) --------------------------------
    # Corre ANTES de los gates de aiEnabled/ventana: un reset también debe
    # sacar la conversación de un handoff activo.
    if canonical_identity(identity) in settings.tester_identities and any(
        (m.text or "").strip().lower() in RESET_COMMANDS for m in inbound
    ):
        await _run_reset(ctx, conv, identity)
        return

    # --- Gate 1.5: conversación ya cerrada por no ir a ningún lado --------
    # El agente ya se despidió amable; seguir contestando es perseguir. Se
    # reabre sola tras el enfriamiento (un lead que vuelve al día siguiente
    # merece respuesta) o cuando el dueño reactiva la IA desde el CRM.
    if conv.stalled_at is not None:
        if utcnow() - conv.stalled_at < STALL_COOLDOWN:
            logger.info(
                "turno %s: conversación cerrada por falta de rumbo — silencio",
                identity,
            )
            return
        logger.info(
            "turno %s: el lead volvió tras el enfriamiento — reabro", identity
        )
        await ctx.store.update_conversation(conv.id, stalled_at=None)
        conv.stalled_at = None

    # --- Gate 2: contexto del CRM (aiEnabled, ventana) --------------------
    # conversationId de la org correcta (multi-tenant): lo inyecta el puente
    # del CRM para que Nea responda en el hilo correcto, no por identidad
    # (ambigua cuando el mismo número existe en varias farmacias).
    conv_id_hint = next(
        (m.conversation_id for m in inbound if m.conversation_id), None
    )
    context = await _fetch_context(ctx, identity, conv_id_hint)
    if context is None:
        logger.warning("turno %s: sin contexto del CRM — silencio", identity)
        return
    conversation_info = context.get("conversation") or {}
    crm_conv_id = conversation_info.get("id")
    if not crm_conv_id:
        logger.warning("turno %s: contexto sin conversationId — silencio", identity)
        return
    if not conversation_info.get("aiEnabled", False):
        # Handoff por medicamento no disponible: el cliente vuelve con OTRA
        # consulta (p. ej. un audio pidiendo otro medicamento). Reactivar la
        # conversación automáticamente para no dejar al cliente en silencio.
        # Otros handoffs (cliente pidió humano, hostilidad, error) NO se
        # reactivan solos: ahí el humano debe decidir.
        if conversation_info.get("handoffReason") == "medicamento_no_disponible":
            logger.info(
                "turno %s: handoff por medicamento_no_disponible — reactivo y continúo",
                identity,
            )
            try:
                await ctx.crm.post_reset(str(crm_conv_id))
            except CrmError as exc:
                logger.warning(
                    "turno %s: no pude reactivar en el CRM (%s) — silencio",
                    identity, exc,
                )
                return
        else:
            logger.info("turno %s: aiEnabled=false (handoff activo) — silencio", identity)
            return
    if not conversation_info.get("windowOpen", False):
        logger.info("turno %s: ventana de 24 h cerrada — silencio", identity)
        return

    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id=str(crm_conv_id),
        last_inbound_at=utcnow(),
        followup_due_at=None,  # el lead habló: se re-agenda al final del turno
    )

    # Señal de vida: leído + "escribiendo…" mientras Nea piensa (007).
    # Best-effort absoluto: un fallo aquí jamás afecta el turno.
    try:
        await ctx.crm.post_typing(str(crm_conv_id))
    except Exception as exc:
        logger.debug("typing de %s falló (%s) — sigo", identity, exc)

    # --- Contenido del turno: texto + multimedia procesada (spec 002) -----
    parts: list[str] = []
    image_uris: list[str] = []
    for m in inbound:
        # El TEXTO entra siempre, incluso cuando trae caption de la foto:
        # "necesito precio de este medicamento" + imagen en el MISMO mensaje.
        if m.text:
            parts.append(m.text)
        # Un mensaje con imagen (base64 inyectada por el puente Evolution o
        # media_id de Meta) se procesa como media aunque type sea "text" y el
        # texto venga vacío (foto de receta sin caption) — o con texto (caption).
        # Caso real (provider 32, 2026-10-29): el caption + la foto en el mismo
        # mensaje dejaban la foto SIN procesar y el agente pedía el nombre que
        # ya estaba en la imagen ("¿me puedes decir el nombre del medicamento?").
        # Igual para audio.
        if m.image_base64 or m.media_id or m.audio_base64:
            part = await media.describe_item(ctx, m)
            if part.text:
                parts.append(part.text)
            if part.image_data_uri:
                image_uris.append(part.image_data_uri)
            continue
        if m.type in ("text", "button", "interactive"):
            continue  # texto sin media: nada que procesar
        part = await media.describe_item(ctx, m)
        if part.text:
            parts.append(part.text)
        if part.image_data_uri:
            image_uris.append(part.image_data_uri)
    if not parts and not image_uris:
        logger.info("turno %s: nada procesable en la ráfaga — silencio", identity)
        return

    # El VOCATIVO inicial ('Robi, ...') se quita ANTES de todo: con la coma dentro se parte
    # en dos trozos y el nombre acaba presentado como un medicamento no disponible. Caso real
    # (conv 2109, provider 19): "Robi, me puedes dar el precio del beprospem" → el cliente
    # veía "⚠️ No disponibles en el catálogo: ROBI".
    user_text = "\n".join(_sin_vocativo(p) for p in parts)
    await ctx.store.add_message(
        conv.id, "user", user_text, wa_message_id=inbound[0].wa_message_id
    )

    # --- Armar mensajes para el LLM ---------------------------------------
    referral = next((m.referral_headline for m in inbound if m.referral_headline), None)
    offered = await ctx.store.get_offered_slots(conv.id)
    # MULTI-TENANT: perfil del tenant de ESTA conversación (saludo/tono propios
    # de la farmacia), no un perfil global cacheado.
    profile = await resolve_profile(ctx, str(crm_conv_id))
    # El providerId del catálogo lo define el CRM por tenant (organization.
    # provider_id) y llega en el contexto — NO es variable de entorno fija.
    # Se calcula ANTES de armar el prompt para inyectar el bloque de farmacia.
    provider_id = (context or {}).get("providerId") or settings.provider_id or ""
    farmacia = bool(provider_id)
    system = build_system_prompt(
        profile=profile,
        context=context,
        conv=conv,
        referral_headline=referral,
        offered=offered,
        tz=_agent_tz(settings),
    )
    # Bloque de farmacia: guía al LLM a distinguir consultas/recetas de
    # medicamentos de otros mensajes del negocio (contratos, página web, chat,
    # comparador, horarios, etc.). Sin esto, el chasis genérico de agendamiento
    # no sabe que este negocio es una farmacia y el LLM puede responder con una
    # búsqueda de catálogo a un mensaje sobre un contrato.
    if farmacia:
        system = system + "\n\n" + _FARMACIA_BLOCK

    # --- Resumen de estado determinista (anti-deriva) ---------------------
    # Inyecta el estado real de la conversación (de la DB, no del LLM) para que
    # el modelo sepa exactamente en qué fase está y no invente contexto entre
    # turnos. Esto es lo que contiene el no-determinismo del flujo encadenado.
    #
    # El historial se trae ANTES de armar el bloque: para saber si el turno
    # anterior del agente pidió precisar la búsqueda hay que mirar el último
    # mensaje del asistente. Sin ese dato, la respuesta corta del cliente
    # ("Tabletas 650") se lee como un mensaje suelto y el agente vuelve a listar
    # las 20 presentaciones mezcladas.
    # Se traen más mensajes de los que ve el LLM: el candado de cierre cuenta
    # el hilo COMPLETO del lead, no solo la ventana de contexto.
    recientes = await ctx.store.recent_messages(conv.id, STALL_LOOKBACK)
    history = recientes[-settings.history_window :]
    pidio_precisar = _agente_pidio_precisar(
        [{"role": m.role, "content": m.content} for m in history]
    )
    cart = await ctx.store.cart_items(
        conv.id, session_hours=ctx.settings.cart_session_hours
    )
    state_block = _build_state_block(
        conv, cart, respuesta_cliente=user_text, pidio_precisar=pidio_precisar
    )
    if state_block:
        system = system + "\n\n" + state_block
    # Los mensajes del CLIENTE vuelven al LLM sin los marcadores del sistema — SALVO
    # el del turno EN CURSO. Al llegar una imagen/audio, `media.py` guarda (y se
    # persiste) un marcador que es una INSTRUCCIÓN NUESTRA:
    #
    #   [El lead mandó una imagen — la tienes adjunta, puedes verla. OCR de la
    #    imagen: "Fexofenadina Clorhidrato 120 mg 10 Tabletas". Si es un
    #    medicamento/receta, interpreta la imagen, extrae el/los medicamento(s) que
    #    pide y consúltalos en el catálogo (buscar_medicamento). No inventes
    #    disponibilidad.]
    #
    # En el TURNO de la imagen eso es correcto y necesario. Pero el marcador se queda
    # en el historial y en los turnos SIGUIENTES el LLM lo lee como si el cliente lo
    # hubiera dicho y lo OBEDECE: el cliente escribe "Hola" y el agente busca y ofrece
    # la Fexofenadina de la foto de dos días antes.
    #
    # Caso real (provider 19, 2026-10): dos "Hola" seguidos → las dos veces el agente
    # ofreció FEXOFENADINA 120 MG en lugar de saludar, porque el marcador de la imagen
    # estaba en su ventana de contexto. Reproducido 3/3 con el LLM real y el historial
    # real de la BD.
    #
    # Se conserva el DATO del cliente (el OCR) y se descartan las instrucciones. El
    # mensaje del turno en curso NO se toca: quitarlo dejaría al LLM sin la
    # instrucción cuando de verdad toca leer la foto (la ruta de más valor, medido:
    # el turno de la imagen es el ÚNICO en que el marcador debe llegar íntegro).
    # `user_text` es exactamente lo que se acaba de persistir para este turno, así
    # que el mensaje del turno en curso se identifica por su contenido. Si no se
    # encontrara (p. ej. el marcador llegó vacío), se cae al ÚLTIMO mensaje del
    # cliente del historial — que es el de este turno — antes que limpiar de más:
    # tocar el turno de la imagen rompería la lectura de recetas por foto.
    idx_actual = next(
        (i for i in range(len(history) - 1, -1, -1)
         if history[i].role == "user" and (history[i].content or "") == user_text),
        None,
    )
    if idx_actual is None:
        idx_actual = next(
            (i for i in range(len(history) - 1, -1, -1) if history[i].role == "user"),
            -1,
        )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] + [
        {
            "role": m.role,
            "content": (
                m.content
                if m.role != "user" or i == idx_actual
                else _texto_cliente_sin_marcadores(m.content)
            ),
        }
        for i, m in enumerate(history)
    ]
    # Hostilidad sostenida (AC-18): el CONTEO es determinista — el LLM salió
    # flaky contando entre turnos. Al tercer strike: alerta en el turno y
    # handoff garantizado más abajo aunque el modelo no llame la herramienta.
    streak = hostile_streak([m.content for m in history if m.role == "user"])
    if streak >= 3:
        messages.append({"role": "system", "content": HOSTILITY_ALERT})
    # Guarda G3 (canal sin media): el cliente menciona foto/adjunto pero este
    # turno NO tiene imagen ni OCR → señal explícita para que jamás afirme
    # haber recibido nada (falla "Recibí tu foto" del Laboratorio).
    tiene_ocr = bool(_texto_ocr_completo(user_text))
    if (
        farmacia
        and not image_uris
        and not tiene_ocr
        and guards.menciona_media(user_text)
    ):
        logger.info("guarda G3: lead menciona media sin imagen real — inyecto restricción de canal")
        messages.append({"role": "system", "content": guards.NO_MEDIA_ALERT})
    # Candado de cierre: conversación que no va a ningún lado. Se despide con
    # UNA línea cálida en este turno y después calla (gate 1.5). El conteo es
    # determinista aquí; el LLM solo pone la redacción.
    del_lead = [m.content for m in recientes if m.role == "user"]
    cerrar_sin_rumbo = streak < 3 and sin_rumbo(del_lead, conv.phase)
    if cerrar_sin_rumbo:
        logger.info(
            "turno %s: sin rumbo (%d mensajes del lead, racha vacía %d) — cierro",
            identity,
            len(del_lead),
            racha_vacia(del_lead),
        )
        messages.append({"role": "system", "content": STALL_ALERT})
    if image_uris:
        # El último user message de este turno se vuelve multimodal: el
        # historial persiste solo el texto; las imágenes viven en ESTE turno.
        last = messages[-1]
        last["content"] = [{"type": "text", "text": str(last["content"])}] + [
            {"type": "image_url", "image_url": {"url": uri}} for uri in image_uris
        ]

    # --- LLM con tools ----------------------------------------------------
    # provider_id y farmacia ya se calcularon arriba (para el bloque de farmacia).
    runtime = ToolRuntime(ctx, conv, str(crm_conv_id), profile=profile, provider_id=provider_id)
    # El mensaje del cliente, para que las tools puedan leer de ahí lo que el LLM quitó del
    # término de búsqueda (la FORMA: 'omeprazol en cápsulas' → término 'omeprazol').
    runtime.texto_cliente = user_text
    # Cargar el último producto consultado persistido en el turno anterior
    # (para el backstop de carrito cuando el cliente responde una cantidad).
    # Defensivo: asyncpg puede devolver JSONB como str si el codec no se aplicó.
    if isinstance(conv.last_product, dict):
        runtime.last_product = conv.last_product
    elif isinstance(conv.last_product, str) and conv.last_product.strip():
        try:
            runtime.last_product = json.loads(conv.last_product)
        except Exception:
            runtime.last_product = None
    runtime.last_term = (conv.last_term or "") if isinstance(conv.last_term, str) else ""
    # Término del OCR de la última imagen del cliente, del turno anterior. Es el
    # dato correcto cuando el cliente solo pregunta "por la foto" sin nombrar el
    # medicamento (buscar 'foto' traería FOTORRETIN por substring).
    runtime.last_ocr_term = (
        conv.last_ocr_term if isinstance(conv.last_ocr_term, str) else ""
    )
    runtime.prev_last_ocr_term = runtime.last_ocr_term
    # Lista de opciones persistida del turno anterior (para resolver
    # "quiero X cajas de la opción Z").
    if isinstance(conv.last_options, list):
        runtime.last_options = conv.last_options
    elif isinstance(conv.last_options, str) and conv.last_options.strip():
        try:
            runtime.last_options = json.loads(conv.last_options)
        except Exception:
            runtime.last_options = []
    # «¿SOLO LO TIENES POR SEPARADO?» — pregunta sobre la respuesta ANTERIOR, no un
    # medicamento. Se contesta con el producto que se acaba de cotizar, sin pasar por el LLM:
    # el modelo negaba el producto dos líneas después de haberle dado el precio.
    if farmacia and _pregunta_por_separado(user_text):
        _prod_sep = getattr(runtime, "last_product", None) or (
            runtime.last_options[0] if runtime.last_options else None
        )
        if isinstance(_prod_sep, dict) and _prod_sep.get("producto"):
            logger.info(
                "backstop por separado: '%s' → respondo con el producto ya cotizado",
                user_text[:40],
            )
            await _send(
                ctx,
                conv.id,
                str(crm_conv_id),
                _con_confirmacion_carrito(
                    _respuesta_por_separado(_prod_sep), runtime
                ),
            )
            return
    # Modo farmacia (providerId ya calculado arriba): se exponen las tools de
    # catálogo y se retiran las de agenda.
    # Heartbeat de typing: mantiene los 3 puntitos vivos mientras el turno
    # procesa (la consulta de medicamento excede la vida del composing de
    # Evolution, ~25 s). Se cancela al salir del tool_loop.
    typing_stop = asyncio.Event()
    typing_task = asyncio.create_task(
        _typing_heartbeat(ctx, str(crm_conv_id), typing_stop)
    )
    try:
        final_text = await _tool_loop(ctx, messages, runtime, farmacia=farmacia, user_text=user_text)
    except LlmExhausted as exc:
        logger.error(
            "turno %s: LLM agotó reintentos (%s) — silencio + handoff error",
            identity,
            exc,
        )
        await _safe_handoff(ctx, str(crm_conv_id), "error")
        await ctx.store.update_conversation(
            conv.id, phase="cerrada", followup_due_at=None
        )
        return
    finally:
        typing_stop.set()
        typing_task.cancel()
        # ESPERAR a que el heartbeat muera de verdad (no solo pedir cancel):
        # `cancel()` solo programa la cancelación; si un post_typing ya estaba
        # en vuelo, su request llega a Evolution DESPUÉS del `paused` final y
        # REVIVE los 3 puntitos (bug: los puntitos quedan encendidos tras
        # responder). Con el await, ningún composing queda en vuelo.
        try:
            await asyncio.wait_for(typing_task, timeout=2.0)
        except BaseException:
            pass

    # Backstop determinista: al tercer strike el handoff SUCEDE, lo haya
    # llamado el modelo o no (la regla de negocio no depende de su humor).
    if streak >= 3 and runtime.handoff_reason is None:
        runtime.handoff_reason = "hostilidad"
    # Guarda G4: acusación de fraude dirigida → escalado INMEDIATO (un solo
    # mensaje basta; el agente del Laboratorio respondía "no tengo capacidad"
    # o seguía vendiendo en vez de escalar).
    if runtime.handoff_reason is None and guards.acusacion_fraude(user_text):
        logger.warning("guarda G4: acusación de fraude — handoff inmediato")
        runtime.handoff_reason = "hostilidad"
        # T8: el cierre de escalado es PLANTILLA CONSTANTE, no generación LLM.
        final_text = guards.TPL_CIERRE_G4

    # NO se hace handoff automático por medicamento no disponible: si este turno
    # se buscó algo que no está en el catálogo, el agente lo informa con
    # honestidad pero deja el chat abierto y ofrece seguir ayudando (buscar otro
    # medicamento, sugerir un genérico, etc.). El handoff solo ocurre cuando el
    # CLIENTE lo pide explícitamente (hablar con una persona) o hay hostilidad.
    cart_activo = bool(
        await ctx.store.cart_items(conv.id, session_hours=settings.cart_session_hours)
    )

    # Backstop de contradicción: si el catálogo SÍ devolvió productos pero el
    # LLM niega disponibilidad en su texto final (alucinación no-determinista),
    # reemplazamos el texto con la lista real. El cliente jamás recibe un "no
    # lo tenemos" falso cuando el producto sí está.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and _niega_disponibilidad(final_text)
    ):
        logger.warning(
            "backstop contradicción: el LLM negó disponibilidad pese a %d productos — reemplazo con lista real",
            len(runtime.last_products),
        )
        final_text = _lista_con_aviso(
            runtime.last_products,
            runtime.last_products_term or runtime.last_term or "",
            "\n\n" + MENSAJE_SUGERIDO_CARRITO,
        )

    # Backstop de lista desordenada: si el LLM enumeró TODOS los productos
    # pero en un orden distinto al canónico (precio ascendente), reemplazamos
    # con la lista ordenada. El número de opción que el cliente ve debe
    # coincidir SIEMPRE con el orden interno de last_options (resolución de
    # "opción Z" / número suelto en el siguiente turno).
    if (
        farmacia
        and runtime.last_products
        and final_text
        and _lista_desordenada(final_text, runtime.last_products)
    ):
        logger.warning(
            "backstop lista desordenada: el LLM mostró %d productos fuera del orden canónico — reemplazo con lista ordenada por precio",
            len(runtime.last_products),
        )
        final_text = _lista_con_aviso(
            runtime.last_products,
            runtime.last_products_term or runtime.last_term or "",
            "\n\n" + MENSAJE_SUGERIDO_CARRITO,
        )

    # Backstop de formato no canónico: el LLM enumeró los productos pero con
    # su propio estilo (markdown, $ con punto, sin 💊 ni Bs). Se reemplaza por
    # la lista canónica: mismo orden (precio asc), formato estándar de la
    # farmacia (💊 N. NOMBRE + $X,XX | Bs Y). El cliente SIEMPRE ve el mismo
    # formato, venga lo que venga del LLM.
    #
    # GUARD DEL CIERRE: si el cliente se está despidiendo, su respuesta es una cortesía y
    # no se sustituye por el catálogo. Caso real (conv 2826): "Ok gracias pasaré por allá"
    # → el backstop reemplazó el adiós con la lista de NAPROXENO que el cliente YA había
    # visto. `_enumera_productos` (dentro del predicado) ya descarta las cortesías; este
    # guard es la segunda capa para el caso de una despedida larga que cite un precio.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and not _lead_esta_cerrando(_texto_cliente_sin_marcadores(user_text))
        and _formato_no_canonico(final_text, runtime.last_products)
    ):
        logger.warning(
            "backstop formato: el LLM enumeró %d productos sin el formato canónico — reemplazo con lista estándar",
            len(runtime.last_products),
        )
        final_text = _lista_con_aviso(
            runtime.last_products,
            runtime.last_products_term or runtime.last_term or "",
            "\n\n" + MENSAJE_SUGERIDO_CARRITO,
        )

    # Backstop de omisión: si el catálogo devolvió productos pero el texto final
    # NO menciona el medicamento (ni el término ni ningún nombre de producto),
    # el cliente recibiría un "¿Cuál prefieres?" sin contexto. Reemplazamos con
    # la lista real para que la respuesta sea autocontenida.
    #
    # GUARD DEL CIERRE: si el lead acaba de agradecer/despedirse, NO se re-lista.
    # Caso real (provider 27, 2026-10): tras la consulta de omeprazol + asaprol el
    # cliente escribió "Gracias". El LLM contestó cortés y sin repetir productos
    # (correcto), pero este backstop vio `last_products` con productos y un texto
    # que no los mencionaba → REEMPLAZÓ la cortesía con la lista completa de
    # ASAPROL PINA (PINZA UMBILICAL, pañales...). El cliente que se despedía
    # recibió otra vez el catálogo. Re-listar a quien ya se despidió es el mismo
    # daño que la repregunta que ya se corrigió en el texto.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and not _lead_esta_cerrando(_texto_cliente_sin_marcadores(user_text))
        and not _menciona_producto(final_text, runtime.last_products, runtime.last_term)
    ):
        logger.warning(
            "backstop omisión: el LLM no mencionó el producto pese a %d resultados — reemplazo con lista real",
            len(runtime.last_products),
        )
        final_text = _lista_con_aviso(
            runtime.last_products,
            runtime.last_products_term or runtime.last_term or "",
            "\n\n" + MENSAJE_SUGERIDO_CARRITO,
        )

    # Backstop de precios inventados: si el catálogo devolvió productos pero el
    # texto final cita un precio ($) que NO coincide con ningún producto real,
    # el LLM alucinó marcas/precios pese a mencionar el término. Reemplazamos
    # con la lista real para que el cliente jamás reciba un precio falso.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and _cita_precio_inventado(final_text, runtime.last_products)
    ):
        logger.warning(
            "backstop precio inventado: el LLM citó un precio falso pese a %d productos — reemplazo con lista real",
            len(runtime.last_products),
        )
        final_text = _lista_con_aviso(
            runtime.last_products,
            runtime.last_products_term or runtime.last_term or "",
            "\n\n" + MENSAJE_SUGERIDO_CARRITO,
        )

    # Backstop de handoff injustificado: si el catálogo SÍ devolvió productos
    # pero el LLM despide al cliente o lo pasa a humano (sin que el medicamento
    # esté agotado), reemplazamos con la lista real. El cliente jamás debe ser
    # despedido cuando hay productos disponibles.
    #
    # GUARD DEL CIERRE: si quien se despide es EL CLIENTE (no el LLM), re-listar
    # es justo lo contrario de lo que corresponde. Aquí el texto del LLM es una
    # despedida porque el lead se despidió: se le deja la cortesía y NO se le
    # devuelve el catálogo.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and not runtime.med_not_found
        and _es_despedida_o_handoff(final_text)
        and not _lead_esta_cerrando(_texto_cliente_sin_marcadores(user_text))
    ):
        logger.warning(
            "backstop handoff injustificado: el LLM despidió pese a %d productos — reemplazo con lista real",
            len(runtime.last_products),
        )
        final_text = _lista_con_aviso(
            runtime.last_products,
            runtime.last_products_term or runtime.last_term or "",
            "\n\n" + MENSAJE_SUGERIDO_CARRITO,
        )

    # AVISO DE FORMA: si el cliente pidió una presentación (suspensión, gotas, tabletas) y
    # la que hay es otra, la respuesta tiene que DECIRLO aunque el LLM redacte su propia
    # versión. Los productos llegan ya acotados por la tool; este aviso es lo que impide que
    # la lista se lea como "esto es lo que pediste".
    # AVISO DE CONCENTRACIÓN: si el cliente pidió una dosis y NINGÚN producto la tiene, el
    # mensaje lo dice SIEMPRE (aunque el LLM redacte su propia versión). Hermano del aviso
    # de forma. Caso real (provider 32, 2026-10-29): "RIVAROXABAN DE 20mg" se respondió con
    # dos presentaciones de 15 mg sin una sola palabra de que la de 20 no existe.
    if farmacia and final_text and runtime.last_products:
        _term_cli = _extraer_termino_medicamento(
            _texto_cliente_sin_marcadores(user_text) or user_text or ""
        ) or ""
        _dosis_cli = _extraer_dosis(_term_cli) if _term_cli else ""
        if _dosis_cli:
            _aviso_d = _aviso_dosis_agotada(runtime.last_products, _dosis_cli)
            if _aviso_d and _aviso_d not in final_text:
                logger.info("backstop dosis: antepongo el aviso — %s", _aviso_d)
                final_text = _aviso_d + "\n\n" + final_text

    _aviso_forma_rt = getattr(runtime, "aviso_forma", "") or ""
    if farmacia and final_text and _aviso_forma_rt and _aviso_forma_rt not in final_text:
        logger.info("backstop forma: antepongo el aviso — %s", _aviso_forma_rt)
        final_text = _aviso_forma_rt + "\n\n" + final_text

    # Backstop de bloque de carrito: SIEMPRE que se consultaron productos
    # (last_products) y el texto final es una lista de presentaciones (no un
    # handoff), garantizar que el MENSAJE_SUGERIDO_CARRITO esté al final. El
    # LLM a veces genera la lista correcta pero omite el bloque, dejando al
    # cliente sin saber cómo agregar al carrito.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and not runtime.med_not_found
        and not _es_despedida_o_handoff(final_text)
    ):
        # Quitar frases redundantes del LLM que duplican el bloque estándar
        # ("Si deseas agregarlo a tu carrito...", "indícame cuántas cajas").
        final_text = _quitar_invito_carrito(final_text)
        # Quitar TODAS las copias del bloque del pie que el LLM haya generado
        # por su cuenta (imitando el historial), dejando solo la que adjuntamos
        # al final. Evita el pie duplicado 2-3 veces.
        final_text = _quitar_pie_carrito_duplicado(final_text)
        final_text = final_text.rstrip()
        if not final_text.endswith(MENSAJE_SUGERIDO_CARRITO):
            logger.info(
                "backstop bloque carrito: adjuntar MENSAJE_SUGERIDO_CARRITO a la lista de %d productos",
                len(runtime.last_products),
            )
            final_text += "\n\n" + MENSAJE_SUGERIDO_CARRITO

    # Backstop determinista del RESUMEN del carrito: si este turno se llamó
    # ver_carrito (el cliente pidió el resumen o dijo LISTO) y el carrito tiene
    # productos, se reemplaza SIEMPRE el texto del LLM por el resumen canónico
    # (generado en _ver_carrito). Garantiza que por cada medicamento aparezcan
    # cantidad y subtotal en USD Y Bs, y el total en ambos — aunque el modelo
    # omita el monto en Bs. Se ejecuta después de los backstops de lista porque
    # last_products no aplica aquí (ver_carrito consulta cart_items).
    #
    # PERO NO SI EL CLIENTE ADEMÁS ESTÁ AGREGANDO PRODUCTOS: en ese caso el texto del
    # LLM es el que trae lo que el cliente pidió añadir, y sustituirlo por el resumen
    # del carrito ACTUAL lo hace desaparecer. Caso real (conv 2109, 2026-10-17):
    #     cliente: "Me puedes dar el resumen de esto y le agregas 3 cajas de glymar de
    #               4 MG y 2 toallas securezza x 15"
    #     el LLM enumeró GLYMAR y SECUREZZA  →  este backstop los borró y dejó solo el
    #     resumen del carrito (COLAYTE). El cliente no volvió a ver lo que pidió.
    if farmacia and runtime.cart_summary_text and not _tambien_pide_agregar(user_text):
        logger.info(
            "backstop resumen determinista: reemplazando texto del LLM por el resumen canónico del carrito"
        )
        final_text = runtime.cart_summary_text
    elif farmacia and runtime.cart_summary_text:
        logger.info(
            "backstop resumen determinista: NO se aplica — el cliente además está "
            "agregando productos; se respeta el texto del LLM"
        )

    # --- Guardas anti-alucinación (QA T4) — vetos deterministas -------------
    # Se ejecutan AL FINAL: un veto reemplaza el texto por plantilla constante
    # y/o fuerza handoff. Cada activación queda logueada para el conteo de
    # vetos del reporte QA.
    # Negativa con carrito activo: el cliente dijo "no" a más medicamentos.
    # En este turno G1-ext/G5/G6 no deben pisar el resumen del pedido.
    es_negativa_con_carrito = (
        farmacia and cart_activo
        and _quiere_ver_resumen(user_text, tiene_carrito=True)
    )
    if farmacia and final_text:
        # G2 — política fuera del KB: escalado hardcodeado, jamás generación.
        if guards.intencion_fuera_kb(user_text):
            logger.warning("guarda G2 VETO: intención fuera de KB (%r) — plantilla de escalado", user_text[:80])
            final_text = guards.TPL_FUERA_KB
            if runtime.handoff_reason is None:
                runtime.handoff_reason = "fuera_de_kb"
        # G3 — el agente afirma haber recibido una imagen que NO llegó.
        elif (
            not image_uris
            and not tiene_ocr
            and guards.afirma_recibir_media(final_text)
        ):
            logger.warning("guarda G3 VETO: el agente afirmó recibir media inexistente — plantilla sin-media")
            final_text = guards.TPL_SIN_MEDIA
        # G1 — precio citado sin respaldo del catálogo.
        elif (
            not runtime.last_products
            and not runtime.cart_summary_text
            and guards.pide_precio_sin_dato(user_text)
            and re.search(
                r"\$\s*\d|\bBs\.?\s*\d|\d+(?:[.,]\d+)?\s*B", final_text, re.I
            )
        ):
            logger.warning("guarda G1 VETO: precio sin respaldo de catálogo — plantilla sin-precio")
            final_text = guards.TPL_SIN_PRECIO
        # G1-ext — superlativo de precio ('el más económico', 'el genérico de')
        # afirmado por el agente sin datos del catálogo en este turno.
        elif (
            not es_negativa_con_carrito
            and not runtime.last_products
            and not runtime.cart_summary_text
            and guards.cita_superlativo_precio(final_text)
        ):
            logger.warning("guarda G1 VETO: superlativo de precio sin respaldo de catálogo — plantilla sin-precio")
            final_text = guards.TPL_SIN_PRECIO
        # G5 — principio activo/composición/genérico afirmado sin respaldo de
        # catálogo. El agente inventa la composición de un medicamento cuando
        # no hay resultados de tool en este turno (Daflon → 'ramiprilo').
        elif (
            not es_negativa_con_carrito
            and guards.afirma_composicion(final_text)
            and not runtime.last_products
        ):
            logger.warning("guarda G5 VETO: afirmación de composición/genérico sin respaldo — plantilla sin-composición")
            final_text = guards.TPL_SIN_COMPOSICION
        # G6 — precio con dígitos, stock o principio activo directo afirmado
        # sin respaldo de catálogo. Complementa G1 (que solo actúa cuando el
        # cliente preguntó el precio) y G5 (composición más amplia): G6 cubre
        # el LLM que inventa datos aunque el cliente NO haya pedido precio
        # explícitamente (p. ej. "el Daflon cuesta $5", "hay 10 unidades").
        elif (
            not es_negativa_con_carrito
            and not runtime.last_products
            and not runtime.cart_summary_text
            and guards.afirma_dato_catalogo(final_text)
        ):
            logger.warning("guarda G6 VETO: datos de producto sin respaldo de catálogo — plantilla verificación")
            final_text = guards.TPL_VERIFICACION_CATALOGO
        # G7 — eco: el agente devuelve casi literalmente el mensaje del cliente
        # en lugar de ejecutar la acción (buscar, agregar, confirmar) o pedir
        # el dato que falta. Caso QA 'Comprador decidido': el agente repitió
        # "Perfecto, quiero 2 cajas de losartan 50 mg." sin llamar ninguna tool.
        elif guards.es_echo(final_text, user_text):
            logger.warning("guarda G7 VETO: eco del mensaje del cliente — plantilla anti-eco")
            final_text = guards.TPL_NO_ECHO
        # G8 — placeholder sin ejecutar: el LLM escribió la plantilla de una
        # tool-call en lugar de ejecutarla (p. ej. '[inserta información del
        # producto desde buscar_medicamento]'). Caso QA 'Errores y modismos'.
        elif guards.tiene_placeholder(final_text):
            logger.warning("guarda G8 VETO: placeholder de herramienta en texto final — plantilla búsqueda honesta")
            final_text = guards.TPL_BUSQUEDA_HONESTA
        # G12 — tool-call JSON cruda filtrada al texto: el LLM emitió la llamada
        # a herramienta como texto plano ('{"name":"sugerir_generico",...}') en
        # vez de ejecutarla. Caso real del Laboratorio (fase 10, G11b-A,
        # pregunton_precios). G8 no lo cubre (solo corchetes '[inserta...]').
        elif guards.contiene_toolcall_json(final_text):
            logger.warning("guarda G12 VETO: tool-call JSON cruda en texto final — plantilla G12")
            final_text = guards.TPL_G12_TOOLCALL
        # G9 — cliente pide hablar con humano pero el LLM no llamó handoff:
        # el bench marcaba el cierre seco ("Entendido…") como debio_escalar/tono.
        # Plantilla constante empática + handoff garantizado.
        elif guards.pide_humano(user_text) and runtime.handoff_reason is None:
            logger.warning("guarda G9 VETO: cliente pide humano sin handoff — plantilla cierre escalado")
            final_text = guards.TPL_CIERRE_ESCALADO
            runtime.handoff_reason = "lead_request"
        # G10a — clase terapéutica o uso farmacológico afirmado sin haber consultado
        # el catálogo en este turno. Caso 'pregunton_precios' del Laboratorio: el
        # agente describía "Daflón 500 es un antiinflamatorio AINE que se usa para..."
        # sin dato de herramienta. G5/G6 no cubren clases terapéuticas amplias.
        # NO se activa si el turno SÍ llamó buscar_medicamento (consulted_catalog).
        elif (
            not runtime.consulted_catalog
            and guards.afirma_composicion_farmacologica(final_text)
        ):
            logger.warning("guarda G10 VETO: composición/uso farmacológico sin catálogo — plantilla honesta G10")
            final_text = guards.TPL_G10_COMPOSICION
        # G10b — promesa de búsqueda futura sin haber llamado herramienta en el turno.
        # El agente dice "Voy a buscar", "Déjame verificar", "Busco y te aviso" sin
        # haber ejecutado buscar_medicamento: el cliente queda esperando una acción
        # que nunca ocurrirá en este turno.
        elif (
            not runtime.consulted_catalog
            and guards.promete_busqueda_sin_accion(final_text)
        ):
            logger.warning("guarda G10 VETO: promesa de búsqueda sin tool call — plantilla honesta G10")
            final_text = guards.TPL_G10_PROMESA
        # G11 — el agente prometió escalar a un humano por iniciativa propia
        # (ej: "te comunico con un asesor", "alguien te contactará") sin haber
        # llamado la herramienta handoff. G9 cubre el caso donde el CLIENTE
        # pide humano; G11 cubre la promesa espontánea del agente sin handoff.
        elif (
            guards.promete_escalado(final_text)
            and runtime.handoff_reason is None
        ):
            logger.warning("guarda G11 VETO: agente prometió escalado sin handoff — plantilla G11")
            final_text = guards.TPL_G11_ESCALADO
            runtime.handoff_reason = "lead_request"

    # Negativa con carrito: si algún guard pisó el texto, restaurar el resumen
    # canónico del carrito (ver_carrito ya se forzó en el tool_loop).
    if es_negativa_con_carrito and runtime.cart_summary_text and final_text != runtime.cart_summary_text:
        logger.info("negativa con carrito: restaurando resumen canónico tras posible veto")
        final_text = runtime.cart_summary_text

    # Si lo cotizado es OTRO fármaco del que pidió el cliente, se responde honesto.
    # Va DESPUÉS de las guardas QA: un fármaco equivocado pesa más que cualquier veto.
    if farmacia and final_text and runtime.last_products:
        _otro = _farmaco_distinto(user_text, runtime)
        if _otro:
            # La FORMA pedida no sale en el aviso: "No tengo BUSCAPINA AMP" sobre un
            # 'buscapina amp' se lee como si existiera algo llamado "buscapina amp";
            # el mensaje honesto nombra el fármaco ("No tengo BUSCAPINA"). Si el término
            # era solo la forma, se muestra el original.
            final_text = _respuesta_no_es_ese_farmaco(
                _termino_sin_forma(_otro) or _otro
            )
            runtime.last_products = []

    sent = False
    if final_text and final_text.strip():
        # Sanitiza el markup interno de handoff (si el modelo lo escribió como
        # texto en vez de llamar la tool): jamás debe llegar al cliente.
        clean = _strip_internal_markup(final_text.strip())
        clean = _quitar_ofrecimiento_consulta(clean)
        # Si el lead acaba de agradecer o despedirse, el agente NO debe repreguntar
        # ("¿Quieres que busque alguno de los medicamentos que mencionaste?"). Se
        # evalúa SOLO el contenido del cliente (sin los marcadores del sistema), así
        # que funciona igual si la despedida llegó escrita o en una nota de voz.
        clean = _quitar_repregunta_tras_cierre(
            clean, _texto_cliente_sin_marcadores(user_text) or None
        )
        # No repetir la dirección/horario ni volver a pedir el nombre de la caja; y si
        # preguntan el teléfono, contestar a eso (ver `_quitar_repeticiones`).
        clean = _quitar_repeticiones(clean, user_text, history)
        # El encabezado no puede prometer la concentración que el mensaje niega.
        clean = _titulo_sin_dosis_negada(clean)
        clean = _con_confirmacion_carrito(clean, runtime)
        if clean:
            sent = await _send(ctx, conv.id, str(crm_conv_id), clean)
            if sent:
                await ctx.store.add_message(conv.id, "assistant", clean)
    # Apagar los puntitos explícitamente tras entregar la respuesta: el último
    # composing (aun con delay=0) y sobre todo el del heartbeat tienen delay y
    # Evolution los re-envía internamente; si no mandamos "paused", los 3
    # puntitos reaparecen DESPUÉS de las opciones y se apagan solos ~3 s
    # después. Este paused corta de raíz el indicador justo cuando el mensaje
    # ya llegó.
    if sent:
        try:
            await ctx.crm.post_paused(str(crm_conv_id))
        except Exception:
            pass
        # Segundo `paused` diferido: Evolution (delay=0) puede tardar en procesar
        # el primero, y si algún composing quedó encolado en su lado, este
        # re-apagado lo corta. Best-effort, no bloquea el turno.
        try:
            await asyncio.sleep(1.0)
            await ctx.crm.post_paused(str(crm_conv_id))
        except Exception:
            pass

    # El handoff se ejecuta DESPUÉS de la despedida (si no, el CRM la rechaza
    # con 409 ai_paused). EXCEPCIÓN: si hay carrito activo, el cliente está en
    # medio de un pedido — un handoff que el LLM llamó tras una negativa ("no"
    # a "¿otro medicamento?") no debe matar la conversación; se cancela y se
    # responde con el resumen.
    if runtime.handoff_reason is not None and cart_activo:
        logger.warning(
            "handoff cancelado: hay carrito activo (%s) — el pedido sigue",
            runtime.handoff_reason,
        )
        runtime.handoff_reason = None
    if runtime.handoff_reason is not None:
        await _safe_handoff(ctx, str(crm_conv_id), runtime.handoff_reason)

    # --- Fase + seguimiento -----------------------------------------------
    updates: dict[str, Any] = {"greeted": True}
    if runtime.last_product:
        updates["last_product"] = runtime.last_product
    if runtime.last_term:
        updates["last_term"] = runtime.last_term
    # Persistir el término del OCR de la imagen para el turno siguiente (cuando el
    # cliente pregunte "por la foto" sin nombrar el medicamento).
    if runtime.last_ocr_term:
        updates["last_ocr_term"] = runtime.last_ocr_term
    if runtime.last_options:
        updates["last_options"] = runtime.last_options
    if cerrar_sin_rumbo:
        # Se marca aunque el envío haya fallado: la decisión de cerrar ya se
        # tomó y no queremos que el próximo mensaje reabra el ciclo.
        updates["stalled_at"] = utcnow()
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    elif runtime.handoff_reason is not None or runtime.booked or runtime.routed_out:
        updates["phase"] = "cerrada"
        updates["followup_due_at"] = None
    else:
        if runtime.proposed:
            updates["phase"] = "agendando"
        # SEGUIMIENTO: solo si hay un PEDIDO que no se convirtió en venta. Antes se
        # agendaba con CUALQUIER turno enviado (una consulta de precio, un "gracias",
        # una reserva de demo) → 40 empujones a gente que nunca mostró intención de
        # comprar. El negocio lo pidió explícito: seguimiento del PEDIDO a las 4 h si NO
        # se convirtió en venta.
        #   · hay ítems en el carrito → hay algo concreto que retomar (no un "¿sigues ahí?")
        #   · el pedido NO se cerró → si el cliente finalizó (LISTO), ya compró y
        #     empujarlo molesta. Se lee el flag FRESCO de la BD porque finalizar_pedido
        #     lo escribe durante el turno (la `conv` en memoria puede estar vieja).
        if sent and not conv.followup_sent:
            try:
                pedido_abierto = bool(
                    await ctx.store.cart_items(
                        conv.id, session_hours=settings.followup_max_age_hours
                    )
                )
                fresca_conv = await ctx.store.get_or_create_conversation(
                    conv.wa_identity
                )
                ya_cerrado = bool(getattr(fresca_conv, "cart_closed", False))
            except Exception:
                # Ante la duda, NO agendar: un seguimiento de más molesta; uno de menos
                # solo pierde una oportunidad.
                pedido_abierto, ya_cerrado = False, True
            if pedido_abierto and not ya_cerrado:
                updates["followup_due_at"] = utcnow() + timedelta(
                    hours=settings.followup_hours
                )
    await ctx.store.update_conversation(conv.id, **updates)

    # Cerrar la traza de observabilidad con el resultado del turno.
    trace.update(
        output={
            "respondio": bool(sent),
            "handoff": runtime.handoff_reason,
            "med_not_found": runtime.med_not_found,
        }
    )


async def _run_reset(ctx: AppContext, conv: Any, identity: str) -> None:
    """Reinicio de pruebas: CRM primero (ficha limpia + IA reactivada, para que
    la confirmación no rebote con 409 ai_paused) y luego la memoria local."""
    crm_conv_id = conv.crm_conversation_id
    if not crm_conv_id:
        context = await _fetch_context(ctx, identity)
        crm_conv_id = ((context or {}).get("conversation") or {}).get("id")
    if crm_conv_id:
        try:
            await ctx.crm.post_reset(str(crm_conv_id))
        except CrmError as exc:
            logger.warning("reset %s: el CRM no pudo reiniciar (%s) — sigo", identity, exc)
    await ctx.store.reset_conversation(conv.id)
    logger.info("reset de pruebas ejecutado para %s", identity)
    if crm_conv_id:
        await _send(
            ctx,
            conv.id,
            str(crm_conv_id),
            "🧹 Listo: memoria reiniciada. Te trato como lead nuevo desde tu "
            "próximo mensaje. (Comando de pruebas, solo líneas autorizadas.)",
        )


async def _fetch_context(
    ctx: AppContext, identity: str, conversation_id: str | None = None
) -> dict[str, Any] | None:
    for attempt in range(CONTEXT_ATTEMPTS):
        try:
            context = await ctx.crm.get_context(identity, conversation_id)
        except CrmError as exc:
            logger.warning(
                "context de %s: error del CRM (intento %d): %s",
                identity,
                attempt + 1,
                exc,
            )
            context = None
        if context is not None:
            return context
        if attempt < CONTEXT_ATTEMPTS - 1:
            await asyncio.sleep(1.0)  # chance a que el relay aterrice en el CRM
    return None


async def _preguntar_metodo_entrega(
    ctx: AppContext, runtime: ToolRuntime
) -> None:
    """Pregunta el MÉTODO DE ENTREGA y deja el estado pendiente. ÚNICO ESCRITOR.

    Centraliza los tres sitios que enviaban el menú (el helper PRE-LLM y los dos
    guards del despacho). Con una sola definición no pueden divergir ni duplicar el
    envío: el cliente vio el menú DOS veces cuando había copias.

    Idempotente dentro del turno (`delivery_pregunta_enviada`) y contra el estado
    persistente (`delivery_pending`).
    """
    conv = runtime._conv
    await ctx.store.update_conversation(conv.id, delivery_pending="method")
    conv.delivery_pending = "method"
    runtime.delivery_pendiente = "method"
    runtime.delivery_pregunta_enviada = True
    runtime.delivery_pregunta = True
    logger.info("paso de entrega: pregunto delivery/retiro antes del resumen")
    await _send(ctx, conv.id, runtime._crm_conv_id, MENSAJE_METODO_ENTREGA)


async def _resolver_paso_entrega(
    ctx: AppContext,
    runtime: ToolRuntime,
    user_text: str,
    *,
    farmacia: bool = False,
) -> tuple[bool, str | None]:
    """Resuelve el PASO DE ENTREGA antes de llamar al LLM. Un solo punto para todos.

    Devuelve ``(manejado, texto)``:

      * ``(False, None)`` — no hay nada del paso de entrega en vuelo: seguir con el LLM.
      * ``(True, None)``  — el turno ya se atendió (se envió una pregunta por ``_send``).
      * ``(True, resumen)`` — ya está todo resuelto; enviar ese texto.

    POR QUÉ AQUÍ Y NO EN LA RAMA SIN-TOOLS: cuando el cliente responde al menú de
    entrega, el modelo NO se queda sin tool-calls — llama ``finalizar_pedido`` o
    ``ver_carrito`` directamente, así que esa rama nunca se evalúa. Medido en
    producción (provider 05, 2026-10):

        20:57:58  paso de entrega: preguntando delivery/retiro      ← menú #1
        20:58:11  backstop carrito: '2' responde a la pregunta de ENTREGA
        20:58:13  guard finalizar: sin método de entrega — pregunto  ← menú #2
        20:58:13  backstop resumen determinista                      ← resumen SIN método

    El cliente vio el menú DOS veces y su "2" se perdió. Al resolverlo antes del LLM,
    en el único punto por el que pasan TODOS los turnos, el modelo no puede saltárselo.
    """
    if not farmacia:
        return False, None

    conv = runtime._conv
    pendiente = conv.delivery_pending or ""

    # --- 1) RESPUESTA A UNA PREGUNTA DE ENTREGA EN VUELO ---
    if pendiente == "method":
        eleccion = _eleccion_entrega(user_text)
        if eleccion == "delivery":
            await runtime.guardar_eleccion_entrega("delivery")
            logger.info("paso de entrega: eligió DELIVERY — pido la dirección")
            await _send(ctx, conv.id, runtime._crm_conv_id, MENSAJE_PEDIR_DIRECCION)
            return True, None
        if eleccion == "pickup":
            await runtime.guardar_eleccion_entrega("pickup")
            logger.info("paso de entrega: eligió RETIRAR EN FARMACIA — muestro el resumen")
            result = await runtime.execute("ver_carrito", {})
            if not runtime.cart_summary_text:
                # Sin carrito no hay resumen que mostrar; que el LLM continúe.
                return False, None
            return True, runtime.cart_summary_text
        # EL CLIENTE NO CONTESTA EL MENÚ, PERO PIDE EL TOTAL.
        #
        # Ser flexible: contestarle lo que está pidiendo en vez de repetirle la pregunta.
        # Caso real (conv 1003, provider 19, 2026-10-16): el cliente escribió
        #     "Cuánto sería el total."  y  "Cuánto sería el monto total de los 3 productos ?"
        # y recibió el menú de entrega (y después nada). Nunca vio su total.
        #
        # La pregunta de entrega SE QUEDA EN VUELO a propósito: el cliente puede contestar
        # "1"/"2" en el turno siguiente y el flujo continúa con normalidad. Limpiarla aquí
        # haría que el guard de `ver_carrito` volviera a preguntar el método.
        if _pide_el_total(user_text):
            result = await runtime.execute("ver_carrito", {})
            if runtime.cart_summary_text:
                logger.info(
                    "paso de entrega: el cliente pidió el TOTAL sin contestar el menú "
                    "— muestro el resumen y dejo la pregunta de entrega en vuelo",
                )
                return True, runtime.cart_summary_text
        # No se reconoce: puede ser una consulta nueva (el cliente cambió de tema).
        # Se limpia la pregunta pendiente y se deja al LLM atender el mensaje.
        logger.info(
            "paso de entrega: %r no responde al menú — limpio la pregunta y sigo",
            user_text[:60],
        )
        await ctx.store.update_conversation(conv.id, delivery_pending="")
        conv.delivery_pending = ""
        runtime.delivery_pendiente = ""
        return False, None

    if pendiente == "address":
        if _parece_direccion(user_text):
            direccion = _texto_cliente_sin_marcadores(user_text) or user_text.strip()
            await runtime.guardar_direccion_entrega(direccion.strip())
            logger.info("paso de entrega: DIRECCIÓN guardada (%r)", direccion[:70])
            result = await runtime.execute("ver_carrito", {})
            return True, runtime.cart_summary_text
        logger.info(
            "paso de entrega: %r no parece dirección — la pido de nuevo",
            user_text[:60],
        )
        await _send(ctx, conv.id, runtime._crm_conv_id, MENSAJE_PEDIR_DIRECCION)
        return True, None

    # --- 2) ¿HAY QUE PREGUNTAR EL MÉTODO AHORA? ---
    # El cliente quiere ver el resumen y aún no eligió cómo recibirlo. Se pregunta
    # ANTES del resumen, en este mismo punto (cubre todos los caminos, incluido que el
    # LLM llame ver_carrito por su cuenta).
    #
    # LA CONDICIÓN ES `delivery_method is None` A SECAS. NO se usa
    # `cart_summary_shown` como puerta: ese flag significa "ya mostré un resumen en
    # esta conversación" y es MEMORIA DE HISTORIAL, no de si ESTE pedido tiene método.
    #
    # Caso real (provider 05, 08-oct): el cliente cerró un pedido a las 00:16 — con el
    # código ANTERIOR al fix de la auditoría, así que `cart_summary_shown` quedó en
    # True para siempre. A las 01:20 hizo un pedido NUEVO: el carrito estaba vacío (se
    # limpió al cerrar) y `delivery_method` era None, pero el flag viejo decía True, así
    # que el paso de entrega se OMITIÓ y el resumen salió sin MÉTODO DE ENTREGA.
    #
    # El dato autoritativo de "este pedido ya tiene método" es `delivery_method`, que
    # se limpia al cerrar el pedido. El flag del resumen es de otra cosa: si además
    # quedó colgado de un pedido anterior, no hay que arrastrarlo.
    carrito = await ctx.store.cart_items(
        conv.id, session_hours=ctx.settings.cart_session_hours
    )
    # `delivery_method is None` es el ÚNICO requisito de "este pedido no tiene método".
    # `cart_summary_shown` NO participa: es memoria de "ya mostré un resumen", no de si
    # ESTE pedido tiene método de entrega (ver el comentario de arriba).
    if (
        carrito
        and conv.delivery_method is None
        and _quiere_ver_resumen(user_text, tiene_carrito=True)
        # Y NO SE PREGUNTA SI EL CLIENTE TODAVÍA ESTÁ AGREGANDO PRODUCTOS.
        # Preguntar el método de entrega mientras el cliente sigue dictando el pedido
        # corta el turno y se traga lo que pidió. Caso real (conv 2109, 2026-10-17):
        #     "Me puedes dar el resumen de esto y le agregas 3 cajas de glymar de 4 MG
        #      y 2 toallas securezza x 15"  →  solo recibió el menú de entrega.
        # Ver `_tambien_pide_agregar`.
        and not _tambien_pide_agregar(user_text)
    ):
        # Si el cliente ya dijo "delivery"/"retirar" en el mismo mensaje, no se
        # pregunta: se toma la elección.
        eleccion = _eleccion_entrega(user_text)
        if eleccion:
            await runtime.guardar_eleccion_entrega(eleccion)
            if eleccion == "delivery":
                await _send(ctx, conv.id, runtime._crm_conv_id, MENSAJE_PEDIR_DIRECCION)
                return True, None
            result = await runtime.execute("ver_carrito", {})
            return True, runtime.cart_summary_text
        await _preguntar_metodo_entrega(ctx, runtime)
        return True, None

    return False, None


async def _tool_loop(
    ctx: AppContext,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    *,
    farmacia: bool = False,
    user_text: str = "",
) -> str | None:
    """Rondas de tool-calling hasta obtener texto final (o rendirse)."""

    # ------------------------------------------------- PASO DE ENTREGA ---
    # SE RESUELVE AQUÍ, EN CÓDIGO Y ANTES DE LLAMAR AL LLM. Es el único punto por el
    # que pasa TODOS los turnos, así que el modelo no puede saltárselo.
    #
    # POR QUÉ NO EN LA RAMA SIN-TOOLS (donde estaba): cuando el cliente responde "2"
    # al menú de entrega, el modelo NO se queda sin tool-calls — llama
    # `finalizar_pedido`/`ver_carrito` directamente. Esa rama nunca se evalúa, así que
    # la respuesta del cliente se descartaba y el guard del despacho volvía a
    # preguntar el método. Medido en producción (provider 05, 2026-10):
    #
    #     20:57:58  paso de entrega: preguntando delivery/retiro      ← menú #1
    #     20:58:11  backstop carrito: '2' responde a la pregunta de ENTREGA
    #     20:58:13  guard finalizar: sin método de entrega — pregunto  ← menú #2
    #     20:58:13  backstop resumen determinista                      ← resumen sin método
    #
    # El cliente vio el menú DOS veces y el resumen salió sin el método de entrega.
    # ------------------------------------------------- HANDOFF POR PETICIÓN ---
    # El cliente pide hablar con una persona → se atiende SIEMPRE y A LA PRIMERA, en
    # código, sin depender del modelo. Va antes de todo lo demás (incluido el paso de
    # entrega y cualquier búsqueda): quien pide un humano suele estar frustrado y no
    # debe recibir una lista de productos ni una respuesta fría.
    #
    # Caso real (conv 2720, 07-oct): "Pasame el humano" → el agente respondió "no puedo
    # pasarte el contacto de ninguna persona directamente" y NO hizo el handoff. El
    # prompt pedía llamar la tool handoff "a la primera", pero sin guard determinista el
    # caso quedaba al azar del modelo.
    if farmacia:
        texto_cliente_handoff = _texto_cliente_sin_marcadores(user_text) or user_text
        if _pide_hablar_con_humano(texto_cliente_handoff):
            logger.info(
                "handoff por petición del cliente: %r — aviso al equipo y despido cálido",
                texto_cliente_handoff[:60],
            )
            await ctx.store.update_conversation(
                runtime._conv.id, phase="cerrada", followup_due_at=None
            )
            await _safe_handoff(ctx, runtime._crm_conv_id, "lead_request")
            await _send(
                ctx, runtime._conv.id, runtime._crm_conv_id, MENSAJE_HANDOFF_HUMANO
            )
            return None

    resuelto, texto_entrega = await _resolver_paso_entrega(
        ctx, runtime, user_text, farmacia=farmacia
    )
    if resuelto:
        return texto_entrega  # None = ya se atendió con _send; texto = el resumen

    # GUARD DE ENVASE AGOTADO — ANTES DEL LLM, junto al paso de entrega.
    #
    # El cliente pide un TAMAÑO DE CAJA ('de 10 pastillas'). Si el catálogo no lo tiene,
    # hay que DECÍRSELO con los tamaños que sí hay — no re-listar los de 30 como si fueran
    # la respuesta.
    #
    # Caso real (conv 2714, provider 27, BRASARTAN): el cliente preguntó "No tienes de 10
    # pastillas?" y el agente respondió re-listando los envases de 30, los MISMOS que ya
    # había mostrado. El cliente se fue creyendo que sí había respuesta a su pregunta,
    # cuando la había y era "no". Re-listar sin decir el NO es no responder: el cliente no
    # puede distinguir "no tengo" de "no me entendió".
    #
    # POR QUÉ AQUÍ Y NO EN EL LOOP: en el loop el turno ya pasó por el LLM, que con
    # 'brasartan 80 mg 10' consulta el catálogo y deja `consulted_catalog=True`. Un guard
    # condicionado a esa bandera nunca dispararía. Es el mismo error que tuvo el paso de
    # entrega. Los pasos obligatorios se resuelven ANTES del modelo.
    if farmacia:
        # ¿PREGUNTA SI EL EQUIPO VIENE CON LOS MATERIALES?
        # El catálogo no guarda el contenido de la caja (ver `_EQUIPOS_CONSUMIBLES`), así
        # que no se puede asegurar. Lo que sí se puede es ofrecer los insumos que se venden
        # aparte — que es lo que haría un humano de mostrador, en vez de un "no tengo
        # información" seco seguido de un handoff.
        # RECETA A MANO O ILEGIBLE: no se transcribe ni se cotiza. Se avisa que un
        # auxiliar atiende y se registra el handoff para que un humano la lea.
        tipo_receta = _receta_ilegible(user_text)
        if tipo_receta:
            logger.info(
                "guard receta ilegible (%s): aviso al auxiliar + handoff", tipo_receta
            )
            texto_receta = (
                RESPUESTA_RECETA_MANUSCRITA if tipo_receta == "mano"
                else RESPUESTA_RECETA_DUDOSA
            )
            await _send(ctx, runtime._conv.id, runtime._crm_conv_id, texto_receta)
            await _safe_handoff(ctx, str(runtime._crm_conv_id), "modelo")
            return None  # turno atendido: no se consulta el catálogo

        # ¿PIDE UNA FOTO DEL PRODUCTO? Se acusa recibo con una frase humana y NO se
        # inventa que la enviamos ni se busca "fotos" en el catálogo. Si el cliente está
        # ENVIANDO una imagen (hay OCR en el turno), esto no aplica.
        if _pide_foto(user_text) and not _texto_ocr_completo(user_text):
            logger.info("guard foto: el cliente pide una foto — acuso recibo")
            await _send(ctx, runtime._conv.id, runtime._crm_conv_id, RESPUESTA_FOTO)
            return None  # turno atendido: no dejar que el LLM reescriba

        familia = _familia_de_pregunta_contenido(
            user_text, runtime.last_term, runtime.last_product
        )
        if farmacia and familia:
            # El nombre del EQUIPO se toma ANTES de buscar: `buscar_medicamento` pisa
            # `last_term`/`last_product` con el consumible, y la frase acababa diciendo
            # "en el catálogo el LANCETAS … figura como una unidad".
            producto_ctx = str(runtime.last_term or "").strip()
            if not producto_ctx and isinstance(runtime.last_product, dict):
                producto_ctx = str(
                    runtime.last_product.get("producto")
                    or runtime.last_product.get("nombre")
                    or ""
                ).strip()
            consumibles: list[dict[str, Any]] = []
            for insumo in _EQUIPOS_CONSUMIBLES[familia]:
                data_ins = await runtime.execute("buscar_medicamento", {"nombre": insumo})
                consumibles.extend((data_ins or {}).get("products") or [])
            # Dedupe por nombre y ordenar por precio, como el resto de listas.
            vistos: set[str] = set()
            limpios: list[dict[str, Any]] = []
            for p in consumibles:
                clave = str(p.get("producto") or p.get("nombre") or "").strip().upper()
                if clave and clave not in vistos:
                    vistos.add(clave)
                    limpios.append(p)
            if _familia_de_equipo(producto_ctx) != familia:
                # El contexto no habla del equipo: se usa la palabra del propio cliente.
                producto_ctx = familia
            producto_ctx = producto_ctx or "el aparato"
            logger.info(
                "guard contenido: pregunta por el contenido de '%s' (%s) — ofrezco %d insumo(s)",
                producto_ctx, familia, len(limpios),
            )
            texto = _respuesta_contenido_equipo(str(producto_ctx), limpios)
            await _send(ctx, runtime._conv.id, runtime._crm_conv_id, texto)
            return None  # turno atendido: no dejar que el LLM reescriba

        env_pedido = _pedido_de_envase(user_text)
        # EL MENSAJE NO DEBE TRAER SU PROPIO MEDICAMENTO.
        #
        # Este guard aplica el tamaño de caja que el cliente mencionó (`x 30 Tab`) al
        # ÚLTIMO medicamento de la conversación. Eso es correcto cuando el cliente está
        # afinando la consulta anterior ("no tienes de 10 pastillas?"), pero NO cuando
        # llega con una consulta nueva que trae sus propios medicamentos.
        #
        # Caso real (conv 2109, provider 19, 2026-10-16): el cliente pidió 8 medicamentos
        #     "Glimepiride de. 4 MG x 30 Tab, candesatan 16 x 30 Tab, ..."
        # y `last_term` era 'discolayte' (de una sesión del 9-oct). El guard tomó el '30'
        # de "x 30 Tab", buscó DISCOLAYTE, no lo encontró en envase de 30 y respondió
        #     "No tengo DISCOLAYTE en envase de 30 unidades. Lo tengo en: 69 unidades."
        # NINGUNO de los 8 medicamentos se buscó: el guard RETORNA y el camino de lista
        # nunca corre. Es la misma regla que ya se aplicó al backstop de refinamiento:
        # la FORMA refina, el FÁRMACO manda.
        if env_pedido and runtime.last_term and not _trae_medicamento_propio(
            user_text, runtime.last_term
        ):
            # Se busca con el término LIMPIO: last_term puede traer el envase ya pegado
            # ('brasartan 80 mg 10') y contaminaría la consulta.
            term_busqueda = _termino_sin_envase(runtime.last_term, env_pedido)
            data_env = await runtime.execute(
                "buscar_medicamento", {"nombre": term_busqueda}
            )
            prods_env = (data_env or {}).get("products") or []
            hay_env = [
                p for p in prods_env
                if _envase_de_nombre(p.get("producto") or p.get("nombre")) == env_pedido
            ]
            if not hay_env:
                disponibles = sorted(
                    {
                        _envase_de_nombre(p.get("producto") or p.get("nombre"))
                        for p in prods_env
                        if _envase_de_nombre(p.get("producto") or p.get("nombre"))
                    },
                    key=int,
                )
                logger.info(
                    "guard envase: '%s' pidió envase %s y NO hay — disponibles: %s",
                    runtime.last_term, env_pedido, disponibles,
                )
                texto = _respuesta_envase_agotado(
                    runtime.last_term, env_pedido, disponibles, prods_env
                )
                await _send(ctx, runtime._conv.id, runtime._crm_conv_id, texto)
                return None  # turno atendido: no dejar que el LLM reescriba
            # SÍ existe: se acota la lista a ese envase y sigue el flujo normal.
            logger.info(
                "guard envase: '%s' envase %s → %d de %d opciones",
                runtime.last_term, env_pedido, len(hay_env), len(prods_env),
            )
            runtime.last_options = sorted(
                hay_env,
                key=lambda p: (p.get("precio") if isinstance(p.get("precio"), (int, float))
                               else 0),
            )

    schemas = active_tool_schemas(farmacia=farmacia)
    # Cuenta rondas consecutivas donde el LLM llamó tools con arguments vacíos
    # ({}): señal de bucle degenerado — cortamos con un texto de respaldo.
    empty_rounds = 0
    # Pre-check de elección por número de opción ("quiero 2 cajas de la opción 3"
    # o selección múltiple "quiero 1 caja de 1,4,7 y 8"): se resuelve ANTES de
    # la primera llamada al LLM, para que el modelo no sobrescriba
    # runtime.last_options con una búsqueda nueva y la opción Z quede fuera de
    # rango. (El cliente elige contra la lista que YA vio.)
    eleccion_prev = _extraer_eleccion_multiple(user_text)
    # UN NÚMERO QUE RESPONDE A NUESTRA PREGUNTA NO ES UNA ELECCIÓN DE PRODUCTO.
    # Si el agente está esperando la respuesta del paso de ENTREGA ("1. Delivery /
    # 2. Retirar en Farmacia") o la DIRECCIÓN, un "1"/"2" contesta ESA pregunta.
    #
    # Caso real (provider 05, 2026-10): el cliente pidió ACETAMINOFEN 650 MG, vio la
    # lista de 3 opciones, eligió "2" (LA SANTE, correcto). Luego se le preguntó
    # delivery/retiro, contestó "1" (delivery) — y ese "1" se resolvió contra
    # `last_options` como la OPCIÓN 1 de la lista: se agregó GENVEN ($0,52) además de
    # LA SANTE ($0,65). El resumen mostró DOS acetaminofén que el cliente nunca pidió.
    #
    # El pre-check vive aquí, ANTES de mi guard de entrega, así que hay que filtrarlo
    # en la fuente: mientras haya una pregunta de entrega en vuelo, ningún número
    # suelto puede ser una elección de opción.
    if eleccion_prev and _hay_pregunta_de_entrega(runtime):
        logger.info(
            "backstop carrito: '%s' responde a la pregunta de ENTREGA — "
            "no es una elección de opción",
            user_text[:40],
        )
        eleccion_prev = None
    # Si el asistente acaba de preguntar "¿cuántas cajas/unidades?", un número
    # suelto ("2") es la CANTIDAD del producto, NO la elección de una opción.
    # La pregunta de cantidad tiene prioridad sobre la lista de opciones.
    if eleccion_prev and _pregunta_es_cantidad(messages) and re.fullmatch(
        r"\s*\d{1,2}\s*", user_text or ""
    ):
        eleccion_prev = None
    # F14.2 — "opción N" en mensaje MIXTO: la selección de una opción que el
    # cliente YA vio es una COMPRA CONFIRMADA y tiene prioridad sobre cualquier
    # búsqueda nueva. Se marca el turno para que el backstop de catálogo no
    # reinterprete el resto del texto como medicamento (antes: extraía
    # "macrodantina querer vitamina opción" y buscaba esa basura, pisando
    # last_options y dejando la opción 9 fuera de rango).
    if farmacia and runtime.last_options and _menciona_opcion_numerada(user_text):
        runtime.opcion_resuelta = True
        logger.info(
            "F14.2 opción numerada en mensaje mixto: resuelvo la selección "
            "(last_options=%d) y no busco término nuevo",
            len(runtime.last_options),
        )
    if (
        farmacia
        and eleccion_prev
        and runtime.last_options
        and not runtime.cart_forced
        and not runtime.consulted_catalog
    ):
        # ¿EL CLIENTE ESTÁ LEYENDO OTRA LISTA? Si alguno de los números que cita no
        # existe en la actual, no se añade NADA y se le pide aclaración. Antes se añadían
        # los que cabían —a un producto equivocado— y el resto se perdía en silencio.
        # Ver `_opciones_no_encajan`.
        if _opciones_no_encajan(eleccion_prev, runtime.last_options):
            logger.warning(
                "backstop carrito (pre-LLM): las opciones %s no caben en la lista actual "
                "(%d) — NO se agrega nada, pido aclaración",
                [op for _, op in eleccion_prev], len(runtime.last_options),
            )
            await _send(
                ctx, runtime._conv.id, runtime._crm_conv_id,
                MENSAJE_OPCIONES_NO_ENCAJAN,
            )
            return None
        runtime.cart_forced = True
        for cantidad_prev, opcion_prev in eleccion_prev:
            idx_prev = opcion_prev - 1
            if not (0 <= idx_prev < len(runtime.last_options)):
                logger.warning(
                    "backstop carrito (pre-LLM): opción %d fuera de rango (hay %d opciones)",
                    opcion_prev, len(runtime.last_options),
                )
                continue
            producto_prev = runtime.last_options[idx_prev]
            logger.info(
                "backstop carrito (pre-LLM): opción %d → %s x%d",
                opcion_prev, producto_prev.get("producto"), cantidad_prev,
            )
            args_prev = {
                "productId": producto_prev.get("productId"),
                "producto": producto_prev.get("producto") or "",
                "cantidad": cantidad_prev,
                "presentacion": producto_prev.get("presentacion") or "",
                "laboratorio": producto_prev.get("laboratorio") or "",
            }
            if producto_prev.get("precio") is not None:
                args_prev["precioUsd"] = producto_prev["precio"]
            if producto_prev.get("precioBs") is not None:
                args_prev["precioBs"] = producto_prev["precioBs"]
            result_prev = await runtime.execute("agregar_al_carrito", args_prev)
            # Registrar el SKU real que el backstop agregó para que el LLM no lo
            # re-sume si vuelve a llamar agregar_al_carrito este turno (si no,
            # la cantidad sale doblada: pidió 1 y quedan 2).
            runtime.backstop_added_skus.add(str(producto_prev.get("productId") or ""))
            runtime.cart_confirmaciones.append(
                f"• {producto_prev.get('producto') or 'producto'} ×{cantidad_prev}"
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"bkp_cart_pre_{opcion_prev}",
                            "type": "function",
                            "function": {
                                "name": "agregar_al_carrito",
                                "arguments": json.dumps(args_prev, ensure_ascii=False),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"bkp_cart_pre_{opcion_prev}",
                    "content": json.dumps(result_prev, ensure_ascii=False, default=str),
                }
            )
        # El LLM confirmará en la siguiente ronda con el resultado real.
    for _ in range(MAX_TOOL_ROUNDS):
        reply = await ctx.llm.complete(messages, tools=schemas)
        if not reply.tool_calls:
            # Backstop de carrito: si el cliente respondió con una CANTIDAD y hay un
            # producto consultado antes pero el LLM no llamó agregar_al_carrito,
            # forzamos el add en código (el modelo a veces no lo llama).
            if farmacia and not runtime.cart_forced:
                # Elección por número de opción ("quiero 2 cajas de la opción 3"
                # o selección múltiple "1 caja de 1,4,7 y 8"): resolver contra la
                # lista persistida del turno anterior.
                #
                # CON LA MISMA GUARDA que el pre-check de arriba: mientras haya una
                # pregunta de ENTREGA en vuelo, un número suelto contesta ESA pregunta
                # y no elige una opción de la lista. Este es el segundo punto donde se
                # resolvía el "1" del menú delivery/retiro como la opción 1.
                elecciones = _extraer_eleccion_multiple(user_text)
                if elecciones and _hay_pregunta_de_entrega(runtime):
                    logger.info(
                        "backstop carrito: '%s' responde a la pregunta de ENTREGA — "
                        "no es una elección de opción (rama sin-tools)",
                        user_text[:40],
                    )
                    elecciones = None
                # MISMA REGLA que en el pre-LLM: si los números citados no caben en la
                # lista actual, el cliente está leyendo OTRA lista → nada se agrega.
                if elecciones and _opciones_no_encajan(elecciones, runtime.last_options):
                    logger.warning(
                        "backstop carrito: las opciones %s no caben en la lista actual "
                        "(%d) — NO se agrega nada, pido aclaración",
                        [op for _, op in elecciones], len(runtime.last_options),
                    )
                    await _send(
                        ctx, runtime._conv.id, runtime._crm_conv_id,
                        MENSAJE_OPCIONES_NO_ENCAJAN,
                    )
                    return None
                for cantidad, opcion in (elecciones or []):
                    idx = opcion - 1
                    if not (0 <= idx < len(runtime.last_options)):
                        logger.warning(
                            "backstop carrito: opción %d fuera de rango (hay %d opciones)",
                            opcion, len(runtime.last_options),
                        )
                        continue
                    producto = runtime.last_options[idx]
                    runtime.cart_forced = True
                    logger.info(
                        "backstop carrito: forzando agregar_al_carrito (%s x%d)",
                        producto.get("producto"), cantidad,
                    )
                    args = {
                        "productId": producto.get("productId"),
                        "producto": producto.get("producto") or "",
                        "cantidad": cantidad,
                        "presentacion": producto.get("presentacion") or "",
                        "laboratorio": producto.get("laboratorio") or "",
                    }
                    if producto.get("precio") is not None:
                        args["precioUsd"] = producto["precio"]
                    if producto.get("precioBs") is not None:
                        args["precioBs"] = producto["precioBs"]
                    result = await runtime.execute("agregar_al_carrito", args)
                    runtime.backstop_added_skus.add(str(producto.get("productId") or ""))
                    runtime.cart_confirmaciones.append(
                        f"• {producto.get('producto') or 'producto'} ×{cantidad}"
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"bkp_cart_{opcion}",
                                    "type": "function",
                                    "function": {
                                        "name": "agregar_al_carrito",
                                        "arguments": json.dumps(args, ensure_ascii=False),
                                    },
                                }
                            ],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": f"bkp_cart_{opcion}",
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                if runtime.cart_forced:
                    # El LLM ya vio los resultados; evitar que el backstop de
                    # catálogo re-interprete la elección como medicamento.
                    continue
            # Backstop de horario: el cliente pregunta cuándo abre/cierra la
            # farmacia. El dato vive en Firestore (`hours` del provider) y solo se
            # obtiene con info_provider; sin forzarlo el LLM respondía "no pude
            # obtener la información del horario... ¿paso tu consulta a un
            # humano?" porque el prompt le prohíbe usar "horario" en el catálogo.
            if farmacia and not runtime.info_provider_forced and _quiere_info_horario(user_text):
                runtime.info_provider_forced = True
                logger.info("backstop horario: forzando info_provider")
                result = await runtime.execute("info_provider", {})
                _append_forced_tool(messages, "info_provider", {}, result)
                continue
            # PASO DE ENTREGA (nuevo, va ANTES del Resumen del Pedido): cuando el
            # cliente quiere ver el resumen y todavía no eligió cómo recibirlo, se
            # le pregunta primero:
            #     1. Delivery          → luego se pide la dirección
            #     2. Retirar en Farmacia
            # Solo después se muestra el Resumen, que incluye el MÉTODO DE ENTREGA.
            #
            # EL PASO DE ENTREGA SE RESUELVE EN `_resolver_paso_entrega`, AL INICIO DE
            # `_tool_loop` Y ANTES DEL LLM. Aquí había una SEGUNDA copia que enviaba el
            # menú: era código muerto (nunca se alcanzaba, porque cuando el cliente
            # responde al menú el modelo llama una tool y esta rama no se evalúa) y a
            # la vez un riesgo de DOBLE ENVÍO si algún día se alcanzaba. Un solo
            # escritor del estado de entrega: el helper.
            carrito_activo = bool(await ctx.store.cart_items(
                runtime._conv.id, session_hours=ctx.settings.cart_session_hours
            ))

            # Backstop de resumen: si el cliente quiere ver el resumen y el LLM
            # no llamó ver_carrito, lo forzamos (el modelo a veces no lo llama).
            # El "no" suelto (a "¿Deseas buscar otro medicamento?") con carrito
            # activo también cuenta: quiere el resumen, no handoff.
            #
            # PERO NO SI ADEMÁS ESTÁ AGREGANDO PRODUCTOS: forzar ver_carrito mete el
            # carrito ACTUAL en el contexto y el modelo se limita a resumirlo, dejando
            # sin atender los productos que el cliente acaba de pedir. Medido con el LLM
            # real (conv 2109, 2026-10-17): en los dos casos el carrito resumido salió
            # sin el GLYMAR ni el SECUREZZA que el cliente había pedido en el mismo
            # mensaje. Sin forzar, el modelo sí busca y enumera los dos productos.
            if (
                farmacia
                and _quiere_ver_resumen(user_text, tiene_carrito=carrito_activo)
                and not runtime.summary_forced
                and not _tambien_pide_agregar(user_text)
            ):
                runtime.summary_forced = True
                logger.info("backstop resumen: forzando ver_carrito")
                result = await runtime.execute("ver_carrito", {})
                _append_forced_tool(messages, "ver_carrito", {}, result)
                continue
            # Backstop de finalizar: si el cliente confirma el pedido y el LLM
            # no llamó finalizar_pedido, lo forzamos. Incluye el "si" suelto
            # cuando el asistente acaba de preguntar "¿Está todo correcto o
            # deseas agregar algo más?": ese "si" CONFIRMA el pedido y el LLM lo
            # leía al revés ("¡Perfecto! ¿Qué deseas agregar?").
            if (
                farmacia
                and not runtime.finalize_forced
                and (
                    _quiere_finalizar(user_text)
                    or (
                        _pregunta_cierre_resumen(messages)
                        and _es_confirmacion_resumen(user_text)
                    )
                )
            ):
                runtime.finalize_forced = True
                logger.info("backstop finalizar: forzando finalizar_pedido")
                result = await runtime.execute("finalizar_pedido", {})
                _append_forced_tool(messages, "finalizar_pedido", {}, result)
                continue
            # forzamos re-consultar el catálogo con ese refinamiento para que el
            # LLM cite los productos reales (no los invente de memoria).
            #
            # DOS CAMINOS, según lo que el cliente haya precisado:
            #  - DOSIS/FORMA ("Tabletas 650", "cap 500"): el catálogo SÍ puede
            #    filtrar por número+unidad, así que se re-consulta con el término
            #    compuesto ('acetaminofen 650 mg').
            #  - MARCA / TAMAÑO / PRECIO ("el de calox", "el de 30"): el catálogo
            #    NO puede filtrar (medido: 'acetaminofen 650 calox' devuelve los 20
            #    con TODOS los laboratorios). Se filtra la lista que el cliente YA
            #    VIO (`last_options`), que es determinista y no depende del matcher.
            if (
                farmacia
                and runtime.last_term
                and _es_refinamiento_presentacion(user_text)
                and not runtime.consulted_catalog
                # EL MENSAJE NO DEBE TRAER SU PROPIO MEDICAMENTO. Si lo trae, es una
                # CONSULTA NUEVA, no un refinamiento de la anterior.
                #
                # Caso real (conv 2287, provider 19): el cliente pidió la vitamina E y el
                # título quedó 'VIVAS' (last_term='vivas'). 22 h después escribió
                #     "Precio de airfen gotas ped"
                # y el backstop compuso term = 'vivas' + 'gotas' = 'VIVAS GOTAS', devolviendo
                # los 5 productos VIVAX otra vez. El cliente pedía AIRFEN, que estaba en su
                # propio mensaje y el catálogo resuelve ('airfen' → 2 productos).
                #
                # 'gotas' es una FORMA, así que el mensaje pasaba por refinamiento; pero
                # 'airfen' es un fármaco nuevo y manda.
                and not _trae_medicamento_propio(user_text, runtime.last_term)
            ):
                # Solo el refinamiento (mg/presentación), NO el user_text completo:
                # "tienes acido folico de 10 mg" → "10 mg" (sin verbos ni duplicar
                # el término). Concatenar user_text crudo rompía la búsqueda
                # ("acido folico tienes acido folico de 10 mg" → 0 resultados →
                # el LLM inventaba "no está disponible").
                ref = _extraer_refinamiento(user_text)
                term = f"{runtime.last_term} {ref}".strip() if ref else runtime.last_term
                logger.info(
                    "backstop refinamiento: forzando buscar_medicamento('%s')", term
                )
                result = await runtime.execute("buscar_medicamento", {"nombre": term})
                _append_forced_tool(messages, "buscar_medicamento", {"nombre": term}, result)
                continue
            # Backstop de refinamiento por ATRIBUTO (marca / tamaño / precio): el
            # cliente respondió a "¿qué miligramo necesitas?" con algo que el
            # catálogo no sabe filtrar. Se filtra la lista ya mostrada y se le pasa
            # al LLM como si fuera el resultado de una búsqueda, para que presente
            # SOLO las opciones que encajan en vez de repetir las 20.
            if (
                farmacia
                and runtime.last_term
                and runtime.last_options
                and not runtime.consulted_catalog
                and _agente_pidio_precisar(messages)
            ):
                opciones, motivo = filtrar_opciones_mostradas(
                    user_text, runtime.last_options
                )
                if motivo:
                    logger.info(
                        "backstop refinamiento por atributo: '%s' → %d/%d opciones (%s)",
                        user_text, len(opciones), len(runtime.last_options), motivo,
                    )
                    result = {
                        "ok": True,
                        "products": opciones,
                        "instrucciones": (
                            f"El cliente precisó su búsqueda anterior ({motivo}). "
                            "Presenta SOLO estas opciones con su nombre exacto y "
                            "precio (USD y Bs), en el mismo formato de lista. NO "
                            "vuelvas a mostrar las demás ni repitas la lista "
                            "completa."
                        ),
                    }
                    _append_forced_tool(
                        messages, "buscar_medicamento", {"nombre": runtime.last_term}, result
                    )
                    continue
            # Backstop anti-alucinación (farmacia): si el cliente preguntó por un
            # medicamento y el modelo NO consultó el catálogo en este turno, es
            # candidato a inventar disponibilidad/precio. Forzamos UNA consulta
            # de catálogo y volvemos a dejar que el modelo responda con datos.
            # Se salta si el cliente está cerrando el pedido (resumen/finalizar):
            # ese texto no es una búsqueda de medicamento.
            if farmacia and not runtime.summary_forced and not runtime.finalize_forced and not runtime.opcion_resuelta:
                # OCR de imagen (medicamento/receta): forzar la consulta con el
                # término extraído del marcador, sin depender del criterio del LLM.
                # catalog_retried evita re-forzar en loop (agotaba las rondas de
                # herramientas y cortaba sin texto).
                ocr_texto = _texto_ocr_completo(user_text)
                medicamentos = _parsear_medicamentos_receta(ocr_texto) if ocr_texto else []
                # CAJA vs RECETA. El OCR de UNA caja de TRIMIC FORTE L producía 27
                # productos en 3 bloques: los encabezados eran las 3 líneas del OCR
                # (nombre / principios activos / presentación), troceadas como si fueran
                # 3 medicamentos. `_medicamentos_de_ocr` decide: si el nombre de la caja
                # está en el catálogo y un producto cubre TODOS los demás componentes del
                # OCR, es UNA caja y se consulta una sola vez (el nombre es lo más
                # discriminante). Si no, es una receta y se trocea.
                if ocr_texto:
                    async def _buscar_ocr(nombre_term: str) -> list[str]:
                        data_ocr = await ctx.crm.get_products(
                            runtime._provider_id, q=nombre_term, limit=20
                        )
                        return [
                            str(p.get("nombre") or "")
                            for p in (data_ocr.get("products") or [])
                        ]

                    meds_ocr, motivo_ocr = await _medicamentos_de_ocr(
                        ocr_texto, _buscar_ocr
                    )
                    if meds_ocr:
                        logger.info(
                            "backstop OCR: %s → %d medicamento(s): %s",
                            motivo_ocr, len(meds_ocr), meds_ocr[:4],
                        )
                        medicamentos = meds_ocr
                # Lista de medicamentos en TEXTO (sin imagen): si el mensaje del
                # cliente contiene 2+ medicamentos (p. ej. "esoz, leprit y
                # evigax"), se responde con el mismo formato de receta.
                #
                # GUARD: una NEGATIVA o despedida NO es una receta. Sin esto,
                # "No gracias no las voy a comprar y disculpe" se partía por la
                # 'y' y se trataba como dos medicamentos ('voy comprar',
                # 'disculpe') -> el agente respondía "No disponibles en el
                # catálogo: DISCULPE VOY COMPRAR" más una lista de chocolates al
                # cliente que se estaba despidiendo.
                #
                # GUARD (2): NUNCA partir el MARCADOR DEL SISTEMA. Los marcadores
                # de media.py son instrucciones NUESTRAS ("[Audio del lead,
                # transcrita]: ... Es una CONSULTA del lead: interpreta la
                # transcripción, extrae el/los medicamento(s) que pide y
                # consúltalos en el catálogo..."), con comas y "y" dentro. Al
                # trocearlos, sus pedazos parecían una LISTA de fármacos y se
                # consultaban como si fueran medicamentos. Real (tenant 27):
                # un audio de "ya llegó la nifedipina de 30 mg" buscó
                # 'audio del lead' y 'extrae medicamento pide'; el primero cayó por
                # fuzzy en 'leda' ≈ 'seda' y el cliente recibió SUTURA SEDA.
                # El contenido del cliente se detecta con _texto_cliente_sin_marcadores.
                contenido_cliente = _texto_cliente_sin_marcadores(user_text)
                # AUDIO: una nota de voz es HABLA CONVERSACIONAL, no una receta. Si el
                # mensaje es una transcripción, el troceo por comas produce piezas que no
                # son medicamentos y hay que validarlas una por una.
                #
                # Caso real (conv 2834): "Buenos días mi amor, en qué precio tienen la
                # venda sol? La caja trae dos, verdad?" se troceó en
                #     ['días amor', 'en qué la venda sol? La caja trae dos', 'verdad']
                # y el cliente recibió
                #     ⚠️ No disponibles en el catálogo: DÍAS AMOR, VERDAD
                #     EN QUÉ LA VENDA SOL? LA CAJA TRAE DOS   ← el título era la frase cruda
                # o sea dos "medicamentos" que son el saludo y la coletilla, y un título
                # que es la pregunta entera. El producto correcto (VENDA ELÁSTICA) sí
                # estaba en la lista, pero envuelto en basura.
                if _texto_transcripcion_completo(user_text):
                    piezas_audio = _medicamentos_de_transcripcion(contenido_cliente or "")
                    if piezas_audio:
                        logger.info(
                            "backstop lista (audio): %d medicamento(s) reales: %s",
                            len(piezas_audio), piezas_audio[:5],
                        )
                        medicamentos = piezas_audio
                if (
                    not medicamentos
                    and _parece_lista_medicamentos(contenido_cliente)
                ):
                    # El troceado por LÍNEAS no sirve cuando la enumeración va en UNA
                    # línea con comas o 'y' ("precio de valsartan e hidroclorotiazida y
                    # omeprazol"): da un solo trozo. `_medicamentos_enumerados` respeta
                    # los separadores y devuelve cada medicamento por separado.
                    medicamentos = _medicamentos_enumerados(contenido_cliente)
                    if not medicamentos:
                        # CADA LÍNEA SE PASA POR `_sin_motivo` ANTES DE PARSEAR. Sin esto el
                        # verbo del motivo queda PEGADO al nombre y llega al cliente como si
                        # fuera un medicamento. Caso real (conv 2824): el cliente escribió
                        # "Dame precio de hidrocoticida de 12.5" y el aviso salió
                        #     ⚠️ No disponibles en el catálogo: DAME HIDROCOTICIDA, CARDEVIDOL
                        # — el verbo 'DAME' incluido, y el cliente lee que 'DAME HIDROCOTICIDA'
                        # es un medicamento que no existe. `_lineas_lista_medicamentos` reparte
                        # el texto en trozos pero NO limpia el motivo de cada uno.
                        medicamentos = _parsear_medicamentos_receta(
                            "\n".join(
                                _sin_motivo(l)
                                for l in _lineas_lista_medicamentos(contenido_cliente)
                            )
                        )
                        # UNA LÍNEA CON VARIOS MEDICAMENTOS separados por coma o " y "
                        # ("Las tres , neblun, epirinolactona y empagliflozina"): el parser
                        # va línea a línea y los dejaba PEGADOS en un solo término, que
                        # luego no matcheaba nada y encabezaba la lista con la primera
                        # palabra (TRES → AMBIENTADOR LITTLE TREES).
                        if len(medicamentos) == 1:
                            enumerados = _medicamentos_enumerados(contenido_cliente)
                            if len(enumerados) >= 2:
                                logger.info(
                                    "backstop lista: una línea con %d medicamentos "
                                    "separados — %s", len(enumerados), enumerados[:5],
                                )
                                medicamentos = enumerados
                    if medicamentos:
                        logger.info(
                            "backstop lista: %d medicamento(s) en la consulta: %s",
                            len(medicamentos), medicamentos[:5],
                        )
                # Consulta multi-medicamento en UNA línea sin separadores:
                # "disponen de clopidogrel de 75 losartan de 50 atorvastatina
                # de 30 nifedipina de 10 mg" — el patrón 'de <dosis>' repetido
                # separa los medicamentos.
                if not medicamentos:
                    medicamentos = _partir_consulta_multi(contenido_cliente)
                if medicamentos and not runtime.receta_atendida:
                    runtime.receta_atendida = True
                    runtime.catalog_retried = True
                    # DOSIS POR MEDICAMENTO, extraída ANTES de tocar el término: la dosis
                    # vive en la LÍNEA del cliente y hay que conservarla para filtrar. El
                    # término va LIMPIO para que la cascada de typos resuelva el nombre.
                    dosis_por_term: dict[str, str] = {}
                    for _t, _d in _medicamentos_con_dosis(contenido_cliente or ""):
                        if _d:
                            dosis_por_term[_t] = _d
                    # El ENVASE que pidió el cliente, también por línea. Se extrae igual
                    # que la dosis: de la LÍNEA original (donde vive el número).
                    envase_por_term: dict[str, str] = {}
                    for _linea in _lineas_lista_medicamentos(contenido_cliente or ""):
                        _term = _extraer_termino_medicamento(_sin_motivo(_linea))
                        if not _term:
                            continue
                        _env = _envase_pedido_en_linea(_linea)
                        if _env:
                            envase_por_term[_term] = _env
                    logger.info(
                        "backstop receta: %d medicamentos detectados — consultando todos",
                        len(medicamentos),
                    )
                    grupos: list[tuple[str, list[dict[str, Any]]]] = []
                    no_disponibles: list[str] = []
                    # Términos que se descartaron por no ser un medicamento (solo la marca
                    # del laboratorio o solo la sal). Si no queda ningún grupo, la respuesta
                    # correcta es pedir el nombre de la caja.
                    terminos_descartados: list[str] = []
                    avisos: dict[str, str] = {}
                    # Productos que el cliente pidió AGREGAR explícitamente y que se
                    # pueden añadir sin preguntar: cantidad dicha por él + un único
                    # candidato. Ver el bloque que los agrega, después del bucle.
                    agregables: list[tuple[str, dict[str, Any], int]] = []
                    for med in medicamentos:
                        runtime.corregido_desde = None
                        runtime.corregido_a = None
                        result = await runtime.execute("buscar_medicamento", {"nombre": med})
                        prods = (result or {}).get("products") or []
                        # EL TÉRMINO ERA EL LABORATORIO, NO UN MEDICAMENTO.
                        # Caso real (conv 7, provider 05, 2026-10-27): la foto de una caja de
                        # 'Fexofenadina Clorhidrato CALOX' se leyó como dos medicamentos,
                        # FEXOFENADINA y CALOX, y la respuesta incluyó un catálogo entero de
                        # esa marca (20 productos: acetaminofén, amlodipina, albendazol...).
                        # Se ignora en SILENCIO: no es un "no disponible" — el cliente nunca
                        # nombró un fármaco CALOX.
                        # EL TÉRMINO ERA LA SAL DEL PRINCIPIO ACTIVO ('clorhidrato').
                        # No es un medicamento ni un "no disponible": se ignora en silencio.
                        # Caso real (2026-10-27): el OCR de una caja de FEXOFENADINA CLORHIDRATO
                        # devolvió solo 'CLORHIDRATO' y se cotizaron betahistina, metformina,
                        # bromexina y loperamida.
                        # ¿EL TÉRMINO ES UNA FRASE, NO UN MEDICAMENTO?
                        # Una PREGUNTA sobre un producto ("¿el Glucómetro HLM-78 viene con las
                        # plaquitas de la sangre...?") troceada por líneas produce términos que
                        # no son medicamentos. Buscarlos devuelve basura o productos que no
                        # vienen al caso; la regla es no responder con algo incoherente.
                        if not _termino_es_medicamento_plausible(med):
                            logger.info(
                                "receta: '%s' no parece un medicamento (frase/pregunta) — no "
                                "busco en el catálogo", med,
                            )
                            continue
                        if getattr(runtime, "es_sal", False):
                            logger.info(
                                "receta: '%s' es solo la SAL del principio activo — se ignora "
                                "(sin aviso de no disponible)", med,
                            )
                            terminos_descartados.append(med)
                            continue
                        if getattr(runtime, "es_laboratorio", False):
                            logger.info(
                                "receta: '%s' es el LABORATORIO de los productos, no un "
                                "medicamento — se ignora (sin aviso de no disponible)",
                                med,
                            )
                            terminos_descartados.append(med)
                            continue
                        # FILTRAR POR LA DOSIS QUE PIDIÓ EL CLIENTE.
                        #
                        # SE FILTRA AL FINAL, NO SE BUSCA CON LA DOSIS. Medido: si el
                        # término lleva la dosis pegada, `_quitar_saludos` convierte el
                        # decimal en un espacio ('hidrocoticida 12.5 mg' → 'hidrocoticida
                        # 12 5 mg') y la cascada de typos YA NO ENCUENTRA el fármaco → 0
                        # productos. El typo debe resolverse con el nombre LIMPIO y la dosis
                        # aplicarse después, sobre los productos devueltos.
                        #
                        # POR QUÉ HACE FALTA EL FILTRO LOCAL: el motor del catálogo matchea
                        # por el fármaco e IGNORA el número, así que devuelve las
                        # concentraciones mezcladas (medido: 'carvedilol 6.25 mg' → 5 de
                        # 25 mg). El cliente escribió su dosis; hay que mostrarle solo esa.
                        dosis_pedida = (
                            dosis_por_term.get(med)
                            or _dosis_sin_unidad(med)
                            or _extraer_dosis(med)
                        )
                        aviso_dosis = ""
                        if dosis_pedida:
                            antes = len(prods)
                            # El aviso se calcula ANTES de filtrar, sobre la lista completa:
                            # necesita saber qué concentraciones HAY para poder decir
                            # "lo tengo en". Es el hermano del aviso de envase.
                            aviso_dosis = _aviso_dosis_agotada(prods, dosis_pedida)
                            prods = _filtrar_por_dosis(prods, dosis_pedida)
                            logger.info(
                                "receta: '%s' dosis %s → %d de %d productos%s",
                                med, dosis_pedida, len(prods), antes,
                                f' · aviso: {aviso_dosis}' if aviso_dosis else "",
                            )
                        # FILTRAR POR EL ENVASE (cuántas tabletas trae la caja).
                        # El motor del catálogo ignora los números igual que ignoraba la
                        # dosis: el cliente pidió "4 MG x 30 Tab" y recibía también el
                        # envase de 10. Mismo patrón que la dosis: se busca con el nombre
                        # LIMPIO y se filtra DESPUÉS sobre lo devuelto.
                        # ORDEN: primero el ENVASE, porque la dosis necesita saberlo para
                        # no confundirse con él (ver el guard de abajo).
                        envase_pedido = (
                            envase_por_term.get(med) or _envase_pedido_en_linea(med)
                        )
                        dosis_pedida = (
                            dosis_por_term.get(med)
                            or _dosis_sin_unidad(med)
                            or _extraer_dosis(med)
                        )
                        # UN NÚMERO IGUAL AL ENVASE NO ES UNA DOSIS.
                        # El término puede acabar con el tamaño de caja cuando el cliente
                        # lo escribió ('metformina de 500 x 30' → término 'metformina 30'):
                        # leer ese '30' como concentración filtraba los productos por el
                        # número 30 y devolvía las concentraciones mezcladas otra vez
                        # (medido: 'dosis 30 → 15 de 20', TODOS los envases de 30 con
                        # 500/850/1000/1 MG dentro). La dosis real —'500'— viene de la
                        # LÍNEA, vía `_dosis_antes_del_envase`.
                        if dosis_pedida and envase_pedido and dosis_pedida == envase_pedido:
                            dosis_pedida = ""
                        aviso_envase = ""
                        if envase_pedido:
                            antes_e = len(prods)
                            # El aviso se calcula ANTES de filtrar, sobre la lista completa:
                            # necesita saber qué envases HAY para poder decir "lo tengo en".
                            aviso_envase = _aviso_envase_agotado(prods, envase_pedido)
                            prods = _filtrar_por_envase(prods, envase_pedido)
                            logger.info(
                                "receta: '%s' envase %s → %d de %d productos%s",
                                med, envase_pedido, len(prods), antes_e,
                                f' · aviso: {aviso_envase}' if aviso_envase else "",
                            )
                        # EL TÍTULO LLEVA LA GRAFÍA DEL CATÁLOGO, no la que escribió el
                        # cliente. Si hubo corrección por typo ('hidrocoticida' →
                        # 'hidroclorotiazida'), encabezar con el typo repetiría el error en
                        # la respuesta y el cliente no sabría qué producto se le está
                        # ofreciendo. El encabezado identifica el grupo; debe ser el nombre
                        # con el que el producto existe.
                        # LA FORMA PEDIDA, EN EL TÍTULO. Si el cliente pidió "apiret en
                        # suspensión" y no hay suspensión, el aviso lo dice y el encabezado
                        # no puede seguir prometiéndola (misma regla que con la dosis).
                        aviso_forma_receta = getattr(runtime, "aviso_forma", "") or ""
                        _base_titulo = runtime.corregido_a or med
                        if aviso_forma_receta:
                            _base_titulo = _termino_sin_forma(_base_titulo)
                        titulo = _base_titulo.upper()
                        # EL TÍTULO NO LLEVA NI LA PALABRA DE LA CAJA NI EL LABORATORIO.
                        # Caso real (conv 7, provider 05, 2026-10-27): la foto de
                        # 'Fexofenadina Clorhidrato CALOX' se leyó como
                        # 'Medicamento Fexofenadina Clorhidrato Calox' y el encabezado de la
                        # lista salió así, con 'MEDICAMENTO' delante y la marca detrás.
                        # 'Medicamento Genérico' es texto de la caja, y CALOX es el
                        # laboratorio: se comprueba contra los productos devueltos, que son
                        # los que confirman que ese token cierra los títulos (ver
                        # `_es_nombre_de_laboratorio`). Nunca se quita el fármaco.
                        if prods:
                            titulo = _titulo_de_lista(titulo, prods)
                            # Y si NINGÚN producto lleva el nombre pedido (el cliente
                            # escribió 'epirinolactona' y el catálogo tiene
                            # ESPIRONOLACTONA), el encabezado pasa a la marca real: el
                            # mensaje no puede encabezar con una palabra que no es la del
                            # producto. Misma regla que `_lista_con_aviso`.
                            titulo = _titulo_por_marcas(prods, titulo)
                            # Y si la CONCENTRACIÓN pedida no está, el encabezado la
                            # pierde: el mensaje avisa "no tengo de 20 mg" y a la vez
                            # encabezaba "RIVAROXABAN 20 MG" (foto del envase, provider 32,
                            # 2026-10-29). Misma regla que `_avisos_de_lista`.
                            if aviso_dosis and dosis_pedida:
                                titulo = _titulo_sin_dosis(titulo, dosis_pedida)
                        if prods:
                            grupos.append((titulo, prods))
                            # Los dos avisos van juntos si los dos aplican: la
                            # concentración pedida no está Y el envase tampoco.
                            _avisos_titulo = " ".join(
                                a for a in (
                                    aviso_forma_receta, aviso_dosis, aviso_envase
                                ) if a
                            )
                            if _avisos_titulo:
                                avisos[titulo.strip().upper()] = _avisos_titulo
                            # ¿PIDIÓ ALGO QUE NO TENGO? El motor del catálogo se cae al OR
                            # cuando la búsqueda exacta no da nada, así que una marca que no
                            # existe desaparece en SILENCIO y el cliente recibe otras marcas
                            # bajo el título de lo que pidió. Se comprueba sobre el término
                            # EFECTIVO (el corregido, si hubo corrección): así 'acido
                            # acetalicio' no avisa, porque la corrección a acetilsalicílico
                            # sí está en los productos devueltos.
                            _term_ef = getattr(runtime, "corregido_a", None) or med
                            _ausente = _nucleo_ausente(_term_ef, prods)
                            if _ausente:
                                logger.info(
                                    "receta: '%s' no está en NINGUNO de los %d productos "
                                    "— aviso 'no lo tengo'", _ausente, len(prods),
                                )
                                _av = _aviso_no_lo_tengo(_term_ef, _ausente)
                                _clave = titulo.strip().upper()
                                avisos[_clave] = (
                                    f"{avisos[_clave]}\n{_av}" if _clave in avisos else _av
                                )
                            # ¿EL CLIENTE PIDIÓ AGREGARLO? ("le agregas 3 cajas de
                            # glymar de 4 MG"). Solo se añade si (a) lo pidió con un
                            # verbo de agregar, (b) dijo CUÁNTAS unidades y (c) hay UN
                            # ÚNICO candidato — si hay varios, no se adivina: se le
                            # ofrece la lista para que elija.
                            if _tambien_pide_agregar(user_text):
                                _cant = _cantidad_pedida(med)
                                if _cant and len(prods) == 1:
                                    agregables.append((titulo, prods[0], _cant))
                                elif _cant:
                                    logger.info(
                                        "receta: '%s' se pidió agregar x%d pero hay %d "
                                        "candidatos — no se adivina",
                                        med, _cant, len(prods),
                                    )
                        else:
                            logger.info("receta: %s no está en el catálogo", med)
                            no_disponibles.append(titulo)
                        # ¿ESTE GRUPO YA SE LO AGREGAMOS? Si el cliente dijo "quiero la
                        # opción 9" (de esta misma lista) y además pregunta el precio de
                        # otra cosa, no se le repite la lista entera que acaba de ver.
                        # El grupo ya se había añadido arriba: se quita.
                        if grupos and _grupo_ya_en_carrito(grupos[-1][1], runtime):
                            logger.info(
                                "receta: '%s' ya está en el carrito por elección de opción "
                                "— no se repite la lista", grupos[-1][0],
                            )
                            grupos.pop()
                    if grupos:
                        # Guardar la lista GLOBAL de opciones (en el MISMO orden
                        # que ve el cliente: medicamento por medicamento, cada
                        # uno ordenado por precio) para resolver "opción Z" en
                        # el siguiente turno.
                        opciones_global: list[dict[str, Any]] = []
                        for _t, prods in grupos:
                            opciones_global.extend(
                                sorted(
                                    prods,
                                    key=lambda p: (
                                        p.get("precio")
                                        if isinstance(p.get("precio"), (int, float))
                                        else 0
                                    ),
                                )
                            )
                        if opciones_global:
                            runtime.last_options = opciones_global
                            runtime.last_product = opciones_global[0]
                        # ---- AGREGAR LO QUE EL CLIENTE PIDIÓ Y DARLE EL RESUMEN ----
                        #
                        # Caso real (conv 2109, provider 19, 2026-10-17 03:37):
                        #     cliente: "Me puedes dar el resumen de esto y le agregas
                        #               3 cajas de glymar de 4 MG y 2 toallas securezza x 15"
                        # El cliente dio una ORDEN explícita ("le agregas") con cantidad y
                        # producto. Antes se le ofrecía la lista y tenía que volver a
                        # confirmar; y el resumen del carrito no salía en esa respuesta.
                        # Ahora se añaden (solo los que tienen UN único candidato y
                        # cantidad dicha por el cliente) y se responde con el resumen, que
                        # ya los incluye.
                        if agregables:
                            hechos: list[str] = []
                            for _tit, _prod, _cant in agregables:
                                args_add: dict[str, Any] = {
                                    "productId": _prod.get("productId"),
                                    "producto": _prod.get("producto") or "",
                                    "cantidad": _cant,
                                    "presentacion": _prod.get("presentacion") or "",
                                    "laboratorio": _prod.get("laboratorio") or "",
                                }
                                if _prod.get("precio") is not None:
                                    args_add["precioUsd"] = _prod["precio"]
                                if _prod.get("precioBs") is not None:
                                    args_add["precioBs"] = _prod["precioBs"]
                                await runtime.execute("agregar_al_carrito", args_add)
                                logger.info(
                                    "receta: el cliente lo pidió — agrego '%s' x%d",
                                    args_add["producto"], _cant,
                                )
                                hechos.append(f"- {_cant} de {args_add['producto']}")
                            result = await runtime.execute("ver_carrito", {})
                            if runtime.cart_summary_text:
                                receta_final = (
                                    "✅ Agregado al pedido:\n"
                                    + "\n".join(hechos)
                                    + "\n\n"
                                    + runtime.cart_summary_text
                                )
                                await _send(
                                    ctx,
                                    runtime._conv.id,
                                    runtime._crm_conv_id,
                                    _con_confirmacion_carrito(
                                        _titulo_sin_dosis_negada(receta_final), runtime
                                    ),
                                )
                                return None  # turno atendido
                        receta_final = _formatear_receta(grupos, no_disponibles, avisos)
                        # La receta se envía DIRECTO desde aquí, así que no pasa por el
                        # post-proceso de `run_turn`: el encabezado se corrige aquí.
                        receta_final = _titulo_sin_dosis_negada(receta_final)
                        receta_final = _con_confirmacion_carrito(receta_final, runtime)
                        await _send(
                            ctx,
                            runtime._conv.id,
                            runtime._crm_conv_id,
                            receta_final,
                        )
                        return None  # turno atendido: no dejar que el LLM reescriba
                    if not no_disponibles and terminos_descartados:
                        # LA IMAGEN NO DEJÓ NINGÚN MEDICAMENTO QUE BUSCAR.
                        # O el OCR leyó solo la marca ('nombre del medicamento calox') o solo
                        # la sal ('clorhidrato'). Cotizar eso devuelve un catálogo al azar, así
                        # que lo honesto es pedir el nombre tal como está en la caja.
                        # Caso real (provider 32, 2026-10-27): la foto de una FEXOFENADINA
                        # CLORHIDRATO CALOX se respondió con 20 productos de la marca CALOX.
                        logger.info(
                            "receta: la imagen no dejó ningún medicamento buscable "
                            "(descartados: %s) — pido el nombre", terminos_descartados,
                        )
                        await _send(
                            ctx,
                            runtime._conv.id,
                            runtime._crm_conv_id,
                            "No consigo leer el nombre del medicamento en la imagen. 🙏\n"
                            "¿Me lo escribes como aparece en la caja? "
                            "(por ejemplo: FEXOFENADINA CLORHIDRATO 120 MG)",
                        )
                        return None  # turno atendido
                ocr_term = _extraer_termino_ocr(user_text)
                # Guardar el término del OCR para el turno SIGUIENTE: el cliente
                # suele preguntar después "¿el producto de la foto lo tienes?"
                # sin repetir el nombre, y buscar "foto" devuelve FOTORRETIN.
                if ocr_term:
                    runtime.last_ocr_term = ocr_term
                if (
                    ocr_term
                    and not runtime.catalog_retried
                    and (not runtime.consulted_catalog or not runtime.med_not_found)
                ):
                    runtime.catalog_retried = True
                    logger.info(
                        "backstop OCR: forzando buscar_medicamento('%s')", ocr_term,
                    )
                    result = await runtime.execute("buscar_medicamento", {"nombre": ocr_term})
                    messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "bkp_ocr",
                                    "type": "function",
                                    "function": {
                                        "name": "buscar_medicamento",
                                        "arguments": json.dumps(
                                            {"nombre": ocr_term}, ensure_ascii=False
                                        ),
                                    },
                                }
                            ],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": "bkp_ocr",
                            "content": json.dumps(
                                result, ensure_ascii=False, default=str
                            ),
                        }
                    )
                    continue
                # Transcripción de nota de voz/audio: extraer el medicamento y
                # forzar la consulta (igual que el OCR, para que el LLM no
                # busque el marcador completo con ruido y niegue disponibilidad).
                trans_term = _extraer_termino_transcripcion(user_text)
                if (
                    trans_term
                    and not runtime.catalog_retried
                    and (not runtime.consulted_catalog or not runtime.med_not_found)
                ):
                    runtime.catalog_retried = True
                    logger.info(
                        "backstop transcripción: forzando buscar_medicamento('%s')",
                        trans_term,
                    )
                    result = await runtime.execute(
                        "buscar_medicamento", {"nombre": trans_term}
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "bkp_voz",
                                    "type": "function",
                                    "function": {
                                        "name": "buscar_medicamento",
                                        "arguments": json.dumps(
                                            {"nombre": trans_term}, ensure_ascii=False
                                        ),
                                    },
                                }
                            ],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": "bkp_voz",
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    continue
                # Búsqueda por texto del cliente (no OCR).
                term = None
                # Si es una nota de voz/audio, el término se extrae de la
                # TRANSCRIPCIÓN (entre comillas), NUNCA del marcador completo
                # del sistema ("Es una CONSULTA del lead: interpreta la
                # transcripción, extrae el/los medicamento(s)..."). Usar el
                # marcador completo como término devolvía 20 productos
                # irrelevantes (DOL, ALCOHOL, GOTAS DEL CARMEN...).
                trans_term_2 = _extraer_termino_transcripcion(user_text)
                if trans_term_2:
                    term = trans_term_2
                elif _parece_consulta_medicamento(user_text) and not _lead_esta_cerrando(
                    _texto_cliente_sin_marcadores(user_text)
                ):
                    term = _extraer_termino_medicamento(user_text)
                # EL TÍTULO TIENE QUE SER EL FÁRMACO, NO LA FRASE. `_extraer_termino_medicamento`
                # quita verbos de consulta y relleno, pero NO el ruido del HABLA (muletillas,
                # coletillas) — eso solo lo hace `_limpiar_transcripcion`. Con una nota de voz
                # el término conservaba la frase y el ENCABEZADO de la respuesta era:
                #     EN QUÉ LA VENDA SOL? LA CAJA TRAE DOS
                # (caso real conv 2834). El cliente lee eso como el nombre del producto.
                # Se vuelve a pasar por el limpiador de audio para que quede 'venda'.
                if term and _texto_transcripcion_completo(user_text):
                    term_audio = _extraer_termino_medicamento(_limpiar_transcripcion(term))
                    if term_audio:
                        term = term_audio
                # El cliente REFERENCIA una imagen anterior ("el producto de la
                # foto lo tienes?") sin aportar un fármaco. Buscar con las
                # palabras de la pregunta devuelve basura por SUBSTRING: 'foto'
                # matchea 'FOTORRETIN' (oftálmico) y el agente responde "sí,
                # tengo el producto de la foto" mostrando ese oftálmico. Se usa
                # el término del OCR de la imagen que el cliente SÍ mandó.
                if _parece_referencia_sin_farmaco(user_text):
                    anterior = runtime.last_ocr_term or runtime.prev_last_ocr_term
                    if anterior:
                        logger.info(
                            "referencia a imagen: buscando con el OCR previo '%s' "
                            "(en vez de con las palabras de la pregunta)",
                            anterior,
                        )
                        term = anterior
                    else:
                        # Referencia a una imagen que no tenemos: no se busca
                        # con basura. El LLM responde honesto.
                        logger.info(
                            "referencia a imagen sin OCR previo: no se fuerza búsqueda",
                        )
                        term = None
                # Forzar búsqueda si:
                # 1. El LLM no consultó el catálogo (not consulted_catalog)
                # 2. O consultó pero no encontró nada (med_not_found) — quizás
                #    usó un término distinto al deterministicamente correcto.
                #    Re-consultar con el término extraído puede encontrar productos.
                if term and (not runtime.consulted_catalog or (runtime.med_not_found and not runtime.catalog_retried)):
                    # Nunca forzar la búsqueda con una frase que NO parece un
                    # medicamento (reclamos, garantías, saludos, "caja cada
                    # uno"): consultar el catálogo con basura devuelve
                    # productos irrelevantes que el backstop de contradicción
                    # le muestra al cliente (GORRO DE ENFERMERA ante un
                    # reclamo). Se deja que el LLM maneje el turno normal.
                    if not _termino_es_medicamento_plausible(term):
                        logger.info(
                            "backstop: término '%s' no parece medicamento — no fuerzo búsqueda",
                            term,
                        )
                        term = None
                    else:
                        runtime.catalog_retried = True
                        logger.info(
                            "backstop: forzando buscar_medicamento('%s') — consulted=%s med_not_found=%s",
                            term, runtime.consulted_catalog, runtime.med_not_found,
                        )
                        result = await runtime.execute("buscar_medicamento", {"nombre": term})
                        messages.append(
                            {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "bkp",
                                        "type": "function",
                                        "function": {
                                            "name": "buscar_medicamento",
                                            "arguments": json.dumps(
                                                {"nombre": term}, ensure_ascii=False
                                            ),
                                        },
                                    }
                                ],
                            }
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": "bkp",
                                "content": json.dumps(
                                    result, ensure_ascii=False, default=str
                                ),
                            }
                        )
                        continue  # nueva ronda del LLM, ahora con datos del catálogo
            return reply.content  # turno de puro texto
        # content vacío con tool_calls es normal (turno solo-herramientas)
        # Pero si TODAS las tool-calls vienen con arguments vacíos ({}), es un
        # bucle degenerado del LLM: no avanzan y queman rondas en silencio.
        all_empty = reply.tool_calls and all(not tc.arguments for tc in reply.tool_calls)
        if all_empty:
            empty_rounds += 1
            if empty_rounds >= 3:
                logger.warning(
                    "turno: %d rondas seguidas con tool-calls sin arguments — corto con respaldo",
                    empty_rounds,
                )
                return _fallback_farmacia(user_text, runtime, ctx, farmacia)
        else:
            empty_rounds = 0
        messages.append(
            {
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in reply.tool_calls
                ],
            }
        )
        for tc in reply.tool_calls:
            # GUARD DEL PASO DE ENTREGA: no se puede mostrar el RESUMEN sin haber
            # preguntado antes cómo quiere recibir el pedido. Igual que el guard de
            # finalizar, va en el DESPACHO de tools porque el modelo puede llamar
            # ver_carrito por su cuenta y saltarse la pregunta — que es exactamente
            # lo que hace cuando el cliente dice "no" (medido: el modelo responde
            # "Perfecto, cuando estés listo puedo ayudarte a finalizar" sin tool, o
            # llama finalizar_pedido/ver_carrito directamente).
            #
            # Se intercepta antes de ejecutar: se envía el menú 1/2 y el turno
            # termina ahí. El resumen se mostrará en el turno siguiente, ya con el
            # método elegido (y la dirección si es delivery).
            if (
                farmacia
                and tc.name == "ver_carrito"
                and runtime._conv.delivery_method is None
                and not runtime._conv.delivery_pending
                and not runtime.delivery_pregunta_enviada
            ):
                items_e = await ctx.store.cart_items(
                    runtime._conv.id, session_hours=ctx.settings.cart_session_hours
                )
                if items_e:
                    logger.info(
                        "guard entrega: el LLM iba a mostrar el resumen sin preguntar "
                        "la entrega (%d producto(s)) — pregunto delivery/retiro",
                        len(items_e),
                    )
                    await _preguntar_metodo_entrega(ctx, runtime)
                    return None
            # GUARD: NO SE PUEDE FINALIZAR SIN HABER MOSTRADO EL RESUMEN.
            #
            # El cliente debe ver nombre, cajas y subtotal de cada medicamento ANTES
            # de que el pedido quede registrado. Si el LLM llama finalizar_pedido de
            # una vez, el cliente recibe solo "Tu pedido ha sido registrado. Total:
            # $1.80" — sin detalle — y el pedido ya se cerró (cart_clear), así que el
            # resumen es IMPOSIBLE de recuperar después.
            #
            # Caso real (provider 05, 2026-10): el agente preguntó "¿Deseas buscar otro
            # medicamento? (SI/NO)", el cliente dijo "No" y el LLM llamó
            # finalizar_pedido directamente (medido 5/5 con el modelo real). El
            # backstop que fuerza el resumen vive en el bloque `if not reply.tool_calls`,
            # así que NUNCA se evaluó: el modelo llamó una tool y se saltó todos los
            # backstops. El cliente vio su pedido registrado sin ver qué compró.
            #
            # Se sustituye la llamada por el resumen y se le da otra ronda al LLM para
            # que lo presente; el cliente confirma en el turno siguiente y ahí sí
            # finaliza (con `cart_summary_shown` ya activo).
            if (
                farmacia
                and tc.name == "finalizar_pedido"
                and not runtime.summary_forced
                and (
                    not runtime._conv.cart_summary_shown
                    or runtime._conv.delivery_method is None
                )
            ):
                items_cart = await ctx.store.cart_items(
                    runtime._conv.id, session_hours=ctx.settings.cart_session_hours
                )
                # Solo se exige el resumen si HAY algo que resumir: sin carrito,
                # finalizar_pedido devuelve 'carrito_vacio' y no hay nada que mostrar.
                if items_cart:
                    # Si el cliente aún no eligió CÓMO recibirlo, primero el paso de
                    # entrega. Un pedido sin método de entrega deja al humano sin saber
                    # si despachar o preparar para retiro.
                    if runtime._conv.delivery_method is None:
                        logger.info(
                            "guard finalizar: sin método de entrega — pregunto "
                            "delivery/retiro antes de cerrar"
                        )
                        await _preguntar_metodo_entrega(ctx, runtime)
                        return None
                    runtime.summary_forced = True
                    logger.info(
                        "guard finalizar: el LLM quiso finalizar sin mostrar el resumen "
                        "(%d producto(s)) — fuerzo ver_carrito antes de cerrar",
                        len(items_cart),
                    )
                    result = await runtime.execute("ver_carrito", {})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                    continue
            result = await runtime.execute(tc.name, tc.arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
    # Si el cliente describía un producto sin nombrarlo, el silencio es lo peor que puede
    # pasar: se manda la pregunta canónica.
    if getattr(runtime, "pide_recomendacion", False):
        logger.warning(
            "turno: demasiadas rondas y el cliente describía el producto sin nombrarlo — "
            "se manda la pregunta canónica en vez de silencio"
        )
        return PREGUNTA_RECOMENDACION
    logger.warning("turno: demasiadas rondas de herramientas — corto sin texto")
    return None

SEND_ATTEMPTS = 4  # backoff 1 s, 2 s, 4 s entre intentos (~7 s en el turno)


async def _send(ctx: AppContext, conv_id: int, crm_conv_id: str, text: str) -> bool:
    """Envía vía el CRM. Si el turno agota sus reintentos, la respuesta NO se
    descarta: se encola en pending_send y el SenderWorker la reintenta con
    backoff hasta entregar o agotar 24 h (incidente 2026-08-03).

    WhatsApp no acepta mensajes de más de 4096 caracteres (el CRM responde
    422 y la respuesta se perdería — incidente receta de 8 medicamentos,
    4831 chars). Se divide en partes por línea vacía (párrafos) SIN cortar
    opciones de la lista por la mitad; cada parte ≤ WA_MAX_CHARS.
    """
    partes = _partir_mensaje_largo(a_formato_whatsapp(text), WA_MAX_CHARS)
    # Modo laboratorio (endpoint /api/chat): captura las respuestas en el
    # outbox en vez de enviarlas por WhatsApp. No reenvía, no encola
    # pending_send, no toca la ventana ni la API.
    if ctx.lab_outbox is not None:
        ctx.lab_outbox.extend(partes)
        return True
    ok_todas = True
    for i, parte in enumerate(partes):
        enviado = False
        for attempt in range(SEND_ATTEMPTS):
            try:
                await ctx.crm.send_message(crm_conv_id, parte)
                enviado = True
                break
            except CrmConflict as exc:
                # ai_paused / window_closed: silencio respetuoso, sin reintento.
                logger.info("envío bloqueado por el CRM (%s) — silencio", exc.code)
                return False
            except CrmError as exc:
                logger.warning(
                    "envío falló (parte %d/%d, intento %d): %s",
                    i + 1, len(partes), attempt + 1, exc,
                )
                if attempt < SEND_ATTEMPTS - 1:
                    await asyncio.sleep(2.0**attempt)
        if not enviado:
            ok_todas = False
            pending_id = await ctx.store.enqueue_pending_send(conv_id, crm_conv_id, parte)
            logger.error(
                "envío agotó reintentos — parte %d/%d encolada como pending_send %d",
                i + 1, len(partes), pending_id,
            )
    return ok_todas


async def _safe_handoff(ctx: AppContext, crm_conv_id: str, reason: str) -> None:
    try:
        await ctx.crm.post_handoff(crm_conv_id, reason)
        logger.info("handoff registrado en el CRM (reason=%s)", reason)
    except CrmError as exc:
        logger.error("no pude registrar el handoff (%s): %s", reason, exc)


# ------------------------------------------------------------- backstop ---
# Anti-alucinación (farmacia): detecta cuándo el cliente pregunta por un
# medicamento para que, si el LLM responde SIN consultar el catálogo, forcemos
# la consulta y el modelo conteste con datos reales, nunca inventados.

_VERBOS_MEDICAMENTO = re.compile(
    # VERBOS DE PASE A HUMANO: pedir el pase no es pedir un medicamento. Sin esto,
    # "Puedes por favor pasarlo al equipo" buscaba 'pasarlo' en el catálogo.
    r"\b(pasar|pasarlo|pasarla|pasarlos|pasarlas|pasame|pásame|paseme|páseme|pasa|"
    r"pasen|transferir|transferirlo|derivar|derivarlo|derivalo|derivelo|escalar|"
    r"escalalo|escalelo|atender|"
    r"atiendelo|atiéndelo|tienen|tenéis|hay|consigo|me dan|me consigues|tienes|busco|buscando|"
    r"buscar|buscas|necesito|quisiera|quiero|quería|queremos|saber|"
    r"venden|vendes|disponible|disponibles|disponen|disponemos|cuesta|cuestan|precio|"
    r"pueden conseguir|traen|consigues|conseguir|tengo|tiene|hay|mande|"
    r"dime|digan|preguntar|pregunto|estaba|estaban|estuve|andaba)\b",
    re.IGNORECASE,
)

# Palabras de relleno (saludos, cortesía, muletillas) que NUNCA son un
# medicamento. Impide que el backstop busque "hola" o "buenas".
# VOCATIVO / CARIÑO: el cliente saluda y llama al agente "mi amor", "corazón", "cariño".
# Caso real (provider 32, 2026-10-29): "Buenas tardes!! Mi amor que precio tiene la
# nitrofurantoina de 50mg y la vitamina C" → el agente buscó 'AMOR NITROFURANTOINA 50MG',
# avisó "No tengo NITROFURANTOINA. De AMOR 50MG hay esto" y cotizó TE AMO PRESERV. 3PCS.
VOCATIVOS_T = {
    "amor", "cariño", "carino", "corazon", "corazón", "bebe", "bebé", "preciosa",
    "precioso", "linda", "lindo", "hermosa", "hermoso", "bonita", "bonito",
    "principe", "príncipe", "reina", "rey", "amiga", "amigo", "senora", "señora",
    "senor", "señor", "doctor", "doctora", "licenciada", "licenciado", "jefe", "jefa",
    "vecina", "vecino", "pana", "panita", "compatriota",
}
# INTENCIÓN DE COMPRA: no es un medicamento.
INTENCION_COMPRA_T = {
    "quiero", "queria", "quería", "quisiera", "querer", "querría", "querria",
    "deseo", "deseamos", "llevo", "llevare", "llevaré", "llevaria", "llevaría",
    "apartame", "apártame", "separame", "sepárame", "encargame", "encárgame",
}
# REFERENCIA A LA LISTA ANTERIOR: 'de la opción 9', 'la número 3'.
REFERENCIA_LISTA_T = {
    "opcion", "opción", "opciones", "opcioncita", "numerito",
}

# «¿SOLO LO TIENES POR SEPARADO?» — es una PREGUNTA sobre la respuesta anterior, no un
# producto. Caso real (provider 32, 2026-10-29): tras cotizar la caja de FIN-AL-GRIP FORTE
# como si fueran cuatro medicamentos, el cliente preguntó "No entendí?? Solo lo tienes por
# separado?" y el agente contestó "⚠️ No disponibles en el catálogo: SEPARADO" con un
# ENTERUNI al lado.
POR_SEPARADO_T = {
    "separado", "separada", "separados", "separadas", "suelto", "suelta", "sueltos",
    "sueltas", "individual", "individuales", "independiente", "independientes", "aparte",
}

# «NO ENTENDÍ» — el cliente pide una ACLARACIÓN, no un medicamento. Sin esto, "No entendí??
# Solo lo tienes por separado?" se buscaba como 'ENTENDI SEPARADO' (provider 32, 2026-10-29).
COMPRENSION_T = {
    "entendi", "entiendo", "entender", "entendido", "entendiste", "entendieron",
    "comprendi", "comprendo", "comprender", "comprendido", "comprende",
    "repite", "repites", "repetir", "repitas", "reitero", "reiteras",
    "explicame", "explicar", "explicas", "explicacion", "aclarame", "aclarar", "aclaracion",
    "confundi", "confundido", "confundida", "duda", "dudas",
}

# FELICITACIONES / DESPEDIDA DE BUENAS NOCHES: "Feliz noche, ¿el precio de la buscapina?"
# no es un medicamento llamado "FELIZ NOCHE BUSCAPINA" (provider 32, 2026-10-29).
FELICITACION_T = {
    "feliz", "felicidad", "felices", "abrazo", "abrazos", "beso", "besitos", "besos",
    "bendiciones", "bendicion", "próspero", "prospero", "prospera", "éxito", "exito",
    "exitos", "excelente", "excelentes", "lindas", "hermosos", "bonita", "bonito",
}

# SALUDOS DE TIEMPO EN SINGULAR: "buenas noches" ya estaba, el 'noche' suelto y
# 'feliz noche' no. Son la misma muletilla (provider 32, 2026-10-29).
SALUDO_TIEMPO_T = {
    "noche", "tarde", "dia", "día", "manana", "mañana", "madrugada",
    "nochecita", "medio dia", "medio dia",
}

# BENDICIONES_T AL SALUDAR: "Hola buen día, Dios le bendiga" no es una línea de productos
# (provider 32, 2026-10-30: el agente cotizó COLLAR DIOS / COLLAR PERLA FE CORAZON DIOS
# para una receta de 5 medicamentos que venía en la foto).
BENDICIONES_T = {
    "dios", "diosito", "bendiga", "bendice", "bendecir", "bendecida", "bendecido",
    "bendecidos", "jesus", "jesús", "cristo", "virgen", "amen", "amén", "salmo",
    "oracion", "oración", "bendicion", "bendiciones",
}

# «ME DAN EL PRESUPUESTO DE ESTOS MEDICAMENTOS»: el verbo de la petición y el
# demostrativo no son el nombre de nada. Sin esto quedaba 'dad estos' como término
# (provider 32, 2026-10-30).
DEMOSTRATIVOS_T = {
    "dad", "dan", "dame", "danos", "da", "den", "denme", "deme", "darme", "darnos",
    "estos", "estas", "este", "esta", "esto", "esos", "esas", "eso", "aquel",
    "aquellos", "aquellas", "aquella", "dichos", "dichas", "mencionados",
    "mencionadas", "indicados", "indicadas", "referidos", "referidas", "antelacion",
    "antelación", "sus", "mis",
}

_FILLER = {
    "hola", "buenas", "buen", "buenos", "buena", "dia", "dias", "tardes",
    "noches", "gracias", "por", "favor", "quisiera", "podria", "puede",
    # FRASE DE CONSULTA: el cliente ANUNCIA la lista antes de enumerarla
    # ("por favor me podrías cotizar los siguientes medicamentos. Glimepiride...,
    # candesatan..., ..."). Sin filtrarlas, esa frase se buscaba como si fuera UN
    # medicamento y el cliente veía "⚠️ No disponibles: COTIZAR SIGUIENTES MEDICAMENTOS".
    # MEDIDO en los 7 providers activos: 0 productos reales. Los 4 aciertos de 'cotizar'
    # en el 27 eran CORTA UÑA/CORTAUÑA y los 3 de 'presupuesto' en el 28 eran
    # PRESTOBARBA: falsos positivos del matcher difuso, o sea el bug que se quiere evitar.
    "cotizar", "cotizame", "cotizacion", "cotizaciones", "cotizarlos",
    # Vocativo, intención de compra y referencia a la lista (gemelos de tools).
    *VOCATIVOS_T, *INTENCION_COMPRA_T, *REFERENCIA_LISTA_T,
    *POR_SEPARADO_T,
    *COMPRENSION_T,
    *FELICITACION_T,
    *SALUDO_TIEMPO_T,
    *BENDICIONES_T,
    *DEMOSTRATIVOS_T,
    "siguiente", "siguientes", "medicamento", "medicamentos", "presupuesto",
    # RÓTULOS DEL CAMPO: el OCR devuelve '**Nombre del medicamento:** Fexofenadina
    # Clorhidrato CALOX' y el término quedaba 'nombre del fexofenadina clorhidrato calox'.
    # Con solo la marca ('nombre del medicamento calox') el término era 'nombre del calox' y
    # el detector de laboratorio no lo reconocía (veía tres palabras). Son TRES listas
    # duplicadas: aquí, en tools._PALABRAS_FUNCIONALES y en relevancia._FUNCIONALES — hay
    # que tocar las tres. Caso real provider 32, 2026-10-27.
    "nombre", "nombres", "rotulo", "etiqueta", "descripcion", "presentacion",
    # ACCESORIOS Y CONSUMIBLES DE UN EQUIPO, NO MEDICAMENTOS. Caso real (provider 32,
    # 2026-10-27): "¿el Glucómetro HLM-78 viene con las plaquitas, el lápiz y la pulla o
    # solo el aparato?" → el troceador sacó ['lapiz', 'pulla aparato'] y el cliente recibió
    # 20 LÁPICES de cejas y labiales, más "no disponibles: PULLA APARATO". Ninguna de estas
    # palabras identifica un fármaco por sí sola.
    # OJO: 'tiras' y 'lancetas' NO van aquí — se venden sueltas y son productos reales.
    "lapiz", "lapices", "lapicero", "lapiceros", "pulla", "pullas",
    # 'tratamiento' no es un producto: el catálogo está lleno de TRATAMIENTO CAPILAR.
    "tratamiento", "tratamientos", "tratamientos capilares",
    # CANTIDADES: 'las tres', 'los dos', 'quiero tres'. NO son medicamentos y el matcher
    # difuso los apoya en cualquier cosa (caso real: 'tres' → LITTLE TREES → 15
    # ambientadores cotizados como medicamentos, provider 32).
    "uno", "una", "unos", "unas", "dos", "tres", "cuatro", "cinco", "seis", "siete",
    "ocho", "nueve", "diez", "once", "doce", "quince", "veinte", "ambos", "ambas",
    "par", "pares", "trio", "docena", "mitad", "todas", "todos",
    # "TIENEN A LA VENTA …" es la forma más común de preguntar si vendes algo: 'venta'
    # no forma parte del nombre y descoloca al matcher (provider 32, 2026-10-29:
    # 'venta aceite oregano' devolvía 5 productos de otra cosa).
    "venta", "ventas", "vende", "vendes", "vendia", "vendían",
    # 'todo pediátrico' del final de una lista no es parte del nombre del último
    # medicamento (encabezado real: "CEFIXIMA SUSP TODO PEDIATRICO"), y 'todo' por sí solo
    # es lo que metía DESODORANTE DOVE en una consulta conversacional.
    "todo", "toda",
    # EL EQUIPO / EL PERSONAL: pedir el pase no nombra un producto ("transferir a un
    # asesor" dejaba 'asesor' como término de búsqueda).
    "asesor", "asesora", "supervisor", "encargado", "encargada", "staff",
    # ADVERBIOS/CUANTIFICADORES del habla: 'gracias, muy amable' dejaba el término 'muy'
    # y el matcher difuso lo apoyaba en cualquier cosa.
    "muy", "tan", "bastante", "demasiado", "algo", "mucho", "mucha", "muchos", "muchas",
    "poco", "poca", "pocos", "pocas", "tambien", "también", "solo", "solamente",
    # FOTO/IMAGEN: pedir una foto no es pedir un producto. Buscar 'fotos' en el catálogo
    # devolvía cualquier cosa (caso real provider 32: "Me puede enviar unas fotos" →
    # "Actualmente no tenemos linternas"). Hay guard propio (`_pide_foto`).
    "foto", "fotos", "fotografia", "fotografias", "imagen", "imagenes", "selfie",
    "plaquita", "plaquitas", "tirita", "tiritas",
    "aparato", "aparatos", "equipo", "maquina", "maquinita",
    "estuche", "maletin", "cargador", "cable", "bateria", "baterias",
    "adaptador", "funda", "repuesto", "repuestos",

    # 'resumen' NO es un medicamento. En "dame el resumen de esto y le agregas 3 cajas
    # de glymar" el troceador lo tomaba como un producto más y el cliente leía
    # "⚠️ No disponibles en el catálogo: RESUMEN".
    "resumen", "resumenes", "resúmenes",
    # 'costo'/'porf' NO son medicamentos. Caso real (conv 2109, provider 19):
    # "tiene disponible crema dental sensodyne y que costo porf" → el troceador partía ahí y
    # cotizaba "COSTO PORF" con AMPICILINA 1GR (POR 1 AMPOLLA) y ANTILAX (POR 10 TABLETAS):
    # 'porf' (por favor) casa con el "POR" de los nombres. Y 'costo' daba 0 productos, o sea
    # tampoco es un medicamento. MEDIDO: 'costo' → 0; 'porf' → 2 (ambos por el "POR").
    "porf", "porfa", "porfavor", "porfis", "favorcito",
    "costo", "costos", "costar", "costaria", "costarían", "costaban",
    # ENVASE NO ES EL MEDICAMENTO. "quiero una caja de eszoz" se convertía en el
    # término 'caja eszoz' y el cliente leía
    #     ⚠️ No disponibles en el catálogo: CAJA ESZOZ
    # — el envase presentado como si fuera el nombre del fármaco. Caso real (conv 7,
    # provider 05): "quiero una caja de eszoz, leprit, bumetin, evigax 125 mg, milax
    # 120 gr, bargonil 10 gr". `_PALABRAS_FUNCIONALES` de tools.py YA tenía 'caja' y
    # 'cajas'; esta lista es su gemela y no las tenía, así que el camino de
    # `_extraer_termino_medicamento` (que mira ESTA) las dejaba pasar.
    # Ojo: la cantidad y el envase se siguen leyendo del texto CRUDO en
    # `_pedido_de_envase` (que no usa `_FILLER`), así que quitarlas aquí no pierde
    # ni "3 cajas" ni el filtro de envase.
    "caja", "cajas", "cajita", "cajitas", "blister", "blisters",
    "frasco", "frascos", "pomo", "pomos", "paquete", "paquetes",
    "me", "le", "la", "de", "el", "los", "las", "para", "que", "con",
    "una", "un", "en", "y", "o", "a", "si", "no", "como", "cuanto", "es",
    "son", "tiene", "tienen", "hay", "está", "estan", "disponible",
    "disponibles", "precio", "cuesta", "cuestan", "venden", "necesito",
    # Formas del verbo PODER y otras conjugaciones que faltaban: la lista tenía
    # 'puede'/'pueden'/'podrias' pero NO 'puedes', así que encabezaba la respuesta.
    # Caso real (provider 27): "Buenas tardes, puedes darme precio de las gotas
    # oftalmicas Clarasol." → el título salía con el verbo delante.
    "puedes", "podrian", "podríamos", "podriamos", "se podra", "se podría",
    "tienes", "tiene", "tienen", "tengo", "quisieras", "quieres", "quiere",
    "quisiera", "necesitas", "necesita", "buscas", "busca", "buscamos",
    "busco", "buscando", "buscar", "buscas", "quiero", "quisiera", "consigo",
    "pueden", "consigues", "conseguir", "tengo", "tambien", "algo", "otro",
    "otra", "mas", "más", "cual", "cuales", "donde", "cuando", "quien",
    "esto", "este", "esta", "eso", "esa", "aquello", "estoy", "soy",
    "nada", "nadie", "solo", "solamente", "también", "ahi", "aqui",
    # Cortesía, negación y despedida. NUNCA son parte del nombre del fármaco, y
    # dejarlas en el término contamina la búsqueda: medido contra el catálogo real
    # del provider 19, 'disculpe atamel forte' devolvía 10 productos con ruido
    # (MULTIVITAMINICO VITAMIX FORTE, BREXIN FORTE) mientras 'atamel forte'
    # devolvía 1, el correcto (ATAMEL FORTE 650 MG). El matcher es AND sobre los
    # tokens, así que un token de cortesía arrastra productos irrelevantes.
    "disculpe", "disculpa", "disculpen", "perdone", "perdon", "lamento",
    "molestia", "siento", "regalo", "regala", "regalas", "obsequio",
    "voy", "vas", "vamos", "compro", "comprar", "comprare", "deseo",
    "interesa", "interesada", "interesado", "olvidalo", "dejalo", "adios",
    "chao", "luego", "vemos", "bendiciones", "amable", "atentamente",
    # Verbos de CONSULTA que el cliente escribe antes del medicamento y que no
    # estaban: 'Ok me INDICA el precio del fulgran' → el término salía
    # 'indica del fulgran' y el TÍTULO de la respuesta era "INDICA DEL FULGRAN",
    # presentado al cliente como si fuera el nombre del producto (conv 2826).
    # Son vocabulario del motivo, nunca parte de un nombre comercial: se midió
    # contra el catálogo real del provider 27 y ninguna aparece en nombres.
    "indica", "indico", "indique", "indicas", "indiquen", "indícame", "indicame",
    "menciona", "mencioname", "mencione", "cotiza", "cotizame", "cotizacion",
    "averigua", "averiguame", "averiguar", "consultar", "consulta", "consulto",
    "sabes", "sabe", "dice", "decir", "decirme", "saberme", "confirmame",
    # FORMAS VERBALES CON PRONOMBRE (enclítico). La lista tenía el infinitivo
    # ('dar') y la forma imperativa ('dame') no, así que el verbo ENCABEZABA la
    # respuesta: caso real (provider 27)
    #     "Buenas tardes, puedes darme precio de las gotas oftalmicas Clarasol."
    #     → título "DARME GOTAS OFTALMICAS CLARASOL"
    # Medido contra el catálogo: ninguna de estas formas aparece dentro de un nombre
    # de producto. OJO: 'da' NO se añade — aparece dentro de 'BOCADILLO DE GUAYABA'
    # y el matcher del catálogo es SUBSTRING.
    "darme", "darte", "darnos", "darme", "damelo", "damela", "damelos", "damelas",
    "dame", "damele", "regalame", "regalamelo", "regalas", "regale", "manejas",
    "maneja", "consigues", "consigue", "consigueme", "podrias", "podriame",
    # 'dar' y sus formas: MEDIDO — no estaban en _FILLER, _FILLER_INICIAL ni
    # _VERBOS_MEDICAMENTO, así que encabezaban el título del medicamento. Caso real (conv 2109,
    # provider 19): "me puedes dar el precio del beprospem" →
    #     _extraer_termino_medicamento = 'dar del beprospem'  → el cliente veía
    #     "DAR DEL BEPROSPEM" sobre los productos BEPROSPEN. Sin 'dar' queda 'beprospem'.
    # (La nota vieja del proyecto decía que 'dar' ya estaba filtrado; era falsa: solo estaba
    # en _PALABRAS_FUNCIONALES, que usa otro guard y no limpia el título.)
    "dar", "darme", "darte", "darnos", "darle", "denme", "den", "daria", "darias",
    # VERBOS DE ENTREGA/RESPUESTA: el cliente que CIERRA dice "ya me diste el precio".
    # Caso real (conv 2109, provider 19): "Ya me diste el precio, gracias" →
    #     _extraer_termino_medicamento = 'diste'   ← ¡un verbo como término de búsqueda!
    #     y el catálogo devolvió 1 producto por búsqueda DIFUSA; el agente inventó
    #     "Asumí que buscas DISCOLAYTE" y le mostró COLAYTE SOBRE X 69,7 GR DISCOLAYTE.
    # Sin estos verbos, el mensaje se queda sin término y NO se fuerza ninguna búsqueda.
    # MEDIDO: ninguno aparece como token en un nombre de producto del catálogo.
    "diste", "distes", "dio", "dieron", "entregaste", "entrego", "entregaron",
    "mandaste", "mando", "mandaron", "enviaste", "envio", "enviaron",
    "respondiste", "respondio", "contestaste", "contesto",
    "indicaste", "indico", "dijiste", "dijo", "dijeron", "comentaste", "comento",
    # FUTURO/CONDICIONAL con que el cliente PREGUNTA ('¿Será que tienen X?',
    # '¿Podrá darme X?', '¿Habrá X?'). Caso real (conv 2287, provider 19):
    #     'Será que tienen vitamina e  de vivas' → el término salía 'será vitamina vivas',
    #     que da 0 resultados; sin el 'será' es 'vitamina vivas', que SÍ resuelve a
    #     'VIT E 400 MG X 15 CAPS BLANDAS VIVAX' (el producto que pedía).
    # Medido contra el catálogo real: NINGUNA de estas formas aparece como token en un nombre
    # de producto, así que filtrarlas no borra ningún término legítimo.
    "sera", "será", "seran", "serán", "seria", "sería", "serian", "serían",
    "podra", "podrá", "podran", "podrán", "podria", "podría", "podrian", "podrían",
    "habra", "habrá", "habran", "habrán", "tendra", "tendrá", "tendran", "tendrán",
    "estara", "estará", "estaran", "estarán", "abran", "abrÁ", "abrá",
    "quisiera", "querría", "quiza", "quizá", "quizas", "quizás", "acaso",
}

# Preposiciones y artículos que pueden QUEDAR AL PRINCIPIO del término cuando el cliente
# escribe "el precio DEL fulgran": el recorte deja 'del fulgran'. Se quitan SOLO al
# inicio — nunca en medio, porque 'del' aparece DENTRO de nombres reales del catálogo
# ('JUGO DEL VALLE', 'MIOVIT VITAMINAS DEL COMPLEJO B'). Medido: 3 productos reales lo
# contienen en medio y NINGUNO empieza por él, así que quitarlo al principio no puede
# borrar el término de una búsqueda legítima.
_FILLER_INICIAL = {"del", "de", "la", "el", "los", "las", "un", "una", "al"}

# Unidades de medida / presentación: cuando el usuario responde con una
# CANTIDAD (p. ej. "2 cajas", "3 unidades", "1 blíster"), NO está buscando un
# medicamento nuevo; está respondiendo la pregunta del agente. El backstop debe
# dejar de forzar buscar_medicamento para que el LLM llame agregar_al_carrito.
_UNIDADES_MEDIDA = re.compile(
    r"\b(caja|cajas|unidad|unidades|blister|blíster|tabs|tabletas|tableta|"
    r"comprimidos|comprimido|ampollas|ampolla|frasco|frascos|tubo|tubos|"
    r"unidades|piezas|pieza|pack|sobre|sobres|grageas|gragea|cápsulas|capsulas)\b",
    re.IGNORECASE,
)


def _opciones_no_encajan(
    elecciones: list[tuple[int, int]] | None, opciones: list[dict[str, Any]] | None
) -> bool:
    """El cliente cita números de opción que NO existen en la lista actual.

    Cuando pasa, el cliente está leyendo OTRA lista —una anterior, o la de su pantalla—
    y resolver por número es puro azar: las opciones que caen dentro del rango se añaden
    (a un producto equivocado) y las demás se descartan en silencio.

    Caso real (conv 2109, provider 19, 2026-10-17 03:33):
        cliente: "X 1 caja opción 6, x 3 opción 12, x 1 opción 16, x 1 opción 18,
                  x 1 opción 23, x 1 opción 27"
        agente : "He agregado al carrito: 1 caja de COLAYTE SOBRE X 69,7 GR DISCOLAYTE"
    El cliente citaba la lista de 29 opciones que tenía delante; `last_options` guardaba
    una de **7**. La opción 6 cabía en las dos, y esa casualidad metió un COLAYTE (de una
    sesión del 9-oct) en el carrito. El cliente respondió "yo no pedí el colayte".

    Ante la duda NO se añade nada: mejor pedirle que repita los números que meterle un
    producto que no pidió.
    """
    if not elecciones:
        return False
    if not opciones:
        return True
    return max(op for _, op in elecciones) > len(opciones)


MENSAJE_OPCIONES_NO_ENCAJAN = (
    "⚠️ Ojo: esas opciones no coinciden con la última lista que te envié.\n\n"
    "¿Me repites los números, o mejor me dices el nombre del medicamento y lo busco?"
)


def _extraer_eleccion_multiple(texto: str) -> list[tuple[int, int]] | None:
    """Detecta la selección de opciones por número, con cantidades por grupo.

    'quiero 1 caja de 1,4,7 y 8' → [(1,1), (1,4), (1,7), (1,8)]  (cantidad 1)
    'quiero 2 cajas de la opción 3' → [(2,3)]
    'la opción 1' → [(1,1)]
    'quiero 2 cajas de 1 y 1 caja de 3,4,5 y 7' → [(2,1), (1,3), (1,4), (1,5), (1,7)]
    Devuelve None si el texto no es una elección de opciones.
    """
    if not texto:
        return None
    t = texto.strip().lower()
    # Número suelto ("2") tras "¿Cuál prefieres?": es la elección de la opción
    # 2 (cantidad 1). El pre-check solo actúa si hay last_options (lista
    # mostrada), así que no choca con "2 cajas" (cantidad).
    if re.fullmatch(r"\d{1,2}", t):
        v = int(t)
        if v >= 1:
            return [(1, v)]
    # "1 caja de cada uno/a" (tras una receta/lista): selecciona TODAS las
    # opciones mostradas con esa cantidad. Va ANTES de es_eleccion porque no
    # lleva lista de números ("de cada uno" sin comas). La lista concreta la
    # resuelve el pre-check con runtime.last_options (opciones 1..N).
    if re.search(r"\bde\s+cada\s+un[oa]s?\b", t):
        m_cant_each = re.search(r"(\d+)\s*(?:caja|cajas|unidad|unidades)", t)
        cant_each = max(1, int(m_cant_each.group(1))) if m_cant_each else 1
        # Cantidad máxima razonable: el pre-check la recorta a last_options.
        return [(cant_each, i) for i in range(1, 51)]
    # Detectar intención: menciona "opción" o hay una lista de números con
    # unidad de caja o separada por comas/y, o "quiero" + lista, o "N caja(s) de M".
    es_eleccion = ("opci" in t) or (
        re.search(r"\b(caja|cajas|unidad|unidades)\b", t)
        and re.search(r"\d{1,2}\s*[,y]\s*\d{1,2}", t)
    ) or (
        re.search(r"\b(quiero|quisiera|necesito|dame|me das)\b", t)
        and re.search(r"\d{1,2}\s*[,y]\s*\d{1,2}", t)
    ) or (
        re.search(r"\b(caja|cajas|unidad|unidades)\s+de\s+(\d{1,2})\b", t)
    )
    if not es_eleccion:
        return None

    # ---- FORMA CON LA PALABRA "opción" ------------------------------------------
    # En "<cantidad> caja(s) opción <M>" la cantidad va ANTES de la palabra y el número
    # de opción DESPUÉS. El escaneo genérico de números de más abajo se come el primero
    # como si fuera una opción: la cantidad hacía de opción Y se perdía la cantidad.
    #
    # Caso real (conv 2109, provider 19, 2026-10-17 03:33):
    #     "X 1 caja opción 6, x 3 opción 12, x 1 opción 16, x 1 opción 18,
    #      x 1 opción 23, x 1 opción 27"
    #     antes → [(1,1), (1,6), (1,3), (1,12), (1,16), (1,18), (1,23), (1,27)]
    #             ocho entradas donde el cliente escribió seis: la opción 1 es FANTASMA
    #             ("1 caja") y la opción 12 iba con cantidad 3, no 1.
    #     ahora → [(1,6), (3,12), (1,16), (1,18), (1,23), (1,27)]
    if "opci" in t:
        pares: list[tuple[int, int]] = []
        for m_op in re.finditer(r"opci[oó]n\s+(\d{1,2})", t):
            opcion = int(m_op.group(1))
            cantidad = 1
            # La cantidad es el número inmediatamente anterior ("x 3 opción 12" → 3).
            ventana = t[max(0, m_op.start() - 14):m_op.start()]
            m_cant_antes = re.search(
                r"(\d{1,2})\s*(?:cajas?|unidades?)?\s*(?:de\s+la\s+|la\s+)?\s*$", ventana
            )
            if m_cant_antes:
                v = int(m_cant_antes.group(1))
                if 1 <= v <= 99:
                    cantidad = v
            pares.append((cantidad, opcion))
            # "…opción 5 y 7": el 7 va con la MISMA cantidad. La 'y' tiene que ir pegada
            # al número de opción para no comerse el arranque del bloque siguiente.
            m_mas = re.match(r"\s*[,y]\s*(\d{1,2})\b", t[m_op.end():])
            if m_mas:
                v2 = int(m_mas.group(1))
                if 1 <= v2 <= 99 and (cantidad, v2) not in pares:
                    pares.append((cantidad, v2))
        if pares:
            return pares

    # CANTIDADES POR GRUPO: parsear bloques "N caja(s) de X,Y,Z" que pueden
    # repetirse unidos por "y", p.ej. "2 cajas de 1 y 1 caja de 3,4,5 y 7".
    # Se separa en grupos donde CADA grupo tiene su propia cantidad explícita.
    grupos_raw = re.split(
        r"\b(?:y\s+)?(?=\d+\s*(?:caja|cajas|unidad|unidades)\s+de\b)", t
    )
    grupos_raw = [g for g in grupos_raw if g.strip()]

    resultado: list[tuple[int, int]] = []
    for grupo in grupos_raw:
        grupo = grupo.strip()
        # Cantidad de ESTE grupo (cada grupo trae su propia "N cajas de").
        m_cant = re.search(r"(\d+)\s*(?:caja|cajas|unidad|unidades)\s+de\b", grupo)
        if not m_cant:
            # Grupo sin cantidad propia: hereda la del grupo anterior si hubo,
            # si no, 1 (default).
            cantidad = resultado[-1][0] if resultado else 1
        else:
            cantidad = max(1, int(m_cant.group(1)))
        resto = grupo
        resto = re.sub(r"\d+\s*(?:caja|cajas|unidad|unidades)\s+de\b", " ", resto, count=1)
        # Un número pegado a "caja(s)/unidad(es)" es una CANTIDAD, nunca una opción —
        # aunque no lleve el "de" ("x 2 cajas 3" → cantidad 2, opción 3). Sin esto, la
        # cantidad entra en la lista de opciones y se agrega un producto fantasma.
        resto = re.sub(r"\b\d{1,2}\s*(?:cajas?|unidades?)\b", " ", resto)
        # Opciones: números de 1-2 dígitos en el resto.
        for n in re.findall(r"\b(\d{1,2})\b", resto):
            v = int(n)
            if v >= 1 and (cantidad, v) not in resultado:
                resultado.append((cantidad, v))

    if not resultado:
        return None
    return resultado


def _menciona_opcion_numerada(texto: str) -> bool:
    """F14.2 — True si el mensaje selecciona una opción por número ("opción 9").

    En un mensaje MIXTO ("Precio de la macrodantina?? Voy a querer una vitamina
    C de la opción 9") esa selección es una COMPRA CONFIRMADA contra la lista
    que el cliente YA vio, y manda sobre cualquier búsqueda nueva: resolverla
    antes de extraer término evita que el resto del texto se interprete como
    medicamento ("macrodantina querer vitamina opción").
    """
    if not texto:
        return False
    return bool(
        re.search(
            r"\bopci[oó]n(?:es)?\s*(?:n[úu]mero|n[º°])?\s*#?\s*\d{1,2}\b",
            texto.strip().lower(),
        )
    )


def _extraer_eleccion_opcion(texto: str) -> tuple[int, int] | None:
    """Detecta la elección por número de opción con cantidad.

    'quiero 2 cajas de la opción 3' → (cantidad=2, opcion=3)
    'la opción 1' → (cantidad=1, opcion=1)  (sin cantidad explícita)
    'quiero la 5' / 'la opción 5' → (1, 5)
    Devuelve None si el texto no menciona una opción.
    """
    if not texto:
        return None
    t = texto.strip().lower()
    # "opción N" (con o sin cantidad previa: "2 cajas de la opción 3")
    m = re.search(
        r"(?:de\s+la\s+)?opci[oó]n\s+(\d{1,2})",
        t,
    )
    if m:
        opcion = int(m.group(1))
        cantidad = _extraer_cantidad(texto)
        return cantidad, opcion
    # "quiero la 5" / "la 3" (número después de "la", sin palabra opción)
    m2 = re.search(r"\b(?:quiero|necesito|dame)\s+(?:la\s+|el\s+)?(\d{1,2})\s*(?:cajas?|unidades?)?\s*$", t)
    if m2:
        return 1, int(m2.group(1))
    return None


def _pregunta_es_cantidad(messages: list[dict[str, Any]]) -> bool:
    """True si el último mensaje del asistente en el historial pregunta por
    CANTIDAD ('¿cuántas cajas/unidades?') — el número que responda el cliente
    es una cantidad, no la elección de una opción.

    OJO: se usa `_ultimo_mensaje_asistente` y NO un bucle que corte en el primer
    `user` desde el final. `messages` incluye el mensaje del cliente del turno
    ACTUAL al final, así que ese bucle devolvía siempre False y este guard nunca
    se activaba (dead code): un "2" tras "¿cuántas cajas?" se interpretaba como
    OPCIÓN 2 en vez de CANTIDAD 2."""
    texto = _ultimo_mensaje_asistente(messages)
    if not texto:
        return False
    texto = texto.lower()
    return bool(
        re.search(r"cu[aá]ntas?\s+(?:cajas?|unidades?|blister|ampollas?)", texto)
        or re.search(r"qu[eé] cantidad", texto)
    )


def _es_respuesta_cantidad(texto: str, has_last_product: bool = False) -> bool:
    """True si el texto parece una respuesta de cantidad/unidad (no una
    búsqueda de medicamento). P. ej. '2 cajas', 'si, quiero 3 unidades'.

    Si hay un producto consultado (has_last_product), un número suelto ('1')
    también cuenta como cantidad: el agente acabó de preguntar "¿cuántas
    cajas quiere?" y el cliente responde con un número solo."""
    if not texto:
        return False
    t = texto.strip().lower()
    # Número + unidad de medida → respuesta de cantidad ("2 cajas", "3 unidades").
    if re.search(r"\d+\s*(?:de\s+)?" + _UNIDADES_MEDIDA.pattern, t):
        return True
    # Palabra "una/un" + unidad → cantidad 1 ("una caja", "un frasco").
    if re.search(r"\b(una|un)\s+(?:de\s+)?" + _UNIDADES_MEDIDA.pattern, t):
        return True
    # "si/yes/ok/claro" seguido de cantidad.
    if re.search(r"\b(si|sí|ok|claro|dale|siempre)\b.*\d", t):
        return True
    # Número suelto cuando ya hay un producto consultado (respuesta a "¿cuántas?").
    if has_last_product and re.fullmatch(r"\s*\d{1,3}\s*", t):
        return True
    return False


def _extraer_cantidad(texto: str) -> int:
    """Extrae el número de una respuesta de cantidad ('2 cajas' -> 2)."""
    if not texto:
        return 1
    m = re.search(
        r"(\d+)\s*(?:de\s+)?(?:caja|cajas|unidad|unidades|blister|blíster|"
        r"tabletas|tableta|tabs|comprimidos|comprimido|ampollas|ampolla|"
        r"frasco|frascos|tubo|tubos|piezas|pieza|pack|sobre|sobres|"
        r"grageas|gragea|cápsulas|capsulas)",
        texto.lower(),
    )
    if m:
        try:
            return max(1, int(m.group(1)))
        except ValueError:
            return 1
    return 1


# Intención de VER RESUMEN: el cliente ya no quiere más productos.
_INTENTO_VER_RESUMEN = re.compile(
    r"\b(ver (?:el )?resumen|resumen|listo|no quiero (?:nada )?más|"
    r"no (?:más|otro)|ya (?:está|esta|basta)|eso (?:es|sería) todo|"
    r"terminar|cerrar (?:el )?pedido|finalizar)\b",
    re.IGNORECASE,
)

# Verbos de AGREGAR productos. Ver `_tambien_pide_agregar`.
_RE_PIDE_AGREGAR = re.compile(
    r"\b(agrega|agregas|agregar|agregame|agrégame|agregale|agrégale|agregalo|"
    r"añade|añades|añadir|añademe|añademela|añádele|"
    r"suma|sumas|sumale|súmale|sumame|sumamele|"
    r"incluye|incluyes|incluir|incluyeme|"
    r"ponme|pones|mete|meteme|adiciona|adicionar)\b",
    re.IGNORECASE,
)


def _quiere_info_horario(texto: str) -> bool:
    """True si el cliente pregunta el HORARIO de la farmacia.

    "Hasta que hora esta abierta la farmacia" / "a qué hora abren" / "están
    abiertos?" → hay que llamar info_provider para responder con el horario real
    (campo `hours` del provider en Firestore).

    Sin este backstop el LLM respondía de memoria genérica o directamente
    "no pude obtener la información del horario de la farmacia... ¿paso tu
    consulta a un humano?" — porque el prompt le prohíbe buscar en el catálogo
    con la palabra "horario" y no le quedaba camino para consultar el dato.
    """
    if not texto:
        return False
    t = texto.strip().lower()
    # Una CITA ("a qué hora es mi cita", "mover mi cita") NO es el horario del
    # local: eso lo resuelve el flujo de agendamiento con su propia agenda.
    if re.search(r"\bcita|citas|agenda|turno\b", t):
        return False
    # Si hay un verbo de EFECTO ("abre la nariz", "sirve para", "alivia") la
    # frase habla del fármaco, no del local: "¿el atamel abre la nariz?" usa
    # "abre" en otro sentido y no pregunta cuándo abre la farmacia.
    if re.search(r"\b(alivia|sirve|funciona|desinflama|calma|efecto|"
                 r"abre\s+(?:la|el|las|los)|despeja|quita)\b", t):
        return False
    # Verbos de abrir/cerrar/atender del LOCAL: cubren las frases que no dicen
    # "horario" ("¿están abiertos?", "¿abren los domingos?", "¿cierran hoy?").
    abrir_cerrar = bool(re.search(
        r"\b(abren|abre|abriran|abrir[aá]n|cierran|cierra|cerraran|cerrar[aá]n|"
        r"abiert[oa]s?|cerrad[oa]s?|atienden|atendiendo|atiende)\b", t))
    if re.search(r"\b(horario|horarios|hora|horas|atencion|atención)\b", t):
        # "¿horario?" a secas, o frase corta que ya es inequívoca.
        if len(t.split()) <= 3:
            return True
        return abrir_cerrar or bool(re.search(
            r"(farmacia|local|negocio|atencion|atención|atienden|hasta|"
            r"a\s+qu[eé]\s+hora|me\s+pueden|decir|informaci[oó]n|cu[aá]l)", t))
    # Sin la palabra "horario"/"hora": solo cuenta si habla de abrir/cerrar.
    return abrir_cerrar


def _tambien_pide_agregar(texto: str) -> bool:
    """¿El cliente, ADEMÁS, está agregando productos al carrito?

    Sirve para NO preguntar el método de entrega cuando el pedido aún está creciendo.
    Preguntar "¿cómo prefieres recibirlo?" mientras el cliente todavía dicta productos
    corta el pedido a la mitad y **se traga lo que pidió**.

    Caso real (conv 2109, provider 19, 2026-10-17 03:37):
        cliente: "Me puedes dar el resumen de esto y le agregas 3 cajas de glymar
                  de 4 MG y 2 toallas securezza x 15"
        agente : "Antes de cerrar tu pedido, ¿cómo prefieres recibirlo? 1️⃣ 2️⃣"
    El cliente pedía tres cosas —el resumen, 3 GLYMAR y 2 SECUREZZA— y solo recibió el
    menú de entrega. `_quiere_ver_resumen` veía la palabra "resumen" y el guard de la
    rama 2 cerraba el turno ahí.

    Se detectan los verbos de AGREGAR (agrega/agregas/agregame/añade/sumale/incluye/
    ponme/mete...). Si el cliente sigue agregando, el turno va al LLM, que sí tiene las
    tools del carrito; la pregunta de entrega llegará cuando el pedido se cierre de
    verdad (un "LISTO" o un "dame el resumen" sin más).
    """
    if not texto:
        return False
    return bool(_RE_PIDE_AGREGAR.search(texto.strip().lower()))


def _quiere_ver_resumen(texto: str, tiene_carrito: bool = False) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    if _INTENTO_VER_RESUMEN.search(t):
        return True
    # "no" suelto (respuesta a "¿Deseas buscar otro medicamento?") → si hay
    # carrito, el cliente quiere ver el resumen, no más búsquedas.
    # Acepta "no", "no, gracias", "no gracias" (cierre) pero NO "no, quiero X"
    # (sigue buscando otro medicamento).
    if tiene_carrito and re.fullmatch(r"no[.,]?\s*(?:gracias)?\s*", t):
        return True
    return False


# Intención de CONFIRMAR/FINALIZAR el pedido.
_INTENTO_FINALIZAR = re.compile(
    r"\b(confirmar|confirmo|si (?:confirmo|está|esta)|dale (?:así|asi)|"
    r"proceder|adelante|listo (?:confirmo|para)|finalizar pedido|"
    r"haz (?:el )?pedido|registra (?:el )?pedido)\b",
    re.IGNORECASE,
)


def _quiere_finalizar(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    return bool(_INTENTO_FINALIZAR.search(t))


# La pregunta de cierre del resumen: "¿Está todo correcto o deseas agregar algo
# más?" mezcla DOS intenciones en una sola pregunta. Un "si" como respuesta es
# ambiguo para el LLM, que lo lee como "sí, quiero agregar algo más" (bug 29/09:
# tras confirmar con "si" respondió "¡Perfecto! ¿Qué deseas agregar al pedido?").
_PREGUNTA_CIERRE_RESUMEN = (
    "está todo correcto",
    "esta todo correcto",
    "deseas agregar algo",
    "confirmas el pedido",
    "quieres agregar otro medicamento",
)

# Frases con las que el agente PIDE PRECISAR una búsqueda antes de poder ofrecer
# un producto concreto. No son cierres: son preguntas que dejan la conversación
# a la espera de una respuesta corta ("650", "el de calox", "la de 30").
#
# POR QUÉ IMPORTA (bug de clase, 2026-10): el agente pregunta "¿Qué miligramo
# necesitas?" y el cliente responde "Tabletas 650". Sin saber que ESA pregunta
# estaba en el aire, el turno siguiente trata la respuesta como un mensaje
# suelto: no reconoce que refina la búsqueda anterior y vuelve a listar todo.
# Tres bugs distintos de la misma sesión (la foto, la despedida, el refinamiento)
# comparten esta raíz: el agente no sabía qué pregunta había hecho él mismo.
_PREGUNTAS_PRECISAR = (
    "qué miligramo",
    "que miligramo",
    "cuál miligramo",
    "cual miligramo",
    "qué concentración",
    "que concentracion",
    "qué presentación",
    "que presentacion",
    "qué dosis",
    "que dosis",
    "cuál de estas",
    "cual de estas",
    "cuál necesitas",
    "cual necesitas",
    "cuál prefieres",
    "cual prefieres",
    "de qué marca",
    "de que marca",
    "qué marca",
    "que marca",
    "de qué laboratorio",
    "que laboratorio",
    "cuál te sirve",
    "cual te sirve",
    "qué cantidad de unidades",
    "cuantas unidades",
    "cuántas unidades",
    "de cuántas tabletas",
    "de cuantas tabletas",
    "qué tamaño de caja",
    "que tamano de caja",
    "qué sabor",
    "que sabor",
    "para qué lo necesitas",
    "para que lo necesitas",
)


def _ultimo_mensaje_asistente(messages: list[dict[str, Any]]) -> str | None:
    """Texto del último mensaje del ASISTENTE en `messages`, saltando los
    mensajes del turno actual del cliente (que van al final) y las
    tool-calls/tool-results que los backstops insertan (role 'tool' o assistant
    con content None). Devuelve None si el turno anterior no fue del asistente."""
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "system":
            return None
        if role == "tool":
            continue
        if role == "assistant":
            if msg.get("content"):
                return str(msg["content"])
            continue
        if role == "user":
            continue
    return None


def _pregunta_cierre_resumen(messages: list[dict[str, Any]]) -> bool:
    """True si el último mensaje del asistente es la pregunta de cierre del
    Resumen del Pedido ('¿Está todo correcto o deseas agregar algo más?')."""
    texto = _ultimo_mensaje_asistente(messages)
    if not texto:
        return False
    return any(p in texto.lower() for p in _PREGUNTA_CIERRE_RESUMEN)


# ------------------------------------------------------------- ENTREGA ---
# Antes del Resumen del Pedido se pregunta CÓMO quiere recibirlo:
#   1. Delivery          → se pide la dirección
#   2. Retirar en Farmacia
# El resumen final muestra el bloque MÉTODO DE ENTREGA con ese dato.

# "1" suelto o la palabra delivery. El "1"/"2" solo cuenta si NUESTRA pregunta de
# entrega está pendiente (lo garantiza delivery_pending), así que aquí basta con
# reconocer las formas explícitas y los números.
_DELIVERY_NUM = re.compile(r"\s*1\b")
_PICKUP_NUM = re.compile(r"\s*2\b")

_DELIVERY_PALABRAS = re.compile(
    r"\b(delivery|delibery|deliberi|env[íi]o|enviar|enviarlo|mandar|mandarlo|"
    r"a\s+domicilio|domicilio|a\s+mi\s+casa|traerlo|traelo|llevar|llevarlo|"
    r"despacho|motorizado)\b",
    re.IGNORECASE,
)

# Menú del MÉTODO DE ENTREGA y petición de la dirección. Mensajes DETERMINISTAS:
# se envían tal cual (no pasan por el LLM) para que las opciones 1/2 sean siempre
# las mismas y el cliente pueda responder con un número.
MENSAJE_METODO_ENTREGA = (
    "Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?\n\n"
    "1️⃣ Delivery (envío a tu dirección)\n"
    "2️⃣ Retirar en Farmacia\n\n"
    "Responde *1* o *2*."
)

MENSAJE_PEDIR_DIRECCION = (
    "¡Perfecto! 🛵 Envíame la *dirección* donde quieres recibir el pedido "
    "(calle, número, sector y una referencia si aplica)."
)

# El cliente pide hablar con una persona del negocio. La respuesta es DETERMINISTA:
# reconoce la petición, confirma que YA se avisó al equipo y no promete contactos
# personales (el agente no debe dar teléfonos de empleados). El tono es cálido: el
# cliente que pide un humano suele estar frustrado o con prisa.
MENSAJE_HANDOFF_HUMANO = (
    "¡Claro que sí! Ya avisé al equipo de la farmacia para que te atienda una "
    "persona directamente. 🙌\n\n"
    "En breve se comunican contigo por este mismo chat. Si quieres adelantar algo, "
    "déjame aquí tu consulta y se la paso tal cual."
)

# ------------------------------------------------------------- HANDOFF ---
# Detección DETERMINISTA de "quiero hablar con un humano". El prompt ya pide llamar
# la tool `handoff` "SIEMPRE, a la primera", pero depender del modelo deja el caso al
# azar: medido en producción (conv 2720, 07-oct), el cliente escribió "Pasame el humano"
# y el agente respondió "no puedo pasarte el contacto de ninguna persona directamente"
# SIN hacer el handoff.
_HANDOFF_PALABRAS_PERSONA = (
    "humano", "humana", "persona", "personas", "empleado", "empleada",
    "encargado", "encargada", "gerente", "supervisor", "asesor", "vendedor",
    "alguien", "dueño", "dueña",
    # 'el equipo' es el personal del negocio. Caso real (provider 19, 2026-10-29):
    # "Puedes por favor pasarlo al equipo" no activaba el guard y el agente buscó
    # 'pasarlo' en el catálogo ("⚠️ No tengo PASARLO").
    "equipo", "staff", "farmacéutico", "farmaceutico", "químico", "quimico",
)
_HANDOFF_VERBOS = (
    "pasa", "pasame", "pásame", "pasen", "pasenme", "pasarme", "paselo", "pasamelo",
    "paseme", "páseme", "pasemelo", "quiero", "quisiera", "necesito", "puedo",
    "deseo", "prefiero", "hablar", "comunicar", "comunicarme", "chatear",
    "atiendanme", "atiendeme", "tratar",
    # CON PRONOMBRE: 'pasarlo/pasarla/pasarlos al equipo' es la forma más natural de
    # pedir el pase y no estaba.
    "pasarlo", "pasarla", "pasarlos", "pasarlas", "pasarselo", "pasárselo", "pasarle",
    "transferir", "transferirlo", "derivar", "derivarlo", "escalar", "escalarlo",
    "atienda", "atender", "atiéndelo", "atiendelo",
)
# RAÍCES de los verbos de pase: cubren las formas con pronombre que no se pueden enumerar
# ('derívalo', 'transferirlo', 'escalalo', 'atendedme', 'pásemelo').
_HANDOFF_RAICES = (
    "pas", "transfer", "deriv", "escal", "atien", "atend", "comunic", "hablar", "chate",
    "quier", "necesit", "pued", "dese", "prefier", "tratar", "contact",
)
_PIDE_HUMANO = re.compile(
    r"(\bpas(?:a|ame|en|arme|elo|enme|eme|ame(?:lo)?)\b[^.]{0,25}\b(?:human|person|"
    r"emplead|alguien|encargad|due[ñn]|gerente|supervisor|vendedor|asesor|agente)|"
    r"\b(?:quiero|quisiera|necesito|puedo|deseo|prefiero)\b[^.]{0,25}\b"
    r"(?:hablar|comunicar(?:me)?|chatear|tratar|atend(?:er|erme))\b[^.]{0,20}"
    r"\b(?:human|person|emplead|alguien|encargad|gerente|supervisor|real)|"
    r"\b(?:hablar|comunicar(?:me)?)\s+con\s+(?:un|una|el|la)\s+"
    r"(?:human|person|emplead|encargad|due[ñn]|gerente|supervisor)|"
    r"\b(?:atien(?:de|dan)me|atiendanme)\s+(?:un|una)\s+(?:human|person)|"
    r"\b(?:human|persona)\s+real\b|"
    r"\bhay\s+(?:alguien|una\s+persona)\s+(?:ah[íi]|disponible)\b|"
    r"\b(?:no\s+)?quiero\s+(?:un|una)\s+(?:human|person)|"
    r"\bpasame\s+con\s+(?:alguien|el\s+encargad|el\s+due[ñn])|"
    r"\bpas(?:a|ame|arlo|arla|arlos|arlas|en|enme|eme|elo)\b[^.]{0,30}"
    r"\b(?:equipo|staff|personal|farmac[ée]utic\w*|auxiliar)\b)",
    re.IGNORECASE,
)


def _pide_hablar_con_humano(texto: str) -> bool:
    """¿El cliente pide EXPLÍCITAMENTE hablar con una persona del negocio?

    Se aplica sobre el texto del cliente SIN marcadores del sistema, para que las
    instrucciones internas ("pasa al lead a un humano si…") no lo disparen.

    Dos capas: un patrón de frases completas y una red de seguridad por palabras
    (verbo de petición + palabra de persona) que cubre los typos del cliente.
    """
    if not texto:
        return False
    t = texto.strip().lower()
    if _PIDE_HUMANO.search(t):
        return True
    palabras = re.findall(r"[a-záéíóúüñ]+", t)
    if any(p in t for p in _HANDOFF_PALABRAS_PERSONA) and (
        any(v in t for v in _HANDOFF_VERBOS)
        or any(w.startswith(_HANDOFF_RAICES) for w in palabras)
    ):
        # "te paso con un humano" / "voy a pasarte con alguien" es el AGENTE hablando
        # (o el historial citado), no una petición del cliente: NO cuenta.
        if re.search(r"\b(te|le)\s+paso\b|\bvoy\s+a\s+pasarte\b", t):
            return False
        return True
    return False
_PICKUP_PALABRAS = re.compile(
    r"\b(retirar|retiro|reto|buscar(?:lo)?|voy\s+a\s+buscar|paso\s+a\s+buscar|"
    r"paso\s+por|lo\s+recojo|recojo|recoger|en\s+la\s+farmacia|en\s+tienda|"
    r"presencial|yo\s+lo\s+busco)\b",
    re.IGNORECASE,
)


def _eleccion_entrega(texto: str) -> str | None:
    """¿El cliente eligió delivery o retiro? None si no se reconoce.

    Se usa SOLO cuando el agente acaba de preguntar por el método de entrega
    (`delivery_pending == 'method'`), así que un "1"/"2" suelto es la respuesta a
    ESA pregunta. Las palabras explícitas también se aceptan por si el cliente
    contesta "quiero delivery" o "lo retiro yo".
    """
    if not texto:
        return None
    t = texto.strip().lower()
    # Número suelto (la respuesta más común al menú 1/2).
    if re.fullmatch(r"\s*1\s*[.)]?\s*", t):
        return "delivery"
    if re.fullmatch(r"\s*2\s*[.)]?\s*", t):
        return "pickup"
    # Palabras explícitas. El retiro gana si menciona ambas ("lo retiro, no envío").
    if _PICKUP_PALABRAS.search(t):
        return "pickup"
    if _DELIVERY_PALABRAS.search(t):
        return "delivery"
    return None


def _pide_el_total(texto: str) -> bool:
    """¿El cliente está pidiendo el TOTAL / el resumen del pedido?

    Se evalúa cuando el agente ACABA de preguntar cómo quiere recibir el pedido
    (`delivery_pending == 'method'`) y el cliente, en vez de contestar "1"/"2", pregunta
    cuánto es. Hay que contestarle lo que pide, no repetirle el menú.

    Caso real (conv 1003, provider 19, 2026-10-16):
        22:19  cliente: "Cuánto sería el total."
        22:19  agente : "Antes de cerrar tu pedido, ¿cómo prefieres recibirlo? 1 / 2"
        22:20  cliente: "Cuánto sería el monto total de los 3 productos ?"
               agente : (sin respuesta — el menú se repetía)
    El cliente preguntó el total DOS veces y nunca lo vio.

    NO se usa para decidir si mostrar el resumen: eso ya lo hace `_quiere_ver_resumen`.
    Esto es solo para reconocer que el cliente está INSISTIENDO con el total mientras hay
    una pregunta de entrega en vuelo. Por eso es deliberadamente amplio: en ese contexto,
    'total' o 'monto' sin más ya significan "dame la cuenta".
    """
    if not texto:
        return False
    t = texto.strip().lower()
    if _INTENTO_VER_RESUMEN.search(t):
        return True
    if re.search(r"\b(total|monto|subtotal|cuenta)\b", t):
        return True
    # "cuánto es/sale/vale/sería" sin la palabra total ("¿y cuánto es?", "¿cuánto sale?").
    # Exige el verbo: un 'cuánto' suelto puede ser una pregunta de dosis ("cuánto de X").
    return bool(re.search(
        r"\bcu[aá]nt[oa]s?\s+(?:es|son|sale|salen|vale|valen|ser[íi]a|serian|"
        r"ser[íi]an|me\s+da|da|queda|quedan|cuesta|cuestan)\b", t))


def _parece_direccion(texto: str) -> bool:
    """¿El texto parece una dirección de entrega?

    Se evalúa cuando el agente pidió la dirección (`delivery_pending=='address'`),
    así que el mensaje es la respuesta a ESA pregunta. Se exige una señal mínima de
    dirección para no guardar una consulta de medicamento como si fuera dirección.
    """
    if not texto:
        return False
    t = texto.strip()
    if len(t) < 5 or len(t) > 300:
        return False
    # Señales típicas de dirección en Venezuela.
    señales = re.compile(
        r"(\b(av(?:e|enida)?|calle|callej[oó]n|carrera|carera|urbanizaci[oó]n|urb|"
        r"sector|barrio|manzana|mz|parcela|pc|residencias|res\.|edificio|edif|"
        r"torre|apto|apartamento|aparta|piso|pto|casa|cas\.|quinta|qta|villa|"
        r"vereda|pasaje|pje|transversal|diagonal|bloque|nro|n[uú]mero|#)\b"
        r"|\d{1,4})",
        re.IGNORECASE,
    )
    return bool(señales.search(t))


def _hay_pregunta_de_entrega(runtime: Any) -> bool:
    """¿El agente está esperando una respuesta del PASO DE ENTREGA?

    Una sola definición para los DOS sitios que resuelven números contra
    `last_options` (el pre-check antes del LLM y el backstop de la rama sin-tools).
    Antes la guarda estaba copiada en ambos, con el riesgo de que uno divergiera.

    Mientras haya una pregunta de entrega en vuelo, un "1"/"2" suelto contesta ESA
    pregunta y NUNCA elige una opción de la lista de medicamentos. Sin esto:
    "2" (LA SANTE), luego "1" (delivery) → el "1" se resolvía como la opción 1 del
    catálogo y se agregaba otro medicamento que el cliente no pidió.
    """
    pendiente = getattr(getattr(runtime, "_conv", None), "delivery_pending", "") or ""
    return bool(pendiente or getattr(runtime, "delivery_pendiente", ""))


def _agente_pidio_precisar(messages: list[dict[str, Any]]) -> bool:
    """True si el turno anterior del agente pidió PRECISAR la búsqueda.

    Es el contexto que da sentido a respuestas cortas como "650", "Tabletas 650",
    "el de calox" o "la de 30": no son mensajes sueltos, son la respuesta a una
    pregunta concreta del agente. Saberlo permite tratar ese turno como un
    REFINAMIENTO de la búsqueda anterior en vez de una consulta nueva.
    """
    texto = _ultimo_mensaje_asistente(messages)
    if not texto:
        return False
    t = texto.lower()
    return any(p in t for p in _PREGUNTAS_PRECISAR)


def filtrar_opciones_mostradas(
    respuesta: str, opciones: list[dict[str, Any]] | None
) -> tuple[list[dict[str, Any]], str]:
    """Filtra la lista de opciones que el cliente YA VIO según su respuesta.

    POR QUÉ NO BASTA RE-CONSULTAR EL CATÁLOGO: el motor del CRM, cuando la
    consulta trae varios tokens, cae al grupo difuso y matchea por el fármaco
    ignorando el resto. Medido contra el catálogo real, 'acetaminofen 650 calox'
    devuelve los MISMOS 20 productos con TODOS los laboratorios (DROTAFARMA,
    ELTER, CALOX, GV, ALESS) — no filtra la marca. Re-consultar tampoco servía
    para el tamaño ('el de 30').

    Pero el agente guarda `last_options`: la lista EXACTA que el cliente vio, en
    el mismo orden. Filtrarla localmente es determinista y no depende del matcher
    del catálogo. Medido: 'el de calox' → 2 opciones (las de CALOX), 'el de 30' →
    1, 'la caja de 20' → 1.

    Devuelve (opciones_filtradas, motivo). Si no reconoce la respuesta, devuelve
    la lista completa con motivo '' — nunca vacía, para no esconder productos.
    """
    if not respuesta or not opciones:
        return opciones or [], ""

    def norm(s: object) -> str:
        t = str(s or "").lower()
        for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                     ("ñ", "n"), ("ü", "u")):
            t = t.replace(a, b)
        return t

    t = norm(respuesta)
    # Un pedido ("2 cajas") o la elección de una opción por número ("la 3") NO son
    # filtros: los resuelve el backstop de carrito. No tocar la lista.
    if re.search(r"\b\d+\s*(?:cajas?|unidades?|frascos?|paquetes?|blisters?)\b", t):
        return opciones, ""
    if re.search(r"\b(?:la|el|opcion|opción|numero|número)\s*\d{1,2}\b", t) and not re.search(
        r"\b\d{3,4}\b", t
    ):
        return opciones, ""

    # 1) DOSIS: número de 3-4 cifras → la concentración en mg del título.
    m = re.search(r"\b(\d{3,4})\b", t)
    if m:
        dosis = m.group(1)
        sel = [p for p in opciones
               if re.search(rf"\b{dosis}\s*mg\b", norm(p.get("nombre") or p.get("producto")))]
        if sel:
            return sel, f"dosis {dosis} mg"

    # 2) TAMAÑO de la caja: número pequeño → el "X N" del título ('el de 30').
    m = re.search(r"\b(\d{1,2})\b", t)
    if m:
        n = m.group(1)
        sel = [p for p in opciones
               if re.search(rf"x\s*{n}\b", norm(p.get("nombre") or p.get("producto")))]
        if sel:
            return sel, f"tamaño x{n}"

    # 3) MARCA o laboratorio: palabra con cuerpo que aparezca en algunos títulos.
    #    Se excluyen las palabras del propio fármaco y las genéricas del dominio
    #    ('tabletas', 'caja', 'marca', 'generico'): no identifican una opción.
    genericas = {
        "acetaminofen", "paracetamol", "tabletas", "tableta", "tab", "capsulas",
        "capsula", "comprimidos", "jarabe", "gotas", "suspension", "caja", "marca",
        "generico", "generica", "laboratorio", "quiero", "dame", "aquel", "esta",
        "este", "esas", "esos", "grande", "pequena", "pequeno", "barato", "caro",
        "unidades", "pastillas", "blister", "sobre",
    }
    for w in re.findall(r"[a-z]{4,}", t):
        if w in genericas:
            continue
        sel = [p for p in opciones
               if w in norm(p.get("nombre") or p.get("producto"))]
        if sel and len(sel) < len(opciones):
            return sel, f'marca "{w}"'

    return opciones, ""


def _es_confirmacion_resumen(texto: str) -> bool:
    """True si el texto, en respuesta a la pregunta de cierre del resumen, es una
    CONFIRMACIÓN de que el pedido está correcto ('si', 'correcto', 'perfecto').
    Un 'si' seguido de un medicamento ('si, atamel') NO cuenta: ahí el cliente
    quiere agregar algo al pedido."""
    if not texto:
        return False
    t = texto.strip().lower()
    # "si, quiero atamel" / "si agrega X" → quiere agregar, no confirmar.
    if re.search(r"\b(agreg|a[ñn]ad|suma|busca|quiero\s+\w{4,})", t):
        return False
    return bool(
        re.fullmatch(
            r"\s*(si|sí|ok|okey|dale|correcto|perfecto|exacto|todo correcto|"
            r"si todo correcto|sí todo correcto|esta bien|está bien|"
            r"asi es|así es|de acuerdo|claro)[.!,\s]*",
            t,
        )
    )


def _append_forced_tool(
    messages: list[dict[str, Any]],
    name: str,
    args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Añade una tool-call forzada (backstop) + su resultado a los mensajes LLM."""
    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"bkp_{name}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            ],
        }
    )
    messages.append(
        {
            "role": "tool",
            "tool_call_id": f"bkp_{name}",
            "content": json.dumps(result, ensure_ascii=False, default=str),
        }
    )


async def _fallback_farmacia(
    user_text: str, runtime: ToolRuntime, ctx: AppContext, farmacia: bool
) -> str:
    """Respuesta de respaldo cuando el LLM entra en bucle de tool-calls sin
    arguments. Consulta el catálogo directamente (si el usuario pidió un
    medicamento) para no dejar al cliente sin respuesta ni inventar datos."""
    if not farmacia:
        return "¿Me cuentas un poco más para ayudarte?"
    term = _extraer_termino_medicamento(user_text)
    if not term:
        return "Disculpa, no te entendí bien. ¿Qué medicamento estás buscando?"
    data = await ctx.crm.get_products(runtime._provider_id_val, q=term, limit=5)
    products = data.get("products") or []
    if not products:
        runtime.med_not_found = True
        return (
            f"Lo siento, no tenemos {term} en nuestro catálogo. "
            "¿Prefieres hablar con un humano que te ayude a conseguirlo?"
        )
    # Lista las presentaciones con precio en USD y Bs (formato amigable 💊).
    from app.tools import _formatear_lista_productos

    lista = _formatear_lista_productos(products, term)
    return lista + "\n\n" + MENSAJE_SUGERIDO_CARRITO


def _respuesta_envase_agotado(
    termino: str,
    env_pedido: str,
    disponibles: list[str],
    productos: list[dict[str, Any]],
) -> str:
    """Respuesta determinista cuando el cliente pide un TAMAÑO DE CAJA que no hay.

    POR QUÉ EXISTE: el cliente preguntó "No tienes de 10 pastillas?" y el agente
    re-listó los envases de 30 — los MISMOS que ya había mostrado. El cliente se fue
    creyendo que sí había respuesta a su pregunta, cuando la había y era "no". Re-listar
    sin decir el NO es no responder: el cliente no puede distinguir "no tengo" de "no me
    entendió".

    Se responde en dos partes: (1) el NO explícito con los tamaños que SÍ hay — así el
    cliente sabe con precisión qué pedir; (2) la lista de lo disponible, para que pueda
    elegir sin volver a preguntar.
    """
    nombre = termino.strip().upper()
    if disponibles:
        tam = ", ".join(f"{n} unidades" for n in disponibles)
        negativo = (
            f"No tengo {nombre} en envase de {env_pedido} unidades. "
            f"Lo tengo en: {tam}."
        )
    else:
        negativo = f"No tengo {nombre} en envase de {env_pedido} unidades."
    partes = [negativo]
    if productos:
        partes.append(_formatear_lista_productos(productos, termino))
        partes.append(MENSAJE_SUGERIDO_CARRITO)
    return "\n\n".join(partes)


def _extraer_refinamiento(texto: str) -> str:
    """Extrae SOLO el refinamiento de presentación del texto: el número+unidad
    de dosis o la forma ('10 mg', '30 tabletas', 'gotas', 'ampolla').

    'tienes acido folico de 10 mg' → '10 mg'
    '30 mg' → '30 mg'
    'quiero gotas' → 'gotas'

    Devuelve '' si no hay un refinamiento claro. NUNCA incluye verbos de
    consulta ni el nombre del medicamento (evita duplicar el término).

    ORDEN LIBRE (bug reportado 2026-10): el cliente responde a "¿qué miligramo
    necesitas?" escribiendo la FORMA antes del NÚMERO — "Tabletas 650", no "650
    tabletas". La primera versión solo reconocía NÚMERO→FORMA, así que en
    "Tabletas 650" la dosis se perdía y el término quedaba 'acetaminofen
    tabletas': el catálogo devolvía las 20 presentaciones con 325/500/650 mg
    mezcladas, que es exactamente lo que el refinamiento debía evitar.

    Y la forma se TRADUCE a su unidad de dosis: medido contra el catálogo real,
    'acetaminofen 650 tabletas' devuelve 20 productos con 325/500/650 mezclados
    (la palabra 'tabletas' no filtra nada), mientras 'acetaminofen 650 mg'
    devuelve 9, TODOS de 650. 'tabletas/comprimidos/capsulas' → mg; 'jarabe/
    jbe/gotas/suspension/ampolla' → ml.
    """
    if not texto:
        return ""
    t = texto.strip().lower()
    # ORDEN DE LOS CAMINOS: primero el ENVASE ('de 10 pastillas' → '10'), porque si
    # cayera al camino de dosis, la forma se traduciría a 'mg' y buscaríamos la dosis
    # '10 mg' (inexistente) en vez del envase de 10 unidades. Ver `_pedido_de_envase`.
    env = _pedido_de_envase(t)
    if env:
        return env
    # Número + unidad explícita (la forma más fiable, en cualquier orden).
    # Las FORMAS líquidas (jarabe, gotas, ampolla...) entran aquí para que se
    # traduzcan a ml: si se dejan fuera, 'jarabe 120' cae al número suelto y se
    # lee como '120 mg' (una dosis que no existe en un jarabe).
    m = re.search(
        r"\b(\d+(?:[,.]\d+)?)\s*(mg|ml|g|mcg|gotas|tabletas|tab|comprimidos|"
        r"cápsulas|capsulas|cap|jarabe|jbe|suspensión|suspension|ampolla|"
        r"inyectable|solución|solucion)\b",
        t,
    )
    if not m:
        m = re.search(
            r"\b(mg|ml|mcg|gotas|tabletas|tab|comprimidos|cápsulas|capsulas|cap|"
            r"jarabe|jbe|suspensión|suspension|ampolla|inyectable)\s*"
            r"(\d+(?:[,.]\d+)?)\b",
            t,
        )
        if m:
            # Se invierte: la unidad va primero en el texto.
            m = _MatchInvertido(m.group(2), m.group(1))
    if m:
        num, uni = m.group(1), m.group(2)
        # Traducir la FORMA a la UNIDAD de dosis que el catálogo usa en el título.
        # Es lo que hace funcionar el filtro de dosis del CRM: su regex solo
        # reconoce número+unidad ('650 mg'), no número+forma ('650 tabletas').
        uni = _UNIDAD_DE_FORMA.get(uni, uni)
        return f"{num} {uni}".strip()
    # Número de dosis "suelto" ('de 650', 'las de 650'): el cliente responde a la
    # pregunta por el miligramo sin repetir la unidad. Se acepta solo si el número
    # tiene 3-4 cifras (una dosis plausible, no una cantidad de cajas).
    #
    # Si el mensaje menciona una forma LÍQUIDA ('jarabe', 'gotas', 'suspension'),
    # el número es un VOLUMEN: se devuelve en ml. Sin esto 'jarabe 120' → '120 mg',
    # una dosis que no existe. El umbral de cifras cubre los dos casos: un volumen
    # de jarabe es 60/120/240 y una dosis sólida 400/500/650.
    m = re.search(r"\b(\d{3,4})\b", t)
    if m and not re.search(r"\b(cajas?|unidades?|frascos?|paquetes?|blisters?)\b", t):
        liquida = re.search(
            r"\b(jarabe|jbe|gotas|suspensión|suspension|ampolla|inyectable|"
            r"solución|solucion|solución|locion|loción)\b",
            t,
        )
        return f"{m.group(1)} {'ml' if liquida else 'mg'}"
    # Presentación sin número (solo si está al final o es el foco del mensaje).
    m = re.search(r"\b(gotas|jarabe|jbe|tabletas|comprimidos|inyectable|ampolla|crema|ungüento)\b", t)
    if m:
        return m.group(1)
    return ""


class _MatchInvertido:
    """Adaptador para reutilizar el código cuando la UNIDAD va antes del número.

    `re.Match` es inmutable, así que se emula con la misma interfaz (group(n)).
    """

    def __init__(self, num: str, uni: str) -> None:
        self._num = num
        self._uni = uni

    def group(self, n: int) -> str:
        return self._num if n == 1 else self._uni


# Forma farmacéutica → unidad de DOSIS con la que el catálogo titula el producto.
# Es lo que hace que el filtro de dosis del CRM funcione: su regex solo reconoce
# número+unidad ('40 mg'), no número+forma ('40 tabletas').
_UNIDAD_DE_FORMA = {
    "tabletas": "mg", "tableta": "mg", "tab": "mg", "comprimidos": "mg",
    "comprimido": "mg", "capsulas": "mg", "cápsulas": "mg", "cap": "mg",
    "grageas": "mg", "pastillas": "mg",
    "jarabe": "ml", "jbe": "ml", "gotas": "ml", "suspension": "ml",
    "suspensión": "ml", "ampolla": "ml", "inyectable": "ml", "solucion": "ml",
    "solución": "ml", "locion": "ml", "loción": "ml",
}


# Formas CONTABLES de presentación: agrupan unidades dentro de una caja. El número que
# las acompaña es el TAMAÑO del envase ("10 pastillas" = caja de 10 unidades), NO una
# dosis. Deliberadamente NO incluye 'unidades', 'blisters', 'sobres', 'frascos': esas
# palabras acompañan una CANTIDAD DE COMPRA ("quiero 3 unidades"), no una presentación.
_FORMAS_CONTABLES = (
    "pastillas", "pastilla", "tabletas", "tableta", "tabs", "tab",
    "capsulas", "cápsulas", "caps", "cap", "comprimidos", "comprimido",
    "grageas", "gragea",
)

# Tamaño de envase: 1-2 cifras (4..99: 7, 10, 14, 20, 28, 30, 60). El mismo umbral que ya
# usa el camino de la dosis suelta: 3-4 cifras es una DOSIS plausible (160, 500, 650), no
# un envase. Se empieza en 4 para no confundir una cantidad de compra ('2 pastillas').
_RE_ENVASE_NUM_FORMA = re.compile(
    rf"\b([4-9]|[1-9]\d)\s*(?:{'|'.join(_FORMAS_CONTABLES)})\b"
)
_RE_ENVASE_FORMA_NUM = re.compile(
    rf"\b(?:{'|'.join(_FORMAS_CONTABLES)})\s*([4-9]|[1-9]\d)\b"
)
# Cantidad de COMPRA: no es un tamaño de envase, la resuelve el carrito.
_RE_CANTIDAD_COMPRA = re.compile(r"\b\d+\s*(?:cajas?|unidades?|frascos?|paquetes?|blisters?)\b")

# CANTIDAD QUE EL CLIENTE PIDE AL AGREGAR: "le agregas 3 cajas de glymar", "2 toallas
# securezza". Va ANCLADO AL PRINCIPIO de la línea para no confundirlo con la dosis ni con
# el envase: '3 cajas glymar 4 mg' → 3, pero 'glimepiride 4 mg 30 tab' → nada.
# (Es un regex PROPIO, no `_RE_CANTIDAD_COMPRA`: aquel decide si un número es envase y
# añadirle 'toallas' cambiaría ese juicio.)
_RE_CANTIDAD_PEDIDA = re.compile(
    r"^\s*(\d{1,2})\s*"
    r"(?:cajas?|unidades?|frascos?|paquetes?|blisters?|toallas?|pomos?|tubos?|"
    r"sobres?|rollos?|cajitas?)\b",
    re.IGNORECASE,
)


def _cantidad_pedida(texto: str) -> int:
    """Cuántas unidades pide el cliente en esta línea ('3 cajas de glymar' → 3).

    Devuelve 0 si no hay una cantidad explícita. Sin cantidad explícita NO se agrega
    nada: el agente ofrecería el producto y esperaría la respuesta, que es lo seguro.
    """
    m = _RE_CANTIDAD_PEDIDA.match(texto or "")
    if not m:
        return 0
    try:
        n = int(m.group(1))
    except ValueError:
        return 0
    return n if 1 <= n <= 99 else 0


# El "X N" del título del producto: 'BRASARTAN CTDN 80MG/12.5X10 FARMA' → '10'.
#
# La 'x' tiene que ser un token SUELTO. Antes la regex no miraba lo que había a su
# izquierda, así que la 'x' final del nombre del fármaco se tomaba como separador y
# la DOSIS pasaba a ser tamaño de envase: en
#     "evigax 125 mg, ... milax 120 gr"
# `_envase_de_nombre('evigax 125 mg')` devolvía '125' y 'milax 120 gr' devolvía '120',
# y el cliente leía
#     ⚠️ No tengo envase de 125 unidades. Lo tengo en: 10 unidades, 20 unidades.
# sobre un medicamento de 125 mg. Caso real (conv 7, provider 05).
_RE_X_ENVASE = re.compile(r"x\s*0*(\d{1,3})\b")

# Unidades que, justo ANTES de la 'x', indican forma compacta de envase:
# 'DOL PLUS TAB 650MGX10' → 10 cajas. Sin esto, la 'x' pegada a una letra siempre
# se descartaría y se perderían los envases escritos sin espacio.
_RE_UNIDAD_ANTES_DE_X = re.compile(r"(?:mg|mgs|gr|grs|g|kg|ml|cc|mcg|ug|ui)$")

# Unidades que convierten el número en DOSIS o peso, nunca en envase:
# 'MILAX POLVO X 360 GRAMOS' o 'EVIGAX X 120 GR' no piden 360 ni 120 cajas.
_RE_UNIDAD_DOSIS_TRAS_NUM = re.compile(
    r"^\s*(?:mg|mgs|gr|grs|g|kg|ml|cc|mcg|ug|ui|gramos?|mililitros?|miligramos?)\b"
)


def _pedido_de_envase(texto: str) -> str:
    """Extrae el TAMAÑO DE ENVASE que pide el cliente: 'de 10 pastillas' → '10'.

    POR QUÉ ES DISTINTO DE UNA DOSIS: son dos números con la misma forma pero
    significados opuestos. '650 tabletas' / 'Tabletas 650' es una DOSIS (650 mg), pero
    '10 pastillas' / 'de 10 tabletas' es CUÁNTAS unidades trae la caja. El agente
    traducía ambos con `_UNIDAD_DE_FORMA` a 'mg', así que 'de 10 pastillas' se buscaba
    como la dosis '10 mg' — que no existe — y el catálogo devolvía los envases de 30
    como si fueran la respuesta (caso real BRASARTAN, conv 2714).

    Desambigua por el RANGO, que es lo que ya hace el resto del módulo: una dosis tiene
    3-4 cifras y un envase 1-2. Devuelve '' si no hay un pedido de envase claro.
    """
    if not texto:
        return ""
    t = texto.strip().lower()
    # "quiero 3 unidades" es una CANTIDAD DE COMPRA, no la presentación.
    if _RE_CANTIDAD_COMPRA.search(t):
        return ""
    m = _RE_ENVASE_NUM_FORMA.search(t) or _RE_ENVASE_FORMA_NUM.search(t)
    if not m:
        return ""
    return m.group(1).lstrip("0") or m.group(1)


def _envase_de_nombre(nombre: object) -> str:
    """El tamaño de envase del título de un producto: '... X 30 CAP' → '30'.

    Dos trampas, las dos medidas en el caso conv 7 (provider 05):

    - La 'x' puede ser la ÚLTIMA LETRA del nombre, no un separador: en
      'evigax 125 mg' y 'milax 120 gr' la dosis (125, 120) se leía como envase y el
      cliente recibía "No tengo envase de 125 unidades". Se descarta la 'x' que va
      pegada a una letra, salvo forma compacta tras una unidad ('650MGX10' → 10).
    - Si el número lleva unidad de peso/volumen no es un envase: 'MILAX POLVO X 360
      GRAMOS' son gramos, no 360 cajas.
    """
    texto = str(nombre or "").lower()
    for m in _RE_X_ENVASE.finditer(texto):
        antes = texto[: m.start()]
        if antes and antes[-1].isalpha() and not _RE_UNIDAD_ANTES_DE_X.search(antes):
            continue
        if _RE_UNIDAD_DOSIS_TRAS_NUM.match(texto[m.end():]):
            continue
        return m.group(1).lstrip("0") or m.group(1)
    return ""


def _termino_sin_envase(termino: str, env: str) -> str:
    """Quita del término el número de ENVASE que un refinamiento anterior le pegó.

    POR QUÉ: `last_term` persiste el término de la búsqueda anterior, y el backstop de
    refinamiento lo COMPONE ('brasartan' + '10' → 'brasartan 80 mg 10'). Si el guard
    busca con ese término contaminado, el número de envase va camino del catálogo y puede
    devolver 0 productos — y entonces la respuesta se quedaría sin la parte más útil: en
    qué tamaños SÍ lo tenemos.

    Solo se quita el número si es EXACTAMENTE el envase pedido y va SUELTO al final (sin
    unidad): 'brasartan 80 mg 10' → 'brasartan 80 mg'. La dosis NO se toca: en
    'acetaminofen 650 mg' el 650 va con unidad y es lo que identifica el producto.
    """
    if not termino or not env:
        return termino
    limpio = re.sub(rf"\s+0*{re.escape(env)}\s*$", "", termino.strip())
    return limpio or termino


def _trae_medicamento_propio(user_text: str, last_term: str) -> bool:
    """True si el mensaje del cliente trae un MEDICAMENTO PROPIO (no solo una forma).

    POR QUÉ EXISTE: el backstop de refinamiento compone `term = last_term + refinamiento`
    para responder a "¿qué miligramo necesitas?". Eso es correcto cuando el cliente solo
    precisa la presentación ("de 20 mg", "tabletas 650", "el de calox"). Pero si el mensaje
    trae un fármaco NUEVO, es una CONSULTA NUEVA y usar el término viejo devuelve el producto
    equivocado.

    Caso real (conv 2287, provider 19):
        last_term = 'vivas'   (del turno anterior, donde el título fue 'VIVAS')
        cliente   = "Precio de airfen gotas ped"
        compuesto = 'vivas' + 'gotas' = 'VIVAS GOTAS'  → 5 productos VIVAX
    El cliente pedía AIRFEN (2 productos en el catálogo), que estaba en su propio mensaje.

    CÓMO: se extrae el término del mensaje, se le quitan las FORMAS de presentación y las
    palabras que YA están en `last_term`; si queda alguna palabra con cuerpo de fármaco, es
    un medicamento nuevo.
    """
    if not user_text:
        return False
    propio = _extraer_termino_medicamento(user_text) or ""
    if not propio.strip():
        return False
    # Palabras del mensaje que NO aportan un fármaco nuevo.
    # OJO: aquí van TODAS las formas y TODOS los tamaños de envase, porque el cliente que
    # solo precisa la presentación ("de 10 pastillas", "jarabe 120") está REFINANDO, no
    # consultando. Cobertura medida con el caso real de BRASARTAN (conv 2714): si 'pastillas'
    # no estuviera aquí, "No tienes de 10 pastillas?" quedaría bloqueado y el cliente con
    # envase de 30 volvería a recibir el de 30.
    formas = {
        # formas farmacéuticas
        "gotas", "jarabe", "jbe", "suspension", "susp", "crema", "gel", "spray",
        "tabletas", "tableta", "tab", "tabs", "comprimidos", "comprimido", "pastillas",
        "pastilla", "capsulas", "capsula", "cap", "caps", "cápsulas", "grageas", "perlas",
        "ampolla", "ampollas", "inyectable", "supositorio", "supositorios", "solucion",
        "pomada", "unguento", "sobres", "sobre", "polvo", "ovulos", "ovulo", "parche",
        "locion", "shampoo", "enema", "jalea", "colirio", "gotero", "spray nasal",
        # unidades de medida
        "mg", "ml", "mcg", "gr", "gramos", "gramo", "unidades", "unidad", "cajas",
        "caja", "tabletas.", "cc", "ui", "meq", "pct", "por",
        # audiencia / presentación comercial
        "pediatrica", "pediatrico", "pediatricas", "pediatricos", "ped", "pedi",
        "adulto", "adulta", "adultos", "adultas", "junior", "infantil", "nino", "nina",
        "forte", "plus", "duo", "retard", "grageas",
        # palabras funcionales
        "de", "del", "la", "el", "los", "las", "x", "para", "con", "sin",
    }
    previas = set(re.findall(r"[a-záéíóúüñ0-9]+", _normalizar_tildes((last_term or "").lower())))
    nuevas = [
        w for w in re.findall(r"[a-záéíóúüñ]{4,}", _normalizar_tildes(propio.lower()))
        if w not in formas and w not in previas
    ]
    return bool(nuevas)


def _es_refinamiento_presentacion(texto: str) -> bool:
    """True si el texto es un refinamiento de presentación ('30 mg', '50 mg',
    'gotas', 'jarabe', 'Tabletas 650') más que una nueva búsqueda de medicamento.

    Se delega en `_extraer_refinamiento` para que AMBAS funciones reconozcan los
    mismos casos: antes tenían regex separados y divergían — 'capsulas 500' daba
    '' en extracción pero tampoco era refinamiento, así que la dosis se perdía por
    los dos lados.
    """
    if not texto:
        return False
    t = texto.strip().lower()
    # Número + unidad de dosis/presentación (en cualquier orden).
    if re.search(
        r"\b\d+\s*(mg|ml|g|mcg|gotas|tabletas|tab|comprimidos|cápsulas|capsulas|cap)\b",
        t,
    ):
        return True
    if re.search(
        r"\b(mg|ml|mcg|gotas|tabletas|tab|comprimidos|cápsulas|capsulas|cap)\s*\d+\b",
        t,
    ):
        return True
    # PEDIDO DE ENVASE ('de 10 pastillas', 'tabletas 20'): también es precisar la
    # presentación, aunque el número sea un tamaño de caja y no una dosis.
    if _pedido_de_envase(t):
        return True
    # Número de dosis suelto ('de 650', 'las de 650'): respuesta a "¿qué miligramo
    # necesitas?" sin repetir la unidad.
    if re.search(r"\b\d{3,4}\b", t) and not re.search(
        r"\b(cajas?|unidades?|frascos?|paquetes?)\b", t
    ):
        return True
    # Presentación sin número.
    if re.search(
        r"\b(gotas|jarabe|jbe|tabletas|comprimidos|inyectable|ampolla|crema|ungüento)\b",
        t,
    ):
        return True
    return False


def _strip_internal_markup(texto: str) -> str:
    """Elimina el markup interno que el modelo pudo escribir como texto en vez
    de llamar la tool. NUNCA debe llegar al cliente.

    Cubre: `<handoff>...</handoff>`, `<function=handoff>{...}`, y cualquier
    `<handler=<tool>>{...}` o `<function=<tool>>{...}` (p. ej. el agente a veces
    escribe `<handler=agregar_al_carrito>{...} <function=ver_carrito>` como texto
    literal). Todo lo que parezca markup de tool-call se retira del texto final.
    """
    if not texto:
        return ""
    # Cualquier etiqueta <handoff>...</handoff>.
    limpio = re.sub(r"<handoff>.*?</handoff>", "", texto, flags=re.DOTALL | re.IGNORECASE)
    # Cualquier <handler=...> o <function=...> con su contenido hasta el cierre
    # (o hasta el fin si no cierra). Incluye tool-calls como agregar_al_carrito,
    # ver_carrito, etc.
    limpio = re.sub(
        r"<(?:\s*handler\s*=\s*|\s*function\s*=\s*)[a-z_]+[^>]*>.*?(?:</[^>]+>|$)",
        "",
        limpio,
        flags=re.DOTALL | re.IGNORECASE,
    )
    limpio = limpio.strip()
    # JSON de handoff suelto (sin etiquetas): descartarlo si es solo eso.
    if limpio.startswith("{") and '"reason"' in limpio:
        try:
            json.loads(limpio)
            return ""
        except Exception:
            pass
    return limpio


# Frases engañosas que el modelo a veces escribe cuando el medicamento NO está:
# el agente no tiene forma de \"consultar\" fuera del catálogo, así que ofrecerlo
# confunde al cliente. Se retiran del texto final y se fuerza el handoff.
_MENTIRA_CONSULTA = re.compile(
    r"¿?\s*[Qq]uieres\s+que\s+(?:te\s+lo|te)\s+(?:consulte|consiga|busque|averigüe)"
    r"(?:\s+(?:en\s+su\s+lugar|algo|en\s+otra\s+farmacia|después|más\s+tarde|desde\s+allá))?"
    r"\s*[?\.]?\s*",
)


def _quitar_ofrecimiento_consulta(texto: str) -> str:
    """Elimina '¿quieres que te lo consulte?' y similares del texto final."""
    if not texto:
        return texto
    return _MENTIRA_CONSULTA.sub("", texto).strip()


# Cierre tras agradecimiento. El lead dice "gracias" / "de nada" / "ok" / "hasta
# luego" y el agente le DEVUELVE una pregunta ("¿Quieres que busque alguno de los
# medicamentos que mencionaste?"). Casos reales (2026-10), medidos sobre 503
# mensajes del agente: 365 terminaban en pregunta.
#
#   cliente: "gracias"
#   agente : "¡De nada! Si necesitas algo más, no dudes en preguntar.
#             ¿Quieres que busque alguno de los medicamentos que mencionaste?"
#
# Repreguntar tras un agradecimiento suena a bot que no escucha y a presión de
# venta. El prompt YA lo prohíbe (sección CERRAR SIN REPREGUNTAR) — pero en este
# proyecto quedó demostrado que el prompt no es garantía: el arreglo que cuenta es
# el backstop en código.
#
# El marcador se busca AL FINAL del mensaje, no en cualquier parte: la gente cierra
# al final ("Ah, ok. Está bien, gracias"), mientras que un "ok" en medio suele
# anunciar una pregunta nueva ("ok, y cuánto sale el losartan?").
_CIERRE_DEL_LEAD = re.compile(
    r"(?:"
    r"gracias(?:\s+\w+){0,3}"
    r"|de\s+nada|ok(?:ay)?|listo|dale|perfecto|genial"
    r"|hasta\s+luego|nos\s+vemos|chao|adiós|adios|hasta\s+mañana|hasta\s+pronto"
    r"|buenas\s+noches|buen\s+d[ií]a|buenas\s+tardes|feliz\s+\w+"
    r"|bendiciones|am[eé]n|que\s+est[eé]s?\s+bien"
    r"|ya\s+me\s+atendi[oó]\s*\w*|ya\s+est[aá]\s+bien"
    r")\s*[.!¡]*\s*[\U0001F300-\U0001FAFF\u2600-\u27BF\u2764\uFE0F]*\s*$",
    re.IGNORECASE,
)

# Preguntas de reapertura que NO deben seguir a un cierre del lead.
_PREGUNTA_REAPERTURA = re.compile(
    r"(?:"
    r"quieres\s+que\s+(?:busque|te\s+busque|consulte|te\s+muestre|agregue)"
    r"|necesitas\s+(?:algo|algo\s+m[aá]s|informaci[oó]n)"
    r"|te\s+ayudo\s+(?:en\s+)?(?:algo|algo\s+m[aá]s)"
    r"|deseas\s+(?:algo|algo\s+m[aá]s|buscar|que\s+busque)"
    r"|hay\s+algo\s+m[aá]s"
    r"|algo\s+m[aá]s\s+en\s+lo\s+que\s+(?:te\s+)?pueda\s+ayudar"
    r"|te\s+comparto\s+m[aá]s\s+informaci[oó]n"
    r"|(?:te\s+)?gustar[ií]a\s+(?:hacer\s+un\s+pedido|agregarlo?|m[aá]s\s+informaci)"
    r"|qu[eé]\s+puedo\s+hacer\s+por\s+ti"
    r"|en\s+qu[eé]\s+(?:te\s+)?puedo\s+ayudar"
    r"|(?:te\s+)?ayudo\s+con\s+algo\s+m[aá]s"
    r"|puedo\s+ayudarte\s+en\s+algo\s+m[aá]s"
    r"|busco\s+alguno\s+de\s+los\s+medicamentos"
    r")",
    re.IGNORECASE,
)

# Una oración "es pregunta" si lleva '?' o abre con interrogativo. Hace falta este
# filtro ANTES de borrar: si no, una línea de cortesía legítima ("Si necesitas algo
# más, no dudes en preguntar.") matchea el patrón y se lleva por delante la mitad de
# la frase — quedaba "¡De nada! Simás, no dudes en preguntar.alguno de los
# medicamentos que mencionaste?".
_ABRE_PREGUNTA = re.compile(
    r"^\s*(?:¿|qu[eé]\b|cu[aá]l\b|c[oó]mo\b|cu[aá]ndo\b|d[oó]nde\b|qui[eé]n\b|"
    r"tienes\b|hay\b|puedes\b|podr[ií]as\b)",
    re.IGNORECASE,
)


def _es_repregunta(oracion: str) -> bool:
    """True si la oración repregunta al lead sobre algo que el agente ya resolvió."""
    o = (oracion or "").strip()
    if not o or not _PREGUNTA_REAPERTURA.search(o):
        return False
    return "?" in o or bool(_ABRE_PREGUNTA.match(o))


# Dónde EMPIEZA la pregunta dentro del texto: el final de la oración previa. El '¿' se
# EXCLUYE a propósito — de él se encarga el paso siguiente, que corta justo ahí para
# que no quede un '¿' huérfano colgando ("Perfecto, Milagros. ¿").
_INICIO_ORACION = re.compile(r"[.!?…\n]")


def _lead_esta_cerrando(texto: str | None) -> bool:
    """True si el mensaje del lead es un agradecimiento o una despedida.

    Se apoya en `_es_negativa_o_despedida` (ya probado: 30/30 en su batería) y le
    suma los cierres que ese helper no cubre ("ok", "listo", "buenas noches",
    "ya me atendió Bruli"). Los mensajes reales llegan con relleno delante — "Ah, ok.
    Está bien, gracias." — así que el marcador se busca al FINAL, no como mensaje
    completo: un "gracias" en medio suele anunciar una pregunta nueva ("gracias, me
    puedes decir el precio del atamel?").
    """
    if not texto:
        return False
    return bool(_es_negativa_o_despedida(texto) or _CIERRE_DEL_LEAD.search(texto))


# ── ANTI-REPETICIÓN DEL TEXTO FINAL ────────────────────────────────────────────────
# Dos frases que el modelo clava iguales turno tras turno y que el cliente lee como que no
# le están escuchando:
#
#   "¿Tienes el nombre exacto que está en la caja? O si prefieres, ¿busco otra presentación
#    o medicamento?"     ← se repite aunque ya se dijo que no hay nada
#   "La farmacia X está en … y trabaja 24 horas."   ← se repite aunque pregunten el TELÉFONO
#
# Caso real (provider 32, 2026-10-28 20:39-20:41), ver el docstring del módulo de la sesión.
_RE_PIDE_NOMBRE_CAJA = re.compile(
    r"(nombre\s+exacto|nombre\s+que\s+(?:est[áa]|aparece|dice|tiene)\s+en\s+la\s+caja|"
    r"otra\s+presentaci[oó]n\s+o\s+medicamento|busco\s+otra\s+presentaci[oó]n)",
    re.IGNORECASE,
)
_RE_DIRECCION_HORARIO = re.compile(
    r"la\s+farmacia[^.\n]{0,90}?(?:est[áa]|queda|se\s+encuentra|atiende)\b[^.\n]{0,140}?"
    r"(?:trabaja|atiende|abre|cierra|24\s*horas)",
    re.IGNORECASE,
)
_RE_CLIENTE_UBICACION = re.compile(
    r"\b(d[oó]nde|ubicaci[oó]n|direcci[oó]n|horario|a\s+qu[eé]\s+hora|abren|cierran|"
    r"24\s*horas|queda|sucursal|local)\b",
    re.IGNORECASE,
)
_RE_CLIENTE_TELEFONO = re.compile(
    # 'tel[eé]fon\w*' cubre teléfono Y telefónico ("el número telefónico", como escribió el
    # cliente del caso real: sin esto el fallback no reconocía la pregunta).
    r"\b(tel[eé]fon\w*|n[uú]mero\s+(?:de\s+|del\s+)?(?:contacto|tel[eé]fon\w*|la\s+farmacia)|"
    r"contactarlos?|llamarlos?|whatsapp\s+de\s+la\s+farmacia)\b",
    re.IGNORECASE,
)

FALLBACK_TELEFONO = (
    "No manejo el teléfono de la farmacia desde aquí. 🙏 Si quieres, te paso con un humano."
)
FALLBACK_PENDIENTE = "Quedo pendiente. ¿Te ayudo con algo más?"


def _quitar_frases(texto: str, patron: re.Pattern[str]) -> str:
    """Quita las frases (separadas por . ! ? o salto) que casan con el patrón."""
    trozos = re.split(r"(?<=[.!?])\s+|\n{2,}", texto)
    quedan = [x for x in trozos if x and not patron.search(x)]
    return "\n\n".join(x.strip() for x in quedan if x.strip()).strip()


def _texto_del_bot(history: list[Any], cuantos: int = 2) -> str:
    """Los últimos mensajes del BOT del historial, unidos."""
    previos = [
        str(m.content or "") for m in history
        if getattr(m, "role", "") == "assistant" and (m.content or "").strip()
    ]
    return "\n\n".join(previos[-cuantos:])


_RE_AVISO_DOSIS_NEGADA = re.compile(
    r"no\s+tengo\s+de\s+(\d+(?:[.,]\d+)?\s*(?:mg|mcg|g|ml|ui))", re.IGNORECASE
)
_RE_LINEA_DE_LISTA = re.compile(r"💊|\$|\bBs\b|^(?:👉|🛒|✅|⚠️|Ejemplo)", re.IGNORECASE)


def _grupo_ya_en_carrito(prods: list[dict[str, Any]], runtime: Any) -> bool:
    """¿Alguno de estos productos lo agregó el backstop por elección de opción?"""
    skus = getattr(runtime, "backstop_added_skus", None)
    if not skus:
        return False
    for p in prods or []:
        sku = str(p.get("productId") or p.get("producto") or "")
        if sku and sku in skus:
            return True
    return False


_RE_PREGUNTA_POR_SEPARADO = re.compile(
    r"(?i)(?:\bsolo\b|\bsolamente\b|\bnada\s+m[aá]s\b|\bno\s+m[aá]s\b)"
    r".{0,40}\b(?:separado|separada|separados|separadas|suelto|suelta|sueltos|sueltas|"
    r"individual|individuales|aparte)\b"
    r"|\bpor\s+separado\b"
    r"|\blo\s+tienes\s+suelto\b|\blos\s+tienes\s+sueltos\b",
)


def _pregunta_por_separado(texto: str) -> bool:
    """¿Pregunta si el producto se vende por partes («¿solo lo tienes por separado?»)?"""
    return bool(_RE_PREGUNTA_POR_SEPARADO.search(texto or ""))


def _respuesta_por_separado(producto: dict[str, Any]) -> str:
    """Contesta SIN contradecir la lista anterior: el producto ES uno solo.

    Caso real (provider 32, 2026-10-29): después de cotizar el FIN-AL-GRIP FORTE, el cliente
    preguntó "¿Solo lo tienes por separado?" y el agente respondió "No tengo Fin-Al-Grip FORTE
    como un solo producto. Te puedo buscar los ingredientes por separado" — dos líneas después
    de haberle dado el precio del producto.
    """
    titulo = str(producto.get("producto") or producto.get("nombre") or "").upper()
    # SIN `_lista_con_aviso`: con el nombre completo del producto como término, el aviso de
    # "lo que no tengo" se disparaba con el LABORATORIO del propio título
    # ("⚠️ No tengo DROTAFARMA. De FIN-AL-GRIP FORTE X 10TAB hay esto:").
    return (
        "No hace falta: es un solo producto, no hay que comprarlo por partes.\n\n"
        + _formatear_lista_productos([producto], titulo)
        + "\n\n"
        + MENSAJE_SUGERIDO_CARRITO
    )


def _con_confirmacion_carrito(texto: str, runtime: Any) -> str:
    """Antepone lo que el backstop agregó al carrito si la respuesta no lo dice.

    El cliente pidió DOS cosas ("precio de la macrodantina" + "quiero la opción 9") y la
    respuesta solo traía la lista de precios: el producto se agregó en silencio.
    """
    confs = list(getattr(runtime, "cart_confirmaciones", None) or [])
    if not confs:
        return texto
    # OJO con la guarda: el cierre canónico dice "👉 Para agregar al carrito…", así que
    # buscar 'carrito' + 'agreg' daba positivo SIEMPRE y la confirmación nunca se ponía.
    # OJO también con el ✅ suelto: el cierre canónico dice "✅ Cuando termines, escribe
    # LISTO…", así que un ✅ cualquiera tampoco vale como confirmación.
    if re.search(
        r"(?i)(?:✅\s*(?:agregad|listo,?\s*agreg)|he\s+agregado|agregu[eé]\s+al|"
        r"agregad[oa]s?\s+(?:al?\s+)?(?:carrito|pedido)|"
        r"(?:carrito|pedido)\s+(?:actualizado|con))",
        texto or "",
    ):
        return texto
    if len(confs) == 1:
        cabecera = "✅ Agregado al carrito: " + confs[0].lstrip("• ")
    else:
        cabecera = "✅ Agregado al carrito:\n" + "\n".join(confs)
    logger.info("confirmación de carrito antepuesta (%d)", len(confs))
    return cabecera + "\n\n" + (texto or "")


def _titulo_sin_dosis_negada(texto: str) -> str:
    """Quita del ENCABEZADO la concentración que el propio mensaje acaba de negar.

    El aviso y el título pueden venir de caminos distintos (tool → backstop, o el LLM
    copiando el formato), así que la corrección se hace sobre el TEXTO FINAL: si el mensaje
    dice "No tengo de 20 mg" y su primera línea de encabezado es "RIVAROXABAN 20 MG", se
    queda en "RIVAROXABAN".

    Caso real (provider 32, 2026-10-29, foto del RIVAROXABAN 20 mg DISTRILAB):
        RIVAROXABAN 20 MG
        ⚠️ No tengo de 20 mg. Lo tengo en: 15 mg.
    """
    m = _RE_AVISO_DOSIS_NEGADA.search(texto or "")
    if not m:
        # CASO 2: sin aviso previo, pero el encabezado lleva una concentración que NINGÚN
        # producto de la lista tiene. Caso real (provider 32, 2026-10-29): el LLM tituló
        # "RIVAROXABAN 20 MG" sobre dos presentaciones de 15 mg, sin avisar de nada.
        lineas_txt = (texto or "").splitlines()
        # El texto de los PRODUCTOS son las líneas de la lista (💊 / precio). Todo lo demás
        # es encabezado o prosa.
        productos = " ".join(
            ln for ln in lineas_txt if _RE_LINEA_DE_LISTA.search(ln.strip())
        ).lower().replace(" ", "")
        if not productos:
            return texto
        for i, ln in enumerate(lineas_txt):
            s = ln.strip()
            if not s or _RE_LINEA_DE_LISTA.search(s):
                continue
            dosis_enc = _RE_DOSIS_ENVASE.findall(s)
            if not dosis_enc:
                continue  # esta línea no lleva concentración: es prosa
            for num, unidad in dosis_enc:
                num_n = num.replace(",", ".")
                clave = f"{num_n}{unidad.lower()}"
                if clave not in productos:
                    logger.info(
                        "título sin dosis inexistente: quito %s %s de %r",
                        num, unidad, s[:40],
                    )
                    lineas_txt[i] = _RE_DOSIS_ENVASE.sub("", s).strip(" -–,:.*")
                    return "\n".join(lineas_txt)
            return texto  # la primera línea con dosis SÍ existe en la lista
        return texto
    dosis = m.group(1).strip()
    num = (re.match(r"[\d.,]+", dosis) or re.match(r"", dosis)).group(0)
    if not num:
        return texto
    patron = re.compile(
        rf"\s*\b{re.escape(num)}\s*(?:mg|mcg|g|ml|ui)\b", re.IGNORECASE
    )
    lineas = texto.splitlines()
    for i, ln in enumerate(lineas):
        s = ln.strip()
        if not s or _RE_LINEA_DE_LISTA.search(s):
            continue
        if patron.search(s):
            lineas[i] = patron.sub("", s).strip(" -–,:.")
            logger.info(
                "título sin la dosis negada: %r → %r", s[:40], lineas[i][:40]
            )
            break
    return "\n".join(lineas)


def _quitar_repeticiones(texto: str, user_text: str, history: list[Any]) -> str:
    """No repetir lo que ya se dijo y no colar la dirección cuando no la pidieron."""
    if not texto:
        return texto
    previo = _texto_del_bot(history)
    cliente = _texto_cliente_sin_marcadores(user_text) or user_text or ""

    # 1) La MISMA pregunta de siempre (nombre de la caja / otra presentación).
    if _RE_PIDE_NOMBRE_CAJA.search(texto) and _RE_PIDE_NOMBRE_CAJA.search(previo):
        logger.info("anti-repetición: quito la pregunta del nombre de la caja (ya se hizo)")
        texto = _quitar_frases(texto, _RE_PIDE_NOMBRE_CAJA)

    # 2) Dirección/horario cuando NO los pidieron y ya se dieron antes.
    if (
        _RE_DIRECCION_HORARIO.search(texto)
        and not _RE_CLIENTE_UBICACION.search(cliente)
        and _RE_DIRECCION_HORARIO.search(previo)
    ):
        logger.info("anti-repetición: quito la dirección/horario (ya se dio y no la pidieron)")
        texto = _quitar_frases(texto, _RE_DIRECCION_HORARIO)

    if _RE_PIDE_NOMBRE_CAJA.search(texto) or _RE_DIRECCION_HORARIO.search(texto):
        texto = texto.strip()
    if texto and re.search(r"[A-Za-zÁÉÍÓÚÑáéíóúñ]{3,}", texto):
        return texto
    # Se quedó vacío: se responde a lo que SÍ preguntó, sin muletillas.
    if _RE_CLIENTE_TELEFONO.search(cliente):
        return FALLBACK_TELEFONO
    return FALLBACK_PENDIENTE


# ── «NO ES EL MEDICAMENTO QUE PEDISTE» ─────────────────────────────────────────────
# Caso real (provider 32, 2026-10-28 20:59): el cliente pidió ESPIRAMICINA, el buscador no
# devolvió nada y el modelo "asumió" ESPIRONOLACTONA y cotizó diez presentaciones del diurético
# como si fuera el antibiótico. Dos fármacos distintos: eso no es un typo, es una dispensación
# equivocada.
#
# El umbral se compara sobre el NÚCLEO del término (la primera palabra significativa) con la
# misma medida que `tools._UMBRAL_NUCLEO_MISMO_FARMACO`: por encima de 0.75 se considera el
# mismo fármaco escrito de otra forma ('atamle'→'atamel', 'omeprasol'→'omeprazol').
def _farmaco_distinto(user_text: str, runtime: Any) -> str | None:
    """El término del cliente si lo que se cotizó es OTRO fármaco. None si todo cuadra."""
    if getattr(runtime, "busqueda_por_principio", False):
        return None  # marca → principio activo: cambio legítimo
    term_buscado = str(getattr(runtime, "last_term", "") or "").strip()
    if not term_buscado or not getattr(runtime, "last_products", None):
        return None  # sin productos cotizados no hay nada que corregir
    cliente = _extraer_termino_medicamento(
        _texto_cliente_sin_marcadores(user_text) or user_text or ""
    )
    if not cliente or not _termino_es_medicamento_plausible(cliente):
        return None
    if _es_solo_saludo(cliente) or _es_solo_saludo(cliente.split()[0]):
        return None  # 'gracias muy amable' no es un fármaco
    # Se comparan las PALABRAS CON CONTENIDO, y basta con que UNA pareja se parezca: antes
    # se miraba solo la primera palabra, así que 'venta aceite oregano' (el cliente dijo
    # "tienen a la venta…") contra 'aceite oregano' daba 'venta' vs 'aceite' y el guard
    # concluía que era OTRO fármaco cuando era el mismo producto.
    _FUNC_COMP = set(_FILLER) | set(_VIAS_ADMINISTRACION) | set(_CATEGORIAS_TERAPEUTICAS)
    cli_palabras = [
        w for w in re.findall(r"[a-záéíóúüñ0-9]+", cliente.lower())
        if len(w) >= 3 and w not in _FUNC_COMP
    ] or (cliente.split() or [""])
    bus_palabras = [
        w for w in re.findall(r"[a-záéíóúüñ0-9]+", term_buscado.lower())
        if len(w) >= 3 and w not in _FUNC_COMP
    ] or (term_buscado.split() or [""])
    ratio = max(
        (
            difflib.SequenceMatcher(None, a, b).ratio()
            for a in cli_palabras
            for b in bus_palabras
        ),
        default=0.0,
    )
    if ratio >= 0.75:
        return None
    logger.info(
        "otro fármaco: el cliente pidió '%s' y se cotizó '%s' (similitud %.2f) — se corrige",
        cliente, term_buscado, ratio,
    )
    return cliente


def _respuesta_no_es_ese_farmaco(cliente: str) -> str:
    return (
        f"⚠️ No tengo {cliente.upper()}. No te ofrezco otro medicamento parecido para no "
        "confundirte: son cosas distintas.\n"
        "¿Me confirmas el nombre tal como viene en la caja, o te paso con un auxiliar?"
    )


def _quitar_repregunta_tras_cierre(texto: str, ultimo_del_lead: str | None) -> str:
    """Quita la repregunta si el lead acaba de agradecer o despedirse.

    Se CORTA DESDE EL INICIO DE LA PREGUNTA hasta el final, no se borra la frase que
    matchea. Borrar solo el fragmento rompía la cortesía que lo precedía —
    "¡De nada! Si necesitas algo más, no dudes en preguntar. ¿Quieres que busque…?"
    quedaba como "¡De nada! Simás, no dudes en preguntar.alguno de los
    medicamentos…?", que es peor que la repregunta original.

    El corte es por ORACIÓN, no por palabra, porque la cortesía y la pregunta suelen
    vivir en la MISMA oración: "De nada 😊 ¿Necesitas algo más en lo que pueda
    ayudarte?" (un solo bloque, sin punto intermedio).

    Si el mensaje del lead no es un cierre, o si al quitar la pregunta no queda nada,
    devuelve el texto intacto: borrar una respuesta legítima o dejar al cliente sin
    mensaje es peor que la repregunta.
    """
    if not texto or not ultimo_del_lead:
        return texto
    if not _lead_esta_cerrando(ultimo_del_lead):
        return texto

    # Se recorre cada repregunta y se calcula dónde arranca su oración. Se toma la
    # MÁS TEMPRANA: así cae también el "¿Algo más?" que venga detrás.
    corte: int | None = None
    for m in _PREGUNTA_REAPERTURA.finditer(texto):
        inicio = 0
        for b in _INICIO_ORACION.finditer(texto, 0, m.start()):
            inicio = b.end()
        # Retrocede hasta el '¿' que abre la pregunta (si lo hay): sin esto queda un
        # "Perfecto, Milagros. ¿" colgando, que el cliente ve como un error.
        hueco = texto[inicio:m.start()]
        interrogante = hueco.rfind("¿")
        if interrogante != -1:
            inicio = inicio + interrogante
        # Solo si desde ahí hasta el final hay una pregunta de verdad.
        cola = texto[inicio:]
        if "?" not in cola:
            continue
        corte = inicio if corte is None else min(corte, inicio)
    if corte is None:
        return texto
    limpio = texto[:corte].strip().rstrip("¿¡,;:—-")
    if not limpio:
        # El mensaje ERA solo la pregunta: quitarlo dejaría al cliente sin respuesta.
        return texto
    return limpio


# Frases redundantes del LLM que invitan al carrito de forma libre y duplican
# el MENSAJE_SUGERIDO_CARRITO estándar que se adjunta al final ("Si deseas
# agregarlo a tu carrito, solo indícame cuántas cajas quieres. 🛒"). Se retiran
# para que el cliente vea UNA sola instrucción de carrito (la canónica).
_INVITO_CARRITO = re.compile(
    r"""\s*[Ss]i\s+deseas\s+agregarlo?\s+(?:al\s+carrito|a\s+tu\s+carrito)\s*[,.:]?\s*(?:solo\s+)?[Ii]nd[ií]came\s+(?:cu[aá]ntas\s+cajas\s+quieres|la\s+cantidad|cu[aá]ntas\s+cajas)[^\n]*?\n?\s*""",
    re.X,
)


def _quitar_invito_carrito(texto: str) -> str:
    """Elimina frases redundantes de invitación al carrito del texto final."""
    if not texto:
        return texto
    
    new_text = _INVITO_CARRITO.sub("", texto)
    # Casos de variantes que no cayeron en el patrón exacto (emojis o redactado
    # distinto): retira cualquier línea final que mencione "cuántas cajas" y
    # "carrito" a la vez (invitación libre que se duplica con el bloque).
    lines = new_text.split("\n")
    lines = [
        l
        for l in lines
        if not (
            "cuántas cajas" in l.lower()
            and "carrito" in l.lower()
            and "opción" not in l.lower()
        )
    ]
    out = "\n".join(lines).strip()
    return out


def _quitar_pie_carrito_duplicado(texto: str) -> str:
    """Elimina las copias del pie de carrito que generó el LLM, dejando el texto
    listo para que el backstop adjunte UNA sola vez el bloque canónico.

    El LLM imita el pie del historial y lo escribe por su cuenta, a veces 2-3
    veces y con VARIANTES: cambia el número de la opción, y sobre todo la tercera
    línea ("¿Necesitas buscar otro medicamento?" en vez de "¿Otro medicamento?
    Escríbeme el nombre y lo busco."). Un patrón que exija las CUATRO líneas
    literales y contiguas falla con esas variantes; peor aún, puede borrar el pie
    CANÓNICO y dejar la variante, y entonces el backstop adjunta el canónico otra
    vez → el cliente ve DOS pies (bug reportado con "Budecort").

    Por eso se ancla en el INICIO del pie (la línea "👉 Para agregar al carrito…",
    que el LLM reproduce casi literal) y se corta desde ahí hasta el final: lo que
    venga después del último producto es el pie (o pies) que el propio modelo
    escribió, y el backstop repone el canónico.
    """
    if not texto:
        return texto
    # Se eliminan los BLOQUES de pie (no "todo lo que sigue"), para que un pie
    # escrito por el LLM ANTES de la lista no se lleve la lista por delante.
    # Un bloque del pie son líneas consecutivas que empiezan con 👉 / Ejemplo: /
    # 🛒 / ✅ (y las líneas en blanco entre ellas). Se corta al aparecer una línea
    # que no pertenece al pie (p. ej. "💊 1. BUDECORT...").
    es_pie = re.compile(r"^\s*(?:👉|Ejemplo:|🛒|✅)", re.IGNORECASE)
    lineas = texto.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lineas):
        linea = lineas[i]
        if es_pie.match(linea):
            # Saltar el bloque entero (incluidas líneas vacías y pies pegados).
            while i < len(lineas) and (es_pie.match(lineas[i]) or not lineas[i].strip()):
                # Una línea vacía solo se salta si aún queda pie por delante.
                if not lineas[i].strip():
                    j = i
                    while j < len(lineas) and not lineas[j].strip():
                        j += 1
                    if j < len(lineas) and es_pie.match(lineas[j]):
                        i = j
                        continue
                    break
                i += 1
            continue
        out.append(linea)
        i += 1
    # Colapsar saltos de línea múltiples que quedan al quitar el pie.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# Un nombre de PRODUCTO escrito pelado ("Shampo Dreene", "Dreene", "Atamel") es
# una consulta aunque no traiga verbo. Los clientes escriben así todo el tiempo:
# en el caso real, "Shampo Dreene" (sin verbo) no disparaba el backstop → el LLM
# contestaba de memoria "No tengo información sobre el shampoo Dreene", mientras
# que "Drene" (una palabra) o "tienes dreene" sí buscaban. Es la MISMA consulta.
_SALUDOS_CONSULTA = {
    "hola", "buenas", "buenos", "buena", "dia", "dias", "tardes", "noches",
    "gracias", "saludos", "epa", "hey", "que", "tal", "como", "estas", "esta",
    "quien", "donde", "cuando", "hora", "horario", "ubicacion", "direccion",
    "si", "no", "ok", "okay", "listo", "claro", "dale", "por", "favor",
    "algo", "mas", "otro", "otra", "nada", "eso", "este", "buen",
}


def _parece_nombre_producto(texto: str) -> bool:
    """True si el texto parece el NOMBRE de un producto escrito pelado.

    Criterio deliberadamente estrecho para no forzar búsquedas con basura:
    pocas palabras, todas "de nombre" (letras, sin signos de pregunta ni
    conjunciones largas) y al menos una suficientemente larga para ser marca o
    fármaco. 'Shampo Dreene' → True. '2 cajas' → False (número). 'que tal' →
    False (saludo). 'no tengo información' → False (frase verbal).
    """
    if not texto:
        return False
    t = texto.strip()
    if not t or len(t) > 60:
        return False
    # Una pregunta explícita no entra aquí: la maneja el verbo.
    if "?" in t or "¿" in t:
        return False
    palabras = re.findall(r"[a-záéíóúüñ]+", t.lower())
    # 1 o 2 palabras: el caso típico de "Dreene" / "Shampo Dreene". 3-4 se
    # aceptan solo si TODAS parecen de nombre (ver abajo).
    if not palabras or len(palabras) > 4:
        return False
    # CANTIDAD de un pedido, no un nombre: "2 cajas", "1 frasco". El agente
    # pregunta "¿cuántas cajas?" y esa respuesta NO debe consultar el catálogo.
    # Se exige número + palabra de envase (un nombre de producto no lleva
    # "cajas"/"unidades"), así "ATORVASTATINA 80 MG" sigue siendo válido.
    if re.search(r"\d", t) and any(
        w in {"cajas", "caja", "unidades", "unidad", "frascos", "frasco",
              "blisters", "blister", "paquetes", "paquete", "docenas"}
        for w in palabras
    ):
        return False
    # Referencia a una opción de la lista ya mostrada ("la opcion 3", "opción
    # 2"): el agente ya consultó el catálogo, no hay que volver a buscar. Sin
    # este guard, el nombre "opcion" (6 letras) pasaría el filtro de cuerpo.
    if any(w in {"opcion", "opciones", "numero", "alternativa"} for w in palabras):
        return False
    # Un número suelto tampoco es un medicamento.
    if t.replace(",", "").replace(".", "").isdigit():
        return False
    # Ninguna palabra de relleno/saludo puede estar: si aparece una, es frase.
    for w in palabras:
        if w in _SALUDOS_CONSULTA:
            return False
    # Debe haber al menos una palabra con cuerpo (>=5 letras): marca o fármaco.
    # Evita disparar con "la de", "dos mg" o interjecciones cortas.
    if not any(len(w) >= 5 for w in palabras):
        return False
    # Y ninguna palabra puede ser un verbo de consulta/acción conocido: si lo
    # fuera, `_parece_consulta_medicamento` ya devolvió True antes (o es otra
    # intención, p. ej. "quiero dos").
    return True


def _parece_consulta_medicamento(texto: str) -> bool:
    if not texto:
        return False
    t = texto.strip().lower()
    if not t:
        return False
    # Un verbo de consulta de medicamento ("busco", "tienes", "necesito",
    # "estoy buscando", ...) indica búsqueda, incluso si el texto menciona
    # presentación ("10 tabletas") o cifras — eso describe el producto, no es
    # una cantidad pedida.
    if _VERBOS_MEDICAMENTO.search(t) or "medicamento" in t:
        return True
    # Sin verbo: un NOMBRE de producto pelado también es una consulta. Antes se
    # exigía verbo, así que "Shampo Dreene" no la disparaba y el LLM negaba de
    # memoria ("No tengo información sobre el shampoo Dreene") pese a que el
    # catálogo SÍ lo tiene. "2 cajas" (cantidad) o "que tal" (saludo) siguen
    # fuera, que es lo que este guard debe proteger.
    return _parece_nombre_producto(texto)


def _build_state_block(
    conv: Conversation,
    cart: list[CartItem],
    respuesta_cliente: str | None = None,
    pidio_precisar: bool = False,
) -> str:
    """Resumen de estado determinista inyectado en el system prompt.

    Le dice al LLM exactamente en qué fase está la conversación y qué datos
    reales hay (producto consultado, carrito), para que no invente contexto
    entre turnos. Esto contiene el no-determinismo del flujo encadenado.

    `pidio_precisar`: el turno anterior del agente pidió precisar la búsqueda
    ("¿qué miligramo necesitas?"). Con eso, la respuesta corta del cliente
    ("Tabletas 650", "el de calox") se trata como REFINAMIENTO de la lista ya
    mostrada, no como una consulta nueva — que es lo que provocaba que el agente
    volviera a listar las 20 presentaciones mezcladas.
    """
    lines: list[str] = ["ESTADO ACTUAL DE ESTA CONVERSACIÓN (dato real, no inventar):"]

    # Fase
    fase_map = {
        "descubrimiento": "inicial — el cliente aún no ha elegido medicamento",
        "insight": "explorando opciones",
        "agendando": "armando pedido",
        "cerrada": "conversación cerrada",
        "salida": "despedida en curso",
    }
    fase = fase_map.get(conv.phase, conv.phase)
    lines.append(f"- Fase: {fase}.")

    # Último producto consultado
    if conv.last_product and isinstance(conv.last_product, dict):
        prod = conv.last_product
        nombre = prod.get("producto") or prod.get("title") or ""
        precio = prod.get("precio") or prod.get("precioUsd")
        if nombre:
            precio_str = f" (${precio})" if precio else ""
            lines.append(f"- Último producto que el cliente vio: {nombre}{precio_str}.")
            lines.append("  Si el cliente responde con una cantidad (un número), llama agregar_al_carrito con este producto.")

    # Último término buscado
    if conv.last_term:
        lines.append(f"- Última búsqueda de medicamento: '{conv.last_term}'.")

    # REFINAMIENTO EN CURSO: el turno anterior TÚ preguntaste por la dosis, marca
    # o presentación y el cliente acaba de responder. Sin esta nota, el modelo
    # lee la respuesta como un mensaje suelto y vuelve a listar todo.
    if pidio_precisar and conv.last_term:
        lines.append(
            "- ATENCIÓN — REFINAMIENTO EN CURSO: en tu mensaje anterior pediste "
            "precisar la búsqueda y el cliente acaba de responder. Su respuesta "
            f"NO es una consulta nueva: está acotando la búsqueda '{conv.last_term}' "
            "que ya hiciste."
        )
        if respuesta_cliente:
            lines.append(f"  Su respuesta: \"{respuesta_cliente}\".")
        lines.append(
            "  Vuelve a llamar buscar_medicamento con un término que JUNTE el "
            f"medicamento y el dato nuevo (p. ej. '{conv.last_term} 650 mg'), NO "
            "vuelvas a buscar solo el medicamento ni muestres la lista completa "
            "otra vez. Si el dato nuevo es una marca, un tamaño de caja o el "
            "precio, filtra la lista que ya mostraste y presenta solo las "
            "opciones que encajan."
        )

    # Carrito
    if cart:
        items_str = "; ".join(f"{it.producto} x{it.cantidad}" for it in cart)
        total = sum((it.precio_usd or 0) * it.cantidad for it in cart)
        lines.append(f"- Carrito actual: {items_str}. Total parcial: ${total:.2f}.")

    # MÉTODO DE ENTREGA. Se le dice al modelo en qué punto del paso va, para que no
    # invente la entrega ni contradiga lo que ya eligió el cliente.
    if conv.delivery_pending == "method":
        lines.append(
            "- ENTREGA: le preguntaste cómo quiere recibir el pedido (1. Delivery / "
            "2. Retirar en Farmacia) y aún NO responde. No avances al resumen."
        )
    elif conv.delivery_pending == "address":
        lines.append(
            "- ENTREGA: el cliente eligió DELIVERY y le pediste la dirección; aún no "
            "la dio. No avances al resumen."
        )
    elif conv.delivery_method == "delivery":
        dir_ = conv.delivery_address or "(sin dirección registrada)"
        lines.append(f"- ENTREGA: DELIVERY a '{dir_}'. Ya está registrada.")
    elif conv.delivery_method == "pickup":
        lines.append("- ENTREGA: RETIRAR EN FARMACIA. Ya está registrado.")

    # Si no hay estado relevante (sin producto, término ni carrito), no inyectar
    if (
        not conv.last_product
        and not conv.last_term
        and not cart
        and not conv.delivery_method
        and not conv.delivery_pending
    ):
        return ""

    lines.append("Usa este estado para responder con coherencia. NO inventes productos, precios ni cantidades que no estén aquí.")
    return "\n".join(lines)


def _es_despedida_o_handoff(texto: str) -> bool:
    """True si el texto es una despedida o pase a humano ('adiós', 'hablar con
    un humano', 'puedo pasarte con alguien', etc.) — cuando hay productos
    disponibles, esto es un handoff injustificado."""
    if not texto:
        return False
    t = texto.lower()
    despedidas = (
        r"adi[óo]s|hablar\s+con\s+(?:un|una)\s+(?:humano|persona)|"
        r"pas(?:e|ar)\s+(?:tu\s+)?(?:pregunta|consulta)?\s*(?:con|a)\s+(?:un\s+)?humano|"
        r"puedo\s+pasarte|te\s+lo\s+pas(?:e|amos)?|"
        r"alguien\s+te\s+ayud|un\s+profesional|"
        r"pasa\s+a\s+hablar"
    )
    if re.search(despedidas, t):
        return True
    return False


def _niega_disponibilidad(texto: str) -> bool:
    """True si el texto niega disponibilidad de un medicamento ('no tengo',
    'no tenemos', 'no está disponible', 'no lo tenemos', 'agotado', etc.)."""
    if not texto:
        return False
    t = texto.strip().lower()
    negaciones = (
        r"no\s+(?:tengo|tenemos|tenéis|tiene|tienen|hay|está|esta|estan|están|"
        r"lo\s+tenemos|lo\s+tengo|disponemos|contamos|encuentro|encontramos|"
        r"existe|existen)"
        r"|no\s+(?:está|esta|estan|están)\s+disponible"
        r"|no\s+tiene\s+(?:información|ese|este|ese\s+medicamento|el)"
        r"|sin\s+(?:stock|existencia|disponibilidad)"
        r"|agotado|agotada|agotados"
        r"|no\s+disponible"
    )
    return bool(re.search(negaciones, t))


def _parsear_precio(texto: str) -> float | None:
    """Convierte '$4,10' (Venezuela), '$4.10' o '$4' a float. Devuelve None si
    no es un número plausible."""
    s = texto.strip().replace(" ", "")
    # Venezuela: "4,10" → punto decimal. Estándar: "4.10" → punto decimal.
    # Distinguir "1.234,56" (miles con punto) de "4.10" (decimal con punto).
    if "," in s:
        # Tratar coma como decimal: quitar puntos de miles si existen.
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _lista_desordenada(texto: str, products: list[dict[str, Any]]) -> bool:
    """True si el texto enumera TODOS los precios reales de la consulta pero
    en un ORDEN distinto al canónico (precio ascendente).

    Caso real: el LLM mostró las 5 presentaciones de ATAMEL sin ordenar por
    precio (3,70 / 4,11 / 4,83 / 4,16 / 2,22). El cliente elige "1" contra la
    lista que VIO (ATAMEL FORTE $3,70), pero el backstop de carrito resuelve
    "opción 1" contra la lista ordenada por precio ($2,22) → agrega el
    producto equivocado. La solución robusta: jamás dejar salir una lista
    desordenada — se reemplaza por la canónica (precio ascendente) antes de
    enviar, así el número que el cliente ve SIEMPRE coincide con last_options.
    """
    if not texto or len(products) < 2:
        return False
    # Precios reales en orden canónico (precio ascendente).
    precios_ordenados = [
        round(float(p.get("precio") or 0), 2)
        for p in sorted(
            products,
            key=lambda p: (p.get("precio") if isinstance(p.get("precio"), (int, float)) else 0),  # type: ignore[arg-type,return-value]
        )
        if p.get("precio")
    ]
    if len(precios_ordenados) < 2:
        return False
    # Precios USD citados en el texto, en el orden de aparición.
    citados = []
    for m in re.finditer(r"\$\s*([0-9][0-9.,]*)", texto):
        v = _parsear_precio(m.group(1))
        if v is not None and v not in precios_ordenados:
            return False  # cita un precio inventado → lo maneja otro backstop
        if v is not None and (not citados or citados[-1] != v):
            citados.append(v)
    # Debe citar TODOS los precios reales (es la lista completa desordenada,
    # no una mención suelta de uno).
    if len(citados) < len(precios_ordenados):
        return False
    return citados != precios_ordenados


def _formato_no_canonico(texto: str, products: list[dict[str, Any]]) -> bool:
    """True si el texto enumera los productos (≥2) pero SIN el formato canónico
    del catálogo: '💊 N. NOMBRE' + precio en línea aparte con USD | Bs.

    Caso real: el LLM respondió con markdown propio ('1. *ATAMEL X 60 ML...*
    - $2.22' con punto decimal, sin 💊, sin Bs). El orden y los precios eran
    correctos → ningún backstop previo actuó, pero el formato viola el estándar
    de presentación (precio venezolano coma decimal + Bs + emoji por opción) y
    el usuario lo espera fijo.

    DEBE ENUMERAR ALGO. Este backstop SUSTITUYE el texto por la lista del catálogo,
    así que solo tiene sentido si el texto ERA una enumeración mal formateada. Antes
    bastaba con que hubiera 2+ productos y el texto no tuviera 💊: una CORTESÍA
    ("¡Con gusto! Que te vaya bien 😊") cumplía ambas condiciones y era reemplazada
    por el catálogo completo. Caso real (conv 2826): el cliente se despidió con "Ok
    gracias pasaré por allá" y recibió NAPROXENO otra vez — la misma lista que ya
    había visto, en lugar del adiós.
    """
    if not texto or len(products) < 2:
        return False
    # El texto tiene que estar ENUMERANDO: varias líneas, o referencias a los
    # productos/precios. Un texto de una sola frase sin ninguna mención no es una
    # enumeración mal formateada, es otra cosa (una cortesía, una pregunta).
    if not _enumera_productos(texto, products):
        return False
    if "💊" not in texto:
        return True
    # Tiene 💊 pero puede faltar el precio en línea aparte (USD | Bs).
    # Canónico: cada opción es '💊 N. ...' seguida de '   $X,XX  |  Bs Y'.
    lineas = [ln for ln in texto.splitlines() if "💊" in ln]
    if len(lineas) < 2:
        return True
    # EL NÚMERO DE OPCIÓN. El formato canónico es '💊 N. NOMBRE', y el número no es
    # decorativo: es lo que el cliente cita para pedir ("quiero 2 cajas de la opción 3").
    #
    # Caso real (FarmaUnion, provider 19): la respuesta traía
    #     💊 GOTAS NASAL (AIRFEN) PEDIATRICA X 15 ML
    #        $6,64  |  Bs 5.820,86
    #     💊 GOTAS NASALES (AIRFEN) X 15 ML ADULT
    #        $7,99  |  Bs 7.009,07
    # El PRECIO estaba bien formado, así que el chequeo de abajo daba 'canónico' y este
    # backstop no actuaba: el cliente recibía opciones SIN numerar y sin el bloque de cierre
    # ('👉 Para agregar al carrito…', '🛒 ¿Otro medicamento?', '✅ LISTO'), imposible de citar.
    sin_numero = [ln for ln in lineas if not re.search(r"💊\s*\d+\s*[.)]", ln)]
    if sin_numero:
        return True
    # Verificar que al menos la mayoría de las opciones tienen la línea de
    # precio en el formato 'USD | Bs' inmediatamente después.
    ok_formato = 0
    lineas_todas = texto.splitlines()
    for ln in lineas:
        idx = lineas_todas.index(ln)
        siguiente = lineas_todas[idx + 1] if idx + 1 < len(lineas_todas) else ""
        if re.search(r"\$.*\|.*Bs", siguiente):
            ok_formato += 1
    return ok_formato < len(lineas)


def _enumera_productos(texto: str, products: list[dict[str, Any]]) -> bool:
    """True si el texto parece estar LISTANDO los productos (no solo mencionarlos).

    Un texto que enumera trae varias líneas o repite la estructura de la lista (números
    de opción, precios, nombres de producto). Una cortesía de una línea, una pregunta, o
    una respuesta conversacional NO enumeran — y por eso no deben ser reemplazadas por la
    lista del catálogo.

    Se usa en los backstops que SUSTITUYEN el texto: sin este filtro cualquier respuesta
    corta con 2+ productos de la consulta anterior entra y el cliente recibe el catálogo
    en vez de la frase que correspondía.
    """
    t = texto.strip()
    if not t:
        return False
    # 3+ líneas con contenido: es una enumeración (la lista canónica o el intento del LLM).
    lineas = [ln for ln in t.splitlines() if ln.strip()]
    if len(lineas) >= 3:
        return True
    # 2 líneas ya es sospechoso de lista si alguna cita un precio.
    if len(lineas) == 2 and re.search(r"\$\s*\d", t):
        return True
    # Una sola línea/parrafo: solo cuenta si enumera con números de opción o cita precios.
    if re.search(r"(?:^|\n)\s*\d{1,2}\s*[.)-]\s+\S", t):
        return True
    if re.search(r"\$\s*\d", t):
        return True
    # O si nombra 2+ productos distintos de la consulta.
    tl = t.lower()
    nombrados = sum(
        1 for p in products
        if str(p.get("producto") or p.get("nombre") or "").lower()[:18] in tl
    )
    return nombrados >= 2


def _cita_precio_inventado(
    texto: str, products: list[dict[str, Any]]
) -> bool:
    """True si el texto cita un precio ($X.XX o Bs) que NO coincide con ningún
    producto del catálogo real. Detecta el caso en que el LLM menciona el
    medicamento (para que el backstop de omisión no actúe) pero inventa
    marcas/precios (p.ej. "PRENATAL Glaxo $4,10" cuando el catálogo tiene
    "ACIDO FOLICO 5MG ... $2,12"). El cliente jamás recibe un precio falso."""
    if not texto or not products:
        return False
    # Precios USD del catálogo, redondeados a 2 decimales (comparación tolerante).
    precios_reales = {
        round(float(p.get("precio") or 0), 2) for p in products if p.get("precio")
    }
    # Buscar todos los precios "$X.XX" en el texto.
    for m in re.finditer(r"\$\s*([0-9][0-9.,]*)", texto):
        citado = _parsear_precio(m.group(1))
        if citado is None:
            continue
        # Si cita un precio y ese precio no está en el catálogo → inventado.
        if citado not in precios_reales:
            return True
    return False


def _menciona_producto(
    texto: str, products: list[dict[str, Any]], term: str
) -> bool:
    """True si el texto menciona el medicamento consultado: el término o el
    nombre de alguno de los productos devueltos por el catálogo."""
    if not texto:
        return False
    t = texto.lower()
    # El término consultado (p.ej. "atamel forte") o una palabra clave suya.
    if term:
        term_low = term.lower()
        if term_low in t:
            return True
        # Palabras significativas del término (>=4 chars) presentes en el texto.
        palabras = [w for w in re.findall(r"[a-záéíóúüñ]+", term_low) if len(w) >= 4]
        if palabras and any(p in t for p in palabras):
            return True
    # Nombre de alguno de los productos (>=4 chars significativos).
    for p in products:
        nombre = str(p.get("producto") or p.get("title") or "").lower()
        palabras = [w for w in re.findall(r"[a-záéíóúüñ]+", nombre) if len(w) >= 4]
        if palabras and any(pw in t for pw in palabras):
            return True
    return False


# Marcadores de media.py. Se quitan ENTEROS (encabezado + instrucciones): un
# marcador de audio va de `[Audio del lead` hasta el cierre del bloque, y sus
# instrucciones intermedias contienen comas y "y" que el backstop de receta
# confundía con una LISTA de medicamentos.
#
# El corte es GREEDY hasta el siguiente `[` (no hasta el primer `]`): los
# marcadores NO anidan, y cortar por el `]` del encabezado
# (`[Audio del lead, transcrita]`) dejaba vivas las instrucciones que siguen.
_RE_MARCADOR_AUDIO = re.compile(
    r"\[(?:Nota de voz|Audio) del lead[^\[]*", re.IGNORECASE | re.DOTALL
)
_RE_MARCADOR_EL_LEAD = re.compile(
    r"\[El lead [^\[]*", re.IGNORECASE | re.DOTALL
)
_RE_MARCADOR_DOCUMENTO = re.compile(
    r"\[Documento '[^']*'[^\[]*", re.IGNORECASE | re.DOTALL
)


def _texto_cliente_sin_marcadores(user_text: str) -> str:
    """Devuelve SOLO lo que dijo el cliente, sin los marcadores del sistema.

    `media.py` describe la multimedia con marcadores que son INSTRUCCIONES
    NUESTRAS, no texto del cliente::

        [Audio del lead, transcrita]: "ya llegó la nifedipina de 30 mg". Es una
        CONSULTA del lead: interpreta la transcripción, extrae el/los
        medicamento(s) que pide y consúltalos en el catálogo
        (buscar_medicamento). No inventes disponibilidad.]

    Esos marcadores traen comas y "y" DENTRO de la frase. Al trocearlos, sus
    pedazos parecen una LISTA de fármacos y el backstop de receta los consulta
    como medicamentos. Caso real (tenant 27): un audio que solo decía "ya llegó
    la nifedipina de 30 miligramos" buscó `'audio del lead'` y
    `'extrae medicamento pide'`; el primero cayó por fuzzy en `'leda' ≈ 'seda'`
    y el cliente recibió SUTURA SEDA.

    Este helper extrae los DATOS del cliente que viven dentro de los marcadores
    (la transcripción, el OCR, el caption, el contenido del documento) y
    descarta las instrucciones. Sin marcadores devuelve el texto tal cual.
    """
    if not user_text:
        return ""
    if not re.search(r"\[(?:Nota de voz|Audio|El lead|Documento)\b", user_text,
                     re.IGNORECASE):
        return user_text
    trozos: list[str] = []
    # Transcripción de la nota de voz / audio.
    for m in re.finditer(
        r'(?:Nota de voz|Audio) del lead, transcrita\]:\s*"?([^"\]]+)"?',
        user_text, re.IGNORECASE,
    ):
        trozos.append(m.group(1).strip())
    # OCR de la imagen (puede ser una receta de varias líneas).
    for m in re.finditer(r'OCR de la imagen:\s*"([^"]+)"', user_text,
                         re.IGNORECASE | re.DOTALL):
        trozos.append(m.group(1).strip())
    # Caption que el cliente escribió junto al archivo.
    for m in re.finditer(r'Nota del lead junto a [^:]+:\s*"([^"]+)"', user_text,
                         re.IGNORECASE):
        trozos.append(m.group(1).strip())
    # Documento: el contenido extraído va tras el marcador.
    m = re.search(r'contenido extraído\]:\s*(.+)', user_text,
                  re.IGNORECASE | re.DOTALL)
    if m:
        trozos.append(m.group(1).strip())
    # Texto que el cliente escribió FUERA de los marcadores (una ráfaga puede
    # traer "hola" + un audio). Los marcadores se quitan ENTEROS, incluidas sus
    # instrucciones: si se cortan por el `]` del encabezado
    # (`[Audio del lead, transcrita]`), el resto de la instrucción ("... extrae
    # el/los medicamento(s) que pide ...") queda suelto y el backstop de receta
    # lo vuelve a leer como si fueran fármacos.
    restante = _RE_MARCADOR_AUDIO.sub(" ", user_text)
    restante = _RE_MARCADOR_EL_LEAD.sub(" ", restante)
    restante = _RE_MARCADOR_DOCUMENTO.sub(" ", restante)
    restante = restante.strip()
    if restante:
        trozos.append(restante)
    return "\n".join(t for t in trozos if t)


def _texto_ocr_completo(user_text: str) -> str:
    """Extrae el texto OCR COMPLETO del marcador de imagen (puede tener varias
    líneas: una receta con varios medicamentos).

    'OCR de la imagen: "ESOZ 40 MG\nLEPRIT 25 MG"'
    → 'ESOZ 40 MG\nLEPRIT 25 MG'
    """
    m = re.search(r'OCR de la imagen:\s*"([^"]+)"', user_text, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


# Muletillas del HABLA (no del texto): una nota de voz viene con saludo,
# cortesía y verbos de conversación ("buenas tardes mi linda, mire en cuanto a
# que salen las lancetas y las tiras reactivas de 50 por favor dame el precio
# ahí te agradezco"). El limpiador de texto no las conoce porque casi nunca se
# escriben, pero al dictar aparecen SIEMPRE. Medido contra el catálogo real: el
# término crudo devolvía 11 productos con basura (SALES DE REHIDRATACION,
# MASCARILLA, ÑAME SALVAJE) donde solo 3 eran pertinentes.
_MULETILLAS_HABLA = {
    # saludos y cortesía
    "buenas", "tardes", "buenos", "dias", "noches", "saludos", "bendiciones",
    "gracias", "agradezco", "agradecida", "agradecido", "favor", "porfa",
    "disculpe", "disculpa", "permiso", "regalame", "regálame", "deme",
    "regalas", "regala", "regalar", "regale", "regalen", "obsequia",
    # apelativos
    "linda", "lindo", "amor", "corazon", "corazón", "mi", "mijo", "mija",
    "senora", "señora", "senor", "señor", "doctor", "doctora", "jefe",
    # verbos de habla / relleno conversacional
    "mire", "mira", "vea", "oiga", "escuchame", "escúchame", "diga", "digame",
    "dígame", "saben", "sabes", "sabia", "sabía", "fijate", "fíjate",
    "salen", "sale", "resulta", "quisiera", "queria", "quería", "necesito",
    "ocupo", "dame", "dime", "decir", "saber", "preguntar", "consultar",
    "ayuda", "ayudame", "ayúdame", "podria", "podría", "puede", "puedes",
    # muletillas y adverbios de habla
    "en", "cuanto", "cuánto", "ahi", "ahí", "aqui", "aquí", "pues", "bueno",
    "este", "esto", "esa", "eso", "verdad", "entonces", "ahora", "luego",
    "te", "le", "les", "nos", "se", "ya", "si", "no", "mas", "más",
}

# F14.1: los apelativos de cortesía se quitan TAMBIÉN del texto escrito (no
# solo del habla). El caso real que lo motivó: "Buenas tardes!! Mi amor que
# precio tiene la nitrofurantoina de 50mg y la vitamina C" extraía
# "amor nitrofurantoina 50mg vitamina" y el token 'amor' validaba "TE AMO
# PRESERV. 3PCS" (un preservativo) como resultado relevante. La lista es la
# MISMA que usa el filtro de relevancia (app/relevancia.py, APELATIVOS): una
# sola fuente para ambas capas.
_MULETILLAS_HABLA = _MULETILLAS_HABLA | _APELATIVOS

# Apelativos que además hay que comparar SIN tilde: el texto escrito llega tal
# cual lo escribió el cliente ("corazón", "papi mío") y las listas están sin
# tildes.
_APELATIVOS_EXTRA = {"mio", "mia", "tuyo", "tuya", "lindo", "linda"}
_APELATIVOS = _APELATIVOS | _APELATIVOS_EXTRA
_MULETILLAS_HABLA = _MULETILLAS_HABLA | _APELATIVOS_EXTRA

_MAPA_TILDES = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouun" + "AEIOUUN")


def _sin_tildes(palabra: str) -> str:
    """La palabra en minúsculas y sin tildes: la clave de comparación.

    El término de salida conserva lo que escribió el cliente ('solunoval
    solución bucal'); las listas de relleno/verbos/apelativos están sin tildes,
    así que la comparación va por esta clave ('corazón' → 'corazon')."""
    return palabra.lower().translate(_MAPA_TILDES)


def _limpiar_transcripcion(texto: str) -> str:
    """Quita el ruido conversacional de una transcripción de voz.

    El habla trae muletillas que el cliente nunca escribe ("buenas tardes mi
    linda, mire en cuanto a que salen las lancetas... dame el precio ahí te
    agradezco"). Pasar eso como consulta ensucia la búsqueda: el matcher del
    catálogo hace SUBSTRING, así que palabras de relleno arrastran productos
    falsos ("dame" → MEBENDAZOL/DAMENZOL, "linda" → CLINDAMICINA, "las" → ACE EN
    POLVO LAS LLAVE). Medido: el término crudo daba 11 productos con basura
    donde solo 3 eran pertinentes.

    Se conservan las palabras "de producto" (fármaco, marca, presentación,
    dosis), que son las únicas que deben llegar al catálogo.
    """
    palabras = re.findall(r"[a-záéíóúüñ0-9]+", texto.lower())
    # QUITAR TILDES ANTES DE COMPARAR contra `_FILLER` y `_MULETILLAS_HABLA`. Esas listas
    # están escritas SIN tildes ("dias", "que", "mas"), pero la transcripción trae las
    # tildes del habla ("días", "qué", "más"). Comparando en crudo no coinciden, así que
    # las muletillas SOBREVIVEN y llegan al catálogo. Caso real (conv 2834):
    #     "Buenos días mi amor, en qué precio tienen la venda sol?..."
    #     → 'días qué venda sol caja trae dos'   ← 'días' y 'qué' se colaron
    # y el catálogo, con el matcher difuso, devolvió GALLETA SODA EL SOL / ADRENALINA SOL /
    # ALUMBRE en vez de las VENDAS. Con 'venda' solo, devuelve las 3 vendas correctas.
    palabras = [_normalizar_tildes(w) for w in palabras]
    # Unidad HABLADA → abreviatura ('miligramos' → 'mg'). Sin esto el número que
    # la precede se descarta por corto y la dosis se pierde (ver UNIDADES_HABLADAS).
    palabras = [_normalizar_unidad(w) for w in palabras]
    # Unidades de dosis/presentación: son CORTAS pero esenciales (mismo bug que
    # el limpiador del término — filtrar por largo descarta "mg" y con él la
    # concentración: "omeprazol 20 mg" → "omeprazol", y el cliente recibe todas
    # las dosis). Nunca se descartan.
    unidades = {
        "mg", "ml", "mcg", "gr", "g", "kg", "ui", "cc",
        "tab", "tabs", "tableta", "tabletas", "cap", "caps", "capsula",
        "capsulas", "jab", "jarabe", "crema", "gel", "spray", "gotas",
        "supositorio", "ovulo", "ovulos", "ampolla", "ampollas", "inyectable",
        "sobre", "sobres", "solucion", "suspension", "pomada", "unguento",
    }
    utiles: list[str] = []
    for w in palabras:
        # Un número es DOSIS (nunca relleno): se conserva siempre.
        if w.isdigit():
            utiles.append(w)
            continue
        # Unidad de dosis/presentación: corta pero imprescindible.
        if w in unidades:
            utiles.append(w)
            continue
        if _sin_tildes(w) in _FILLER or _sin_tildes(w) in _MULETILLAS_HABLA:
            continue
        # Las palabras muy cortas (1-2 letras) son siempre conectores del habla
        # ("a", "y", "de", "el", "mi", "te"), nunca un producto.
        if len(w) < 3:
            continue
        utiles.append(w)
    # COLETILLA DEL HABLA: "la caja TRAE dos", "la caja viene con dos". Es una pregunta
    # conversacional sobre el ENVASE, no parte del nombre del producto. Se corta el término
    # en el patrón, porque dejarlo producía títulos como
    #     EN QUÉ LA VENDA SOL? LA CAJA TRAE DOS
    # (caso real conv 2834): el cliente lee la frase entera como nombre del producto.
    #
    # POR QUÉ POR PATRÓN Y NO POR PALABRA: medido contra el catálogo real, 'caja' es token
    # de 'DIOSMINA-HESPER 450/50 MG CAJA X 10 TAB' y 'dos' de 'FRON DOS KETACONAZOL'. Quitar
    # esas palabras sueltas rompería esas búsquedas. Lo que NO existe es la secuencia
    # 'caja' + verbo de habla, así que cortar ahí es seguro.
    corte = re.search(
        r"\b(?:caja|empaque|envase|frase)\s+(?:trae|tiene|viene|traen|tienen|vienen)\b",
        " ".join(utiles),
    )
    if corte:
        utiles = " ".join(utiles)[: corte.start()].split()
    return " ".join(utiles)


def _medicamentos_de_transcripcion(texto: str) -> list[str]:
    """Medicamentos REALES dentro de la transcripción de una nota de voz.

    POR QUÉ NO SIRVE EL TROCEO NORMAL: el habla es una frase conversacional con comas, no
    una receta. Trocear por comas/y produce piezas que no son medicamentos. Caso real
    (conv 2834), "Buenos días mi amor, en qué precio tienen la venda sol? La caja trae dos,
    verdad?" daba:
        ['días amor', 'en qué la venda sol? La caja trae dos', 'verdad']
    y el cliente recibió "⚠️ No disponibles en el catálogo: DÍAS AMOR, VERDAD" con el
    título "EN QUÉ LA VENDA SOL? LA CAJA TRAE DOS" — el saludo, la coletilla y la pregunta
    entera presentados como medicamentos.

    CÓMO: cada pieza se pasa por el pipeline de AUDIO (limpiar el ruido del habla + extraer
    el término) y se exige que parezca un medicamento de verdad. Las piezas que quedan
    vacías o son frases se descartan. Medido: así 0 de 5 consultas habladas se trocean,
    mientras las 3 recetas habladas reales ('necesito esoz, leprit y evigax') siguen
    devolviendo sus 3 medicamentos.

    Devuelve [] cuando no hay 2+ medicamentos: una consulta de UN medicamento no es una
    receta y debe seguir el camino normal (que ya limpia la transcripción).
    """
    if not texto:
        return []
    piezas = [p.strip() for p in _lineas_lista_medicamentos(texto) if p.strip()]
    if len(piezas) < 2:
        return []
    out: list[str] = []
    for p in piezas:
        # El pipeline del audio: quita muletillas del habla y verbos de consulta.
        limpio = _extraer_termino_medicamento(_limpiar_transcripcion(p))
        if not limpio:
            continue
        # Descartar frases ('en qué la venda sol caja trae dos' no es un medicamento).
        if not _termino_es_medicamento_plausible(limpio):
            continue
        if limpio not in out:
            out.append(limpio)
    # Menos de 2 medicamentos REALES no es una receta.
    return out if len(out) >= 2 else []


def _texto_transcripcion_completo(user_text: str) -> str:
    """Extrae el texto de la transcripción del marcador de nota de voz/audio.

    '[Nota de voz del lead, transcrita]: "quería saber atamel forte"'
    → 'quería saber atamel forte'
    También tolera el marcador sin comillas (formato anterior).
    """
    m = re.search(
        r"(?:Nota de voz|Audio) del lead, transcrita\]:\s*\"([^\"]+)\"",
        user_text,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m2 = re.search(
        r"(?:Nota de voz|Audio) del lead, transcrita\]:\s*([^\n\]]+)",
        user_text,
        re.IGNORECASE,
    )
    return m2.group(1).strip() if m2 else ""


def _extraer_termino_transcripcion(user_text: str) -> str | None:
    """Extrae el/los medicamento(s) de la transcripción de una nota de voz.

    '[Nota de voz del lead, transcrita]: "quería saber atamel forte"'
    → 'atamel forte'
    Si menciona varios, devuelve el primero (el resto se resuelven en el
    siguiente turno / flujo de receta).
    """
    texto = _texto_transcripcion_completo(user_text)
    if not texto:
        return None
    # PRIMERO quitar el ruido del habla ("buenas tardes mi linda mire en cuanto a
    # que salen... dame el precio ahí te agradezco"), que el limpiador de texto
    # no conoce porque casi nunca se escribe. Si no se quita, esas palabras
    # llegan al catálogo y arrastran productos falsos ("dame"→MEBENDAZOL).
    limpio = _limpiar_transcripcion(texto)
    if not limpio:
        return None
    # _extraer_termino_medicamento limpia verbos de consulta y relleno
    # ("quería saber atamel forte" → "atamel forte").
    term = _extraer_termino_medicamento(limpio)
    if not term:
        return None
    # Si lo que queda son SOLO palabras de relleno ("nada", "gracias"),
    # no es una consulta de medicamento.
    palabras = set(re.findall(r"[a-záéíóúüñ0-9]+", term.lower()))
    if palabras and palabras <= _FILLER:
        return None
    # Tras quitar el ruido puede quedar solo un número ("...de 50"): eso es la
    # DOSIS que el cliente mencionó, pero sin fármaco no hay nada que buscar.
    # Devolverlo consultaría el catálogo por "50" y traería basura.
    if all(p.isdigit() for p in term.split()):
        return None
    return term


# --------------------------------------------------------------------------- #
# ¿El OCR de una imagen es UNA CAJA o una RECETA de varios medicamentos?
# --------------------------------------------------------------------------- #
# Caso real (provider 27, 2026-10): la foto de UNA caja de TRIMIC FORTE L produjo 27
# productos en 3 bloques. Los encabezados de la respuesta eran EXACTAMENTE las 3 líneas
# del OCR ('TRIMIC FORTE L' / 'Metronidazol 750 mg, Miconazol 200 mg, Lidocaína 100 mg'
# / '7 óvulos (supositorios vaginales)'): el backstop de receta había troceado la
# descripción de UN medicamento y consultado el catálogo 3 veces.
#
# Una CAJA describe UN producto: su nombre, sus principios activos y su presentación.
# Una RECETA (varias cajas o una lista) trae VARIOS nombres comerciales independientes.
_LINEAS_DESCRIPTIVAS = (
    # etiquetas de campo del envase
    "principio activo", "principios activos", "principio activo y concentración",
    "concentración", "concentracion", "concentracin", "presentación", "presentacion",
    "presentacin", "contenido neto", "forma farmacéutica", "forma farmaceutica",
    "vía de administración", "via de administracion", "registro sanitario",
    "laboratorio", "fabricante", "indicaciones", "composición", "composicion",
    "dosis", "descripción", "descripcion", "código", "codigo", "cpe",
    # texto promocional/del envase
    "antibiótico", "antibiotico", "antimicótico", "antimicotico", "antiprotozoario",
    # ETIQUETAS DE USO: describen para quién/cómo es, no son otro medicamento.
    # 'USO PEDIÁTRICO' salía como un grupo propio en la foto del frasco de amoxicilina
    # (provider 32, 2026-10-29).
    "uso pediátrico", "uso pediatrico", "uso adulto", "uso oral", "uso tópico", "uso topico",
    "uso vaginal", "uso oftálmico", "uso oftalmico", "vía oral", "via oral",
    "vía de administración oral", "polvo para", "polvo para suspensión",
    "polvo para suspension", "suspensión oral", "suspension oral", "solución oral",
    "solucion oral", "para suspensión oral", "para suspension oral",
)
# Una línea que empieza por una FORMA farmacéutica es presentación del mismo producto.
_RE_SOLO_FORMA = re.compile(
    r"^\s*\d*\s*(?:óvulos?|ovulos?|supositorios?|cápsulas?|capsulas?|tabletas?|"
    r"comprimidos?|sobres?|ampollas?|frascos?|tubos?|gotas?|crema|gel|polvos?|"
    r"suspensión|suspension|solución|solucion|jarabe|emulsión|emulsion)\b",
    re.IGNORECASE,
)


_PALABRAS_PRESENTACION_ENVASE = {
    "tab", "tabs", "tableta", "tabletas", "caps", "capsula", "capsulas",
    "comp", "comprimido", "comprimidos", "gragea", "grageas", "sobre", "sobres",
    "ampolla", "ampollas", "vial", "viales", "und", "unid", "unidad", "unidades",
    "ml", "gr", "g", "mg", "mcg", "ui", "recubierta", "recubiertas", "recubierto",
    "recubiertos", "masticable", "masticables", "efervescente", "efervescentes",
    "frasco", "pomo", "tubo", "blister", "sachet", "stick", "sticks", "vial",
}


_RE_CODIGO_NOMBRE = re.compile(r"^[a-z]+\d{1,3}$")


def _ES_FORMA_FARMACEUTICA(w: str) -> bool:
    """¿La palabra es nombre de forma farmacéutica ('tableta', 'susp', 'jbe')?"""
    return bool(re.match(
        r"^(tab|tabs|tableta|tabletas|caps?|c[aá]psulas?|comp|comprimidos?|grageas?|"
        r"sob|sobre|sobres|susp|suspension|jbe|jarabes?|crema|gel|pomada|unguent|"
        r"[oó]vulo|ovulos|ovu|supositorios?|spray|solucion|sol|colirio|ampolla|ampollas|"
        r"inyectable|granulado|elixir|locion|shampoo|parche|parches)$",
        w,
    ))


_RE_DECLARA_ENVASE = re.compile(
    r"(?i)\b(tabletas?|tabs?|c[aá]psulas?|caps?|comprimidos?|comp|grageas?|sobres?|"
    r"ampollas?|viales?|vial)\b"
)


def _declaraciones_de_envase(lineas: list[str]) -> int:
    """¿Cuántas líneas declaran el contenido de UN envase ('30 tabletas recubiertas')?

    Una CAJA lo declara UNA vez. Cuatro cajas en la misma foto lo declaran CUATRO veces
    (caso real, provider 32, 2026-10-30: metformina + dapagliflozina + losartán +
    hidroclorotiazida en una sola imagen se cotizaron como UN producto y el cliente solo
    vio la metformina).
    """
    total = 0
    for ln in lineas:
        l = ln or ""
        if not _RE_DECLARA_ENVASE.search(l):
            continue
        # '"(10 mEq por tableta)"' describe la DOSIS POR UNIDAD, no el contenido del envase:
        # contarla hacía que la caja del UROCIT pareciera dos envases.
        if re.search(r"\(\s*\d", l) or re.search(
            r"(?i)\b(por|per)\s+(tabletas?|c[aá]psulas?|comprimidos?)", l
        ):
            continue
        if not re.search(r"\d", l):
            continue
        total += 1
    return total


def _es_solo_dosis_y_forma(termino: str) -> bool:
    """¿El término NO nomtra fármaco: es solo números, unidades y formas?

    '1080 mg tableta' y '10 meq tableta' no son medicamentos: el catálogo no puede
    responder nada útil y el matcher difuso empareja 'meq' con lo primero que pille
    (ACHICORIA AD. MEQ, provider 32, 2026-10-29 21:21, para la foto del UROCIT).
    """
    palabras = re.findall(r"[a-záéíóúüñ0-9]+", (termino or "").lower())
    if not palabras:
        return True
    unidades = {
        "mg", "ml", "mcg", "gr", "g", "cc", "ui", "kg", "meq",
        "tab", "tabs", "cap", "caps", "comp", "sob", "sobre", "amp", "vial", "und",
        "mg/g", "meqs",
    }
    residuo = [
        w for w in palabras
        if not w.isdigit()
        and w not in _PALABRAS_PRESENTACION_ENVASE
        and w not in unidades
        and not _ES_FORMA_FARMACEUTICA(w)
    ]
    return not residuo


def _es_linea_de_presentacion(linea: str) -> bool:
    """¿La línea dice el CONTENIDO del envase ('10 Tabletas Recubiertas', 'X 60 ML')?

    Es la firma de una CAJA: una receta escrita no termina con "10 tabletas recubiertas".

    Caso real (provider 32, 2026-10-29): la foto del FIN-AL-GRIP FORTE (paracetamol 500 mg +
    fenilefrina 10 mg + cetirizina 5 mg + dextrometorfano 15 mg, 10 tabletas recubiertas) se
    leyó como CINCO medicamentos: cotizó el FIN-AL-GRIP FORTE y además avisó "No disponibles:
    FENILEFRINA 10 MG, DEXTROMETORFANO 15 MG" y ofreció un PARACETAMOL en ampolla y una
    CETIRIZINA en jarabe. La caja no se reconocía porque la cobertura de componentes exige
    que el TÍTULO del catálogo los nombre, y "FIN-AL-GRIP FORTE" no nombra ninguno.
    """
    l = _normalizar_tildes((linea or "").strip().lower())
    l = re.sub(r"^[•·\-\*\u2022]+\s*", "", l)
    # 1) número + nombre del envase ('100 tabletas de liberación prolongada',
    #    '10 tabletas recubiertas'): la frase puede seguir y sigue siendo presentación.
    if re.match(
        r"^\d+\s*[-.,]?\s*"
        r"(tabs?|tabletas?|c[aá]ps?|c[aá]psulas?|comp|comprimidos?|grageas?|sobre?s?|"
        r"ampollas?|viales?|vial|frasco?s?|pomo?s?|tubo?s?|und?s?|unid|sticks?|blister)",
        l,
    ):
        return True
    # 2) todos los términos son vocabulario de envase ('X 60 ml', 'cont. net: 60 ml').
    palabras = re.findall(r"[a-z]+", l)
    if not palabras:
        return False
    return all(w in _PALABRAS_PRESENTACION_ENVASE for w in palabras)


def _es_linea_descriptiva(linea: str) -> bool:
    """¿La línea describe el MISMO producto (no es otro medicamento)?

    'Principio activo: Metronidazol' → sí (descripción)
    'Presentación: 7 óvulos'         → sí
    '7 óvulos (supositorios vaginales)' → sí (solo forma)
    'LEPRIT 25 MG'                   → NO (es un medicamento propio)
    """
    l = linea.strip().lower()
    if not l:
        return True
    # Un bullet del envase ('• Antibiótico', '- Antimicótico') describe el producto.
    l = re.sub(r"^[•·\-\*\u2022]+\s*", "", l)
    if any(l.startswith(e) or f"{e}:" in l for e in _LINEAS_DESCRIPTIVAS):
        return True
    if _RE_SOLO_FORMA.match(l):
        return True
    # '7 óvulos (supositorios vaginales)' — cantidad + forma + paréntesis
    if re.match(r"^\s*\d+\s+\w+\s*\(", l):
        return True
    return False


def _lineas_candidatas_medicamento(ocr: str) -> list[str]:
    """Líneas del OCR que podrían ser un MEDICAMENTO independiente."""
    return [
        l.strip() for l in (ocr or "").splitlines()
        if l.strip() and not _es_linea_descriptiva(l)
    ]


_RE_DOSIS_ENVASE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(mg|mcg|gr|g|ml|ui)\b", re.IGNORECASE
)


def _dosis_unica_del_envase(lineas: list[str]) -> str:
    """La concentración del envase SI hay UNA sola ('20 mg').

    Caso real (provider 32, 2026-10-29): la foto del RIVAROXABAN 20 mg devolvía la lista de
    15 mg SIN decir que la de 20 no está — porque el término de la caja se quedaba en
    'rivaroxaban' y la dosis que el cliente veía en la caja se perdía. Con la dosis en el
    término, `_aviso_dosis_agotada` avisa: "No tengo de 20 mg. Lo tengo en: 15 mg".

    Si hay VARIAS concentraciones (combinaciones tipo '400 mg - 57 mg/5 ml') se devuelve ''
    para no ensuciar el término.
    """
    vistas: list[str] = []
    for linea in lineas:
        for m in _RE_DOSIS_ENVASE.finditer(linea or ""):
            valor = f"{m.group(1).replace(',', '.')} {m.group(2).lower()}"
            if valor not in vistas:
                vistas.append(valor)
    return vistas[0] if len(vistas) == 1 else ""


_RE_ALTERNATIVAS = re.compile(r"\s/\s|\s+o\s+", re.IGNORECASE)


async def _resolver_alternativas_de_receta(
    lineas: list[str], meds: list[str], buscar: Any
) -> list[str]:
    """Cambia cada línea con alternativas ('A / B o C') por la PRIMERA marca que exista.

    No se inventan marcas: si ninguna está en el catálogo, se deja el primer candidato para
    que el mensaje pueda decir honestamente que no está.
    """
    con_alternativas = [ln for ln in lineas if _RE_ALTERNATIVAS.search(ln or "")]
    if not con_alternativas:
        return meds
    nuevos: list[str] = []
    for ln in lineas:
        if not _RE_ALTERNATIVAS.search(ln or ""):
            continue
        sin_par = re.sub(r"\([^)]*\)", " ", ln)
        candidatos = [
            c.strip(" .-,;")
            for c in _RE_ALTERNATIVAS.split(sin_par)
            if c and c.strip(" .-,;")
        ]
        elegido = ""
        for cand in candidatos:
            term = _limpiar_termino_medicamento(cand) or cand.lower()
            if not term or _es_solo_dosis_y_forma(term):
                continue
            try:
                prods = await buscar(term)
            except Exception:  # noqa: BLE001
                prods = []
            if prods:
                elegido = term
                logger.info("receta: alternativa '%s' → '%s' (está en el catálogo)", cand, term)
                break
        if not elegido and candidatos:
            elegido = _limpiar_termino_medicamento(candidatos[0]) or candidatos[0].lower()
            logger.info("receta: ninguna alternativa de '%s' está — aviso con la primera", ln[:40])
        if not elegido:
            continue
        # Sustituye el término que salió de ESTA línea (si salió) y no lo duplica.
        ya = [m for m in meds if elegido.split()[0] in m.lower()]
        if ya:
            for m in ya:
                meds[meds.index(m)] = elegido
            continue
        nuevos.append(elegido)
    return meds + nuevos


async def _medicamentos_de_ocr(
    ocr: str, buscar: Any
) -> tuple[list[str], str]:
    """Interpreta el OCR: UNA caja (1 medicamento) o una RECETA (varios).

    Devuelve `(terminos, motivo)`. `buscar` es un callable async que consulta el
    catálogo (`nombre -> list[str]` de nombres de producto).

    El discriminador está VERIFICADO contra el catálogo, porque la estructura del texto
    sola no basta (un OCR de caja sin etiquetas parece una lista):
      · si el NOMBRE de la caja está en el catálogo y ALGÚN producto suyo cubre también
        los demás componentes del OCR → esos componentes son del MISMO envase → es una
        caja, se consulta UNA vez con el nombre (lo más discriminante);
      · si no, es una RECETA y se trocea como siempre.

    Medido: TRIMIC FORTE L (caja) → 1 término; ESOZ/LEPRIT, ACETAMINOFEN/IBUPROFENO/
    OMEPRAZOL y LOSARTAN/METFORMINA/ATORVASTATINA (recetas) → 3 términos cada una.
    """
    lineas = _lineas_candidatas_medicamento(ocr)
    if not lineas:
        return [], "sin líneas claras"

    nombre = _limpiar_etiquetas_ocr(lineas[0]) or lineas[0]
    term = " ".join(re.findall(r"[a-záéíóúüñ0-9]+", nombre.lower()))
    componentes = lineas[1:]

    # VARIOS ENVASES EN LA MISMA FOTO. Cuatro cajas apiladas declaran su presentación cuatro
    # veces; una caja de combinación la declara una sola vez. Sin esto, la regla de
    # "un producto cubre los componentes" se cumplía por palabras genéricas ('tabletas',
    # 'recubiertas') y las cuatro cajas se cotizaban como UNA.
    if componentes and _declaraciones_de_envase(lineas) >= 2:
        meds_multi = _parsear_medicamentos_receta("\n".join(lineas))
        meds_multi = [m for m in meds_multi if not _es_solo_dosis_y_forma(m)]
        if len(meds_multi) >= 2:
            meds_multi = await _resolver_alternativas_de_receta(lineas, meds_multi, buscar)
            return meds_multi, "varios envases (cada uno declara su presentación)"

    if term:
        try:
            productos = await buscar(term)
        except Exception:  # noqa: BLE001
            productos = []
        if productos:
            if not componentes:
                return [term], "caja (el OCR solo trae el nombre)"

            # ¿algún producto del catálogo cubre TAMBIÉN los otros componentes?
            #
            # SOLO cuentan los componentes con palabra ÚTIL (≥5 letras) y que no sean
            # descriptivos: así el laboratorio ('QUIM-FAR' → quim/far de 4 y 3 letras) y
            # las etiquetas ('POLVO PARA SUSPENSIÓN ORAL', 'USO PEDIÁTRICO') no impiden
            # reconocer la caja. Antes exigía cubrirlos TODOS y ninguna caja daba «caja».
            # Un componente solo cuenta si puede ser OTRO medicamento: se descartan las
            # etiquetas, las SALES ('clorhidrato', que la caja imprime bajo el nombre), las
            # categorías ('antialérgico') y el LABORATORIO ('CALOX', 'QUIM-FAR').
            from app.tools import (  # noqa: PLC0415
                _PALABRAS_FUNCIONALES as _FUNC,
                _SALES_FARMACEUTICAS as _SALES,
                _CATEGORIAS_TERAPEUTICAS as _CATS,
            )

            def _comp_es_otro_farmaco(comp: str) -> bool:
                # SIN TILDES: las listas guardan 'generico'/'antialergico' y la caja escribe
                # 'Genérico'/'Antialérgico'; sin normalizar no se reconocían y la caja de una
                # FEXOFENADINA se partía en dos medicamentos.
                palabras = [
                    _normalizar_tildes(w)
                    for w in re.findall(r"[a-záéíóúüñ]{5,}", comp.lower())
                ]
                if not palabras:
                    return False
                if all(w in _SALES or w in _CATS or w in _FUNC for w in palabras):
                    return False
                # `_es_nombre_de_laboratorio` espera dicts con 'producto'/'laboratorio';
                # el `buscar` de este módulo devuelve nombres. Se normaliza aquí.
                _prods = [
                    x if isinstance(x, dict) else {"producto": str(x)} for x in productos
                ]
                if _es_nombre_de_laboratorio(comp, _prods):
                    return False
                return True

            utiles_componentes = [
                comp for comp in componentes
                if not _es_linea_descriptiva(comp) and _comp_es_otro_farmaco(comp)
            ]
            def _palabras_de_farmaco(comp: str) -> list[str]:
                # Las palabras de presentación ('tabletas', 'recubiertas', 'cápsulas') NO
                # identifican a otro fármaco: con ellas la cobertura se cumplía sola y
                # cuatro cajas distintas pasaban por una. Ver `_declaraciones_de_envase`.
                return [
                    w for w in re.findall(r"[a-záéíóúüñ]{5,}", comp.lower())
                    if w not in _PALABRAS_PRESENTACION_ENVASE
                ]

            for prod in productos:
                if all(
                    (not _palabras_de_farmaco(comp))
                    or any(es_relevante(w, prod) for w in _palabras_de_farmaco(comp))
                    for comp in utiles_componentes
                ):
                    # El término lleva también los componentes que el producto cubre
                    # ('AMOXICILINA' + 'ÁCIDO CLAVULÁNICO'): buscar solo 'amoxicilina'
                    # devuelve la monoterapia mezclada con la combinación.
                    completo = " ".join([nombre] + utiles_componentes)
                    term_caja = " ".join(
                        re.findall(
                            r"[a-záéíóúüñ0-9]+",
                            _limpiar_etiquetas_ocr(completo).lower(),
                        )
                    )
                    base = term_caja or term
                    dosis_env = _dosis_unica_del_envase(lineas)
                    if dosis_env and dosis_env not in base:
                        base = f"{base} {dosis_env}"
                    return [base], (
                        "caja (un producto cubre los componentes del envase)"
                    )

    # ANTES DE RENDIRSE: ¿el envase se llama con TODOS los componentes juntos?
    # La búsqueda por el primer nombre devuelve solo la monoterapia (medido: 'amoxicilina'
    # trae 19 presentaciones sueltas y NINGUNA de la combinación, que es lo que había en la
    # foto), así que la cobertura no encuentra producto. Si el nombre completo SÍ existe en
    # el catálogo, entonces el envase es UN producto.
    # Los componentes con PALABRA (≥5 letras) son los que pueden formar parte del nombre;
    # '60 ml' o '400 mg - 57 mg/5 ml' son volumen/dosis y solo ensucian la búsqueda.
    componentes_nombre = [
        c for c in lineas[1:]
        if not _es_linea_descriptiva(c) and re.findall(r"[a-záéíóúüñ]{5,}", c.lower())
    ]
    if term and componentes_nombre:
        completo = " ".join([nombre] + componentes_nombre)
        term_completo = " ".join(
            re.findall(r"[a-záéíóúüñ0-9]+", _limpiar_etiquetas_ocr(completo).lower())
        )
        if term_completo and term_completo != term:
            try:
                prods_completo = await buscar(term_completo)
            except Exception:  # noqa: BLE001
                prods_completo = []
            # NO basta con que la búsqueda devuelva algo (el matcher es difuso: con
            # 'esoz leprit bumetin' devuelve productos sueltos y una RECETA de 3 se
            # colapsaba en una). El producto tiene que cubrir TODAS las palabras de todos
            # los componentes.
            # Incluye TAMBIÉN las palabras del NOMBRE: si solo se exigen las del
            # componente, 'LOSARTAN 50 MG / METFORMINA 850 MG' (dos medicamentos) se
            # colapsaba en uno porque hay productos de metformina.
            todas = [
                w
                for c in [nombre] + componentes_nombre
                for w in re.findall(r"[a-záéíóúüñ]{5,}", c.lower())
            ]
            if prods_completo and todas and any(
                all(es_relevante(w, p) for w in todas) for p in prods_completo
            ):
                dosis_env = _dosis_unica_del_envase(lineas)
                base = term_completo
                if dosis_env and dosis_env not in base:
                    base = f"{base} {dosis_env}"
                return [base], (
                    "caja (el envase se llama con todos los componentes)"
                )

    # ANTES DE RENDIRSE, LA ÚLTIMA SEÑAL DE CAJA: el envase declara su contenido.
    #
    # Caso real (provider 32, 2026-10-29): la foto del FIN-AL-GRIP FORTE (paracetamol 500 mg +
    # fenilefrina 10 mg + cetirizina 5 mg + dextrometorfano 15 mg, 10 tabletas recubiertas) se
    # leyó como CINCO medicamentos: cotizó la caja y además avisó "No disponibles: FENILEFRINA
    # 10 MG, DEXTROMETORFANO 15 MG" y ofreció un PARACETAMOL en ampolla IV y una CETIRIZINA en
    # jarabe. La cobertura de componentes falla aquí porque el título del catálogo no nombra
    # ninguno ('FIN-AL-GRIP FORTE'), pero una receta escrita no termina en "10 tabletas
    # recubiertas". Se comprueba sobre el OCR CRUDO: `_lineas_candidatas_medicamento` filtra
    # esa línea por descriptiva.
    #
    # Va DESPUÉS de la cobertura a propósito: 'AMOXICILINA + ÁCIDO CLAVULÁNICO' también declara
    # su presentación, y ahí el término bueno es el de los componentes ('amoxicilina acido
    # clavulanico'), no la mono-terapia.
    if term and componentes:
        _todas = [ln.strip() for ln in (ocr or "").splitlines() if ln.strip()]
        if any(_es_linea_de_presentacion(ln) for ln in _todas):
            # SIN la dosis del envase: el catálogo puede escribir la MISMA presentación
            # en otra escala ('1080 mg de citrato = UROCIT LP 10MG'), y meter la dosis
            # del paquete produce "⚠️ No tengo de 1080 mg. Lo tengo en: 10 mg" sobre el
            # MISMO producto que el cliente está sosteniendo en la mano.
            return [term], "caja (el envase declara su presentación)"

    # Receta: se devuelven las líneas como medicamentos independientes, con la misma
    # lógica que ya usaba el backstop.
    #
    # Se reusan las líneas YA FILTRADAS: pasar el OCR crudo colaba 'POLVO SUSPENSION' y
    # 'USO PEDIATRICO' como medicamentos (los mismos que `_es_linea_descriptiva` descarta).
    meds = _parsear_medicamentos_receta("\n".join(lineas))
    if not meds and _parece_lista_medicamentos(ocr):
        meds = _parsear_medicamentos_receta(
            "\n".join(_lineas_lista_medicamentos(ocr))
        )
    # ALTERNATIVAS EN UNA LÍNEA: "LACRIFORT / HUMED / TOPTEAR / LAGRIOTOL (gotas
    # oftálmicas)" u "OCUPRED O PREDNISONA (gotas oftálmicas)". El cliente ofrece VARIAS
    # marcas para lo mismo y espera el precio de la que haya; buscar la línea entera como
    # un nombre (o descartarla) pierde el renglón. Caso real (provider 32, 2026-10-30).
    meds = await _resolver_alternativas_de_receta(lineas, meds, buscar)
    # UNA DOSIS Y UNA FORMA NO SON UN MEDICAMENTO. Provider 32, 2026-10-29 (foto del
    # UROCIT): el OCR se partió en cuatro y se buscó '1080 mg tableta' (0 productos →
    # "No disponibles en el catálogo: 1080 MG TABLETA") y '10 meq tableta', que el matcher
    # difuso emparejó con ACHICORIA AD. MEQ 120ML.
    meds = [m for m in meds if not _es_solo_dosis_y_forma(m)]
    return meds, "receta (varios medicamentos independientes)"


def _extraer_termino_ocr(user_text: str) -> str | None:
    """Extrae el término de medicamento del marcador OCR de una imagen.

    'OCR de la imagen: "ACIDO FOLICO 5 MG X 10 TABLETAS DROTOFARMA"'
    → 'acido folico 5 mg 10 tabletas drotofarma'.

    ANTES de limpiar, se quitan las ETIQUETAS del envase que el OCR añade
    ('Principio activo:', 'Concentración:', 'Presentación:', 'Contenido Neto:').
    Sin esto el término queda verboso y el matcher (que es AND sobre los tokens)
    no encuentra nada: medido contra el catálogo real del provider 19, el texto de
    la caja de ácido hialurónico producía 20 productos irrelevantes (ÁCIDO
    TRANEXAMICO, ÁCIDO FOLICO...) y NO el correcto; limpio devuelve 1, el correcto.
    """
    m = re.search(r'OCR de la imagen:\s*"([^"]+)"', user_text, re.IGNORECASE)
    if not m:
        return None
    return _extraer_termino_medicamento(_limpiar_etiquetas_ocr(m.group(1)))


# Etiquetas del envase que el OCR copia y que NO son parte del nombre del fármaco.
# Se conserva el VALOR de cada etiqueta ('Concentración: 2%' → '2%'), que es donde
# suelen venir la dosis y la presentación.
#
# OJO: el modelo varía la PRIMERA etiqueta entre ejecuciones ('Nombre del
# medicamento:', 'Medicamento:', 'Producto:', 'Nombre:'). Medido: con 'Nombre del
# medicamento:' el término quedaba verboso y el catálogo devolvía 15 productos
# irrelevantes (gel fijador, toallas sanitarias) en vez del correcto. Por eso la
# lista cubre todas las variantes vistas, no solo las de la etiqueta física.
_ETIQUETAS_OCR = (
    "principio activo", "principioactivo", "concentración", "concentracion",
    "concentracin", "presentación", "presentacion", "presentacin",
    "contenido neto", "vía de administración", "via de administracion",
    "fórmula magistral", "formula magistral", "registro sanitario",
    "laboratorio", "fabricante",
    # Variantes de la etiqueta de nombre que el modelo inventa al extraer.
    "nombre del medicamento", "nombre del producto", "nombre comercial",
    "nombre", "medicamento", "medicamentos", "producto", "productos",
    "texto", "descripción", "descripcion", "dosis", "forma farmacéutica",
    "forma farmaceutica",
)

# Unidades de dosis/presentación: NUNCA se deduplican ni se descartan, aunque se
# repitan entre líneas. Es el mismo patrón que ya mordió tres veces en este stack:
# los números y sus unidades son la excepción a cualquier regla de limpieza.
_UNIDADES_OCR = {"mg", "ml", "mcg", "g", "ui", "gr", "cc", "%"}


def _es_etiqueta_ocr(t: str) -> bool:
    """True si el texto es (solo) una etiqueta del envase."""
    t = (t or "").strip().lower().rstrip(":")
    return any(
        t == e or t.startswith(e + ":") or (t.startswith(e) and len(t) <= len(e) + 2)
        for e in _ETIQUETAS_OCR
    )


def _limpiar_etiquetas_ocr(texto: str) -> str:
    """Quita etiquetas del envase y une el contenido útil, sin perder dosis.

    - 'Etiqueta: valor' → conserva el VALOR ('Concentración: 2%' → '2%').
    - Línea que es solo la etiqueta → se descarta.
    - Deduplicación POR LÍNEA (quita el nombre repetido dentro de una misma línea),
      NUNCA entre líneas: 'ESOZ 40 MG\\nLEPRIT 25 MG' no debe perder la unidad de
      la segunda dosis.
    """
    lineas: list[str] = []
    for linea in (texto or "").splitlines():
        t = linea.strip()
        if not t:
            continue
        if ":" in t:
            izq, der = t.split(":", 1)
            if _es_etiqueta_ocr(izq):
                der = der.strip()
                if der:
                    lineas.append(der)
                continue
        if _es_etiqueta_ocr(t):
            continue
        lineas.append(t)

    salida: list[str] = []
    for linea in lineas:
        vistos: set[str] = set()
        tokens: list[str] = []
        for tok in linea.split():
            clave = tok.lower()
            # Unidades y números nunca se deduplican (son dosis).
            if clave in vistos and clave not in _UNIDADES_OCR and not tok[:1].isdigit():
                continue
            vistos.add(clave)
            tokens.append(tok)
        if tokens:
            salida.append(" ".join(tokens))
    return " ".join(salida)


def _parece_referencia_sin_farmaco(user_text: str) -> bool:
    """True si el cliente solo REFERENCIA una imagen, sin nombrar un fármaco.

    Caso real (provider 19, 2026-10): el cliente manda la foto de una caja de
    ÁCIDO HIALURÓNICO 2% ÓVULOS y luego pregunta "El producto de la foto lo
    tienes?". El texto no nombra ningún medicamento: solo habla de "la foto".

    Buscar en el catálogo con esas palabras devuelve basura por SUBSTRING:
    'foto' ⊂ 'FOTORRETIN', así que el catálogo devolvía GOTAS OFTALMICA
    (FOTORRETIN) X 5 ML y el agente afirmaba "Sí, tengo el producto que aparece
    en la foto" mostrando un oftálmico ante unos óvulos vaginales.

    En este caso la búsqueda debe usar el término del OCR de la imagen anterior,
    no las palabras de la pregunta.

    Se exige: (a) una referencia explícita a la imagen y (b) que NO quede ningún
    token sustantivo (fármaco) tras quitar las palabras funcionales — si el
    cliente dice "el ácido hialurónico de la foto", SÍ hay fármaco y se busca con
    él.
    """
    if not user_text:
        return False
    t = user_text.lower()
    t_sin = (
        t.replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u")
    )
    # (a) ¿menciona la imagen/el envío?
    if not re.search(
        r"\b(?:foto|imagen|captura|pantallazo|adjunto|anexo|envie|enviaste|"
        r"mande|mandaste|mandado|enviado|muestra|aparece|figura)\b",
        t_sin,
    ):
        return False
    # (b) ¿queda algún sustantivo que pueda ser fármaco?
    palabras = re.findall(r"[a-z0-9]+", t_sin)
    for w in palabras:
        if len(w) < 3:
            continue
        if w in _PALABRAS_FUNCIONALES:
            continue
        # Un número suelto o una dosis no es un fármaco por sí solo.
        if w.isdigit():
            continue
        return False  # hay una palabra sustantiva: el cliente nombró algo
    return True


def _es_negativa_o_despedida(texto: str) -> bool:
    """True si el mensaje es una NEGATIVA, disculpa o despedida del cliente.

    Caso real (provider 19, 2026-10):
        cliente: "No gracias no las voy a comprar y disculpe"
        agente : "⚠️ No disponibles en el catálogo: DISCULPE
                  VOY COMPRAR
                  💊 1. CHOCOLATE SAVOY 75 ANOS X 25 GR ..."

    El splitter de listas partía el mensaje por la 'y' y por comas, así que los
    fragmentos 'voy comprar' y 'disculpe' se trataban como DOS medicamentos: se
    consultaba el catálogo con esas frases, no había resultados, y el agente
    respondía con la lista de "no disponibles" — encima con chocolates, que
    matcheaban por casualidad. El cliente se estaba despidiendo y recibió un
    catálogo de chocolates.

    Criterio: cortesía/negativa (disculpa, gracias, negación de compra) Y sin
    ningún verbo de consulta de medicamento. Un mensaje que nombra un fármaco
    ("no, mejor dame el de 40 mg") NO es esto: `_VERBOS_MEDICAMENTO` lo salva.

    OJO con la negación del verbo: "ya no QUIERO nada" lleva 'quiero' (que está en
    `_VERBOS_MEDICAMENTO`) pero es una NEGATIVA, no una consulta. Por eso las
    formas negadas ('no quiero', 'ya no quiero', 'no necesito') se comprueban
    ANTES del corte por verbo.
    """
    if not texto:
        return False
    t = texto.strip().lower()
    t = (t.replace("á", "a").replace("é", "e").replace("í", "i")
         .replace("ó", "o").replace("ú", "u"))

    # (a) Negación DIRECTA del verbo: 'no/ya no' + verbo de consulta. Se evalúa
    # antes del corte por verbo, porque el verbo está pero negado. El pronombre
    # intermedio es opcional ('no LO voy a comprar', 'no LAS voy a comprar').
    #
    # Se EXCLUYE la expresión de DUDA ('no sé si quiero…', 'no estoy seguro si…'):
    # ahí el 'no' no niega la compra, el cliente está comparando opciones y SÍ
    # quiere información. Sin esta exclusión una duda legítima se trataba como
    # despedida y el cliente se quedaba sin respuesta.
    if re.search(r"\bno\s+(?:se|sé|estoy\s+segur\w*|sabria|sabría)\b", t):
        return False
    if re.search(
        r"\b(?:ya\s+)?no\s+(?:\w+\s+){0,2}?"
        r"(?:quiero|necesito|busco|me\s+interesa|voy\s+a?\s*comprar|"
        r"compro|puedo|deseo|pienso\s+comprar)\b",
        t,
    ):
        return True

    # (b) Cualquier otro verbo de consulta: NO es una simple despedida.
    if _VERBOS_MEDICAMENTO.search(t):
        return False
    return bool(
        re.search(
            r"\b(?:disculpe|disculpa|disculpen|perdone|perdon|lo\s+siento|"
            r"no\s+gracias|gracias|dejelo|dejalo|olvidalo|olvídelo|"
            r"no\s+compro|no\s+quiero\s+nada|"
            r"no\s+me\s+interesa|adios|hasta\s+luego|"
            r"nos\s+vemos|que\s+estes?\s+bien|chao)\b",
            t,
        )
    )


# Vocabulario del MOTIVO: precio, disponibilidad, "¿tienes...?". NO acota la consulta a
# un producto — solo dice por qué lo pregunta. Se descarta para contar medicamentos.
# Caso real (provider 27, 2026-10): "Precio de valsartan 80 hidroclorotiazida 12.5 y
# omeprazol" respondía SOLO omeprazol, porque 'precio' está en `_VERBOS_MEDICAMENTO` y
# eso hacía que `_parece_lista_medicamentos` cortara con False. La consulta pedía DOS
# medicamentos (valsartán+hidroclorotiazida es una combinación, y omeprazol aparte).
_VERBOS_MOTIVO = re.compile(
    r"\b(?:precio|precios|cuesta|cuestan|cuanto|cuánto|vale|valen|"
    r"disponible|disponibles|disponen|disponemos|tienen|tienes|tiene|tenemos|"
    r"venden|vendes|consigo|consigues|conseguir|hay|manejan|trabajan|"
    r"necesito|busco|quiero|quisiera|dame|me das|"
    # AGREGAR AL CARRITO. El cliente que ya está armando el pedido pide AÑADIR, y ese
    # verbo se quedaba pegado al producto: 'agregas 3 cajas glymar 4 mg' se buscaba como
    # 'agregas 3 glymar 4 mg', y como el catálogo exige TODOS los tokens, la relevancia
    # lo descartaba y GLYMAR salía como "no disponible".
    # Caso real (conv 2109, 2026-10-17): "…y le agregas 3 cajas de glymar de 4 MG y 2
    # toallas securezza x 15" → el agente respondió
    #     "⚠️ No disponibles en el catálogo: RESUMEN, AGREGAS 3 CAJAS GLYMAR 4 MG"
    r"agrega|agregas|agregar|agregale|agrégale|agregalo|agregame|agrégame|"
    r"añade|añades|añadir|añademe|añádele|"
    r"sumale|súmale|sumame|incluye|incluyes|incluir|ponme|mete|adiciona)\b",
    re.IGNORECASE,
)
# Separadores de ENUMERACIÓN de medicamentos.
_RE_SEP_LISTA = re.compile(
    r"[,;]"
    r"|(?:\s+y\s+)"
    # ' e ' es conjunción, PERO NO cuando es la VITAMINA E. Caso real (conv 2287,
    # provider 19): "Será que tienen vitamina e  de vivas" se partía en
    #     ['Será que tienen vitamina', 'de vivas']
    # y cada trozo se buscaba como un medicamento distinto: 'será vitamina' → 0 (y el
    # cliente recibía "⚠️ No disponibles: SERÁ VITAMINA") y 'vivas' → 5 productos VIVAX
    # encabezados por 'VIVAS'. El cliente pedía VITAMINA E de la marca VIVAX, que ESTÁ en
    # el catálogo ('VIT E 400 MG X 15 CAPS BLANDAS VIVAX').
    # Se excluye con LOOKBEHIND ('vitamina' 8 letras, 'vit' 3: longitudes fijas, válidas).
    r"|(?:(?<!vitamina)(?<!vit)\s+e\s+)"
    r"|(?:ademas|además)"
    # FIN DE FRASE: son DOS peticiones distintas. Caso real (provider 32, 2026-10-29):
    #     "Precio de la macrodantina?? Voy a querer una vitamina C de la opción 9"
    # se buscaba como UN medicamento ('macrodantina vitamina c 9') y el cliente recibía
    # "⚠️ No tengo MACRODANTINA VITAMINA C 9".
    r"|[?!]+",
    re.IGNORECASE,
)


def _sin_motivo(texto: str) -> str:
    """Quita el vocabulario del MOTIVO (precio, cuánto cuesta, tienes...)."""
    return _VERBOS_MOTIVO.sub(" ", texto or "")


def _sin_vocativo(texto: str) -> str:
    """Quita un VOCATIVO inicial: el nombre de la persona a la que se habla.

    Caso real (conv 2109, provider 19):
        cliente: "Robi, me puedes dar el precio del beprospem"
        agente : ⚠️ No disponibles en el catálogo: ROBI
                 DAR DEL BEPROSPEM
                 💊 1. BEPROSPEN 7 MG AMPOLLA X 1 ML C0N JER
    El producto se encontró, pero el cliente veía 'ROBI' presentado como un medicamento que
    no tenemos. 'Robi' no es un fármaco: es a quien el cliente le habla.

    POR QUÉ: la COMA parte el mensaje en ['Robi', 'me puedes dar...'] y el camino de LISTA
    busca cada trozo como un medicamento distinto. `_parsear_medicamentos_receta('Robi')`
    devuelve ['robi'], así que el trozo no se descarta.

    CUÁNDO: solo si (a) lo que precede a la coma son UNA O DOS palabras y (b) el resto es una
    consulta COMPLETA (trae un verbo de consulta). Así las listas reales no se tocan:
        'esoz, leprit y evigax'      → el resto no trae verbo  → NO se toca (sigue lista)
        'atamel, ibuprofeno'         → el resto no trae verbo  → NO se toca
        'Robi, me puedes dar ...'    → el resto trae 'dar'     → se quita el vocativo

    DOS palabras, no una: el vocativo real casi nunca es un solo token. MEDIDO:
        'mi amor, me das el omeprazol'  → con 1 palabra NO se tocaba y quedaba
                                          ⚠️ No disponibles: AMOR (el mismo bug que 'ROBI')
        'Buenas tardes, puedes darme precio de las gotas oftalmicas Clarasol'
                                        → con 2 palabras se quita el saludo y el término sigue
                                          siendo 'gotas oftalmicas clarasol' (verificado)
    """
    m = re.match(
        r"^\s*((?:[A-Za-zÁÉÍÓÚÑÜáéíóúñü]+\s+){0,1}[A-Za-zÁÉÍÓÚÑÜáéíóúñü]{2,20})"
        r"\s*,\s*(.+)$",
        texto or "",
        re.DOTALL,
    )
    if not m:
        return texto
    resto = m.group(2).strip()
    if not resto:
        return texto
    # SIN TILDES para evaluar los verbos: las listas de verbos vienen sin tildes, así que
    # 'podrías' NUNCA casaba con `\bpodrias\b` y el vocativo no se quitaba. Caso real
    # (conv 2109): "Hola Robi, buenas tardes, por favor me podrías cotizar..." dejaba
    # 'Hola Robi' como trozo → 'ROBI' presentado como medicamento que no tenemos.
    resto_norm = (
        resto.lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u").replace("ü", "u")
    )
    tiene_verbo = bool(
        re.search(_VERBOS_MEDICAMENTO.pattern, resto_norm, re.IGNORECASE)
        or re.search(_VERBOS_MOTIVO.pattern, resto_norm, re.IGNORECASE)
        or re.search(
            r"\b(?:dar|dame|darme|darte|darnos|denme|puedes|podrias|podria|podriamos|"
            r"cotizar|cotizame|presupuesto|quiero|"
            r"quisiera|necesito|busco|tienes|hay)\b",
            resto_norm,
            re.IGNORECASE,
        )
    )
    return resto if tiene_verbo else texto


def _medicamentos_enumerados(texto: str) -> list[str]:
    """Medicamentos de una consulta que ENUMERA varios (con separadores).

    'Precio de valsartan 80 hidroclorotiazida 12.5 y omeprazol'
      → ['valsartan 80 hidroclorotiazida 12', 'omeprazol']
    """
    if not texto:
        return []
    trozos = [p.strip() for p in _RE_SEP_LISTA.split(texto) if p.strip()]
    if len(trozos) < 2:
        return []
    out: list[str] = []
    for trozo in trozos:
        de = _parsear_medicamentos_receta(_sin_motivo(trozo))
        if de:
            out.extend(de)
        else:
            # Un trozo sin medicamento reconocido puede ser continuación del anterior
            # ('valsartan 80' + 'hidroclorotiazida 12.5' describen la MISMA combinación).
            #
            # PERO tiene que PARECER un medicamento. Antes bastaba una palabra de 5+
            # letras, y con eso se colaba CUALQUIER frase: en
            #     "Buenas tardes, puedes darme precio de las gotas oftalmicas Clarasol."
            # el SALUDO 'Buenas tardes' pasaba el filtro y el cliente recibía
            #     ⚠️ No disponibles en el catálogo: BUENAS TARDES
            # o sea el saludo presentado como un medicamento que no tenemos.
            limpio = " ".join(_sin_motivo(trozo).split())
            if not limpio or not re.search(r"[a-záéíóúüñ]{5,}", limpio):
                continue
            # El motivo se quita ANTES de decidir: en 'puedes darme precio de las gotas
            # oftalmicas Clarasol' lo que sobra es el verbo, no el fármaco.
            cand = _limpiar_termino_medicamento(limpio) or limpio
            # Un saludo ('buenas tardes') NO es un medicamento.
            if _es_solo_saludo(cand) or _es_solo_saludo(limpio):
                continue
            # Título = el fármaco, no la frase del cliente ('gotas oftalmicas clarasol').
            cand = _extraer_termino_medicamento(cand) or cand
            if _termino_es_medicamento_plausible(cand):
                out.append(cand)
    return out


def _filtrar_por_dosis(
    products: list[dict[str, Any]], dosis: str
) -> list[dict[str, Any]]:
    """Deja solo los productos cuya CONCENTRACIÓN coincide con la dosis pedida.

    POR QUÉ HACE FALTA FILTRAR LOCALMENTE: buscar con la dosis en el término NO basta.
    Medido contra el catálogo real del provider 05:
        'carvedilol 6.25 mg'      → 5 productos, TODOS de 25 mg   (0 coincidencias)
        'hidroclorotiazida 12.5 mg' → 9 productos, 5 de 12.5 y 4 de 25
        'candesartan 16 mg'       → 2 productos, ambos de 16 mg   (el único que filtra)
    El motor del catálogo cae al grupo difuso y matchea por el FÁRMACO ignorando el número,
    así que devuelve concentraciones mezcladas. Caso real: el cliente pidió 3 medicamentos
    con su dosis y recibió todas las concentraciones de cada uno.

    Si el filtro deja 0 productos NO se descarta todo: se devuelve la lista completa, porque
    es mejor ofrecer las concentraciones disponibles (el cliente elige) que negar el
    medicamento por una dosis que este catálogo quizá no maneja.
    """
    if not dosis or not products:
        return products
    objetivo = dosis.replace(",", ".").strip()
    out: list[dict[str, Any]] = []
    for p in products:
        nom = str(p.get("producto") or p.get("nombre") or p.get("title") or "")
        # Concentraciones escritas en el título: '12.5MG', '6,25', '80/12,5', '80 MG'.
        nums = {
            n.replace(",", ".")
            for n in re.findall(r"\d+(?:[.,]\d+)?", nom)
        }
        if objetivo in nums:
            out.append(p)
    return out or products


def _filtrar_por_envase(
    products: list[dict[str, Any]], envase: str
) -> list[dict[str, Any]]:
    """Deja solo los productos cuyo ENVASE coincide con el que pidió el cliente.

    Es el hermano de `_filtrar_por_dosis`, y hace la misma falta: el motor del catálogo
    matchea por el FÁRMACO e IGNORA los números, así que devuelve los envases mezclados.

    Caso real (conv 2109, provider 19): el cliente pidió
        "Glimepiride de. 4 MG x 30 Tab"
    y recibía 4 opciones, la primera de ellas `GLIMEPIRIDA 4 MG X 10 TAB` — 10 tabletas
    cuando pidió 30. Medido en ese catálogo: 'candesatan 30 tab' devuelve 12 productos
    (10 de envase 30 y 2 de envase 10); 'amlodipina 5 mg 30 tab' devuelve 6 (4 de 30 y
    2 de 10).

    Si el filtro deja 0 productos NO se descarta todo: se devuelve la lista completa. Es
    mejor ofrecer los envases que hay (el cliente elige, o se le puede decir que el de 30
    no está) que negar el medicamento por un envase que este catálogo quizá no maneje.

    Los productos cuyo envase NO se puede leer del título se descartan mientras haya
    otros que sí coincidan: si el cliente pidió 30, no se le puede confirmar que un
    producto ilegible trae 30. (Medido: en estos catálogos el título siempre trae el
    envase, así que no se pierde nada.)
    """
    if not envase or not products:
        return products
    objetivo = envase.lstrip("0") or envase
    out = [
        p for p in products
        if _envase_de_nombre(p.get("producto") or p.get("nombre") or p.get("title"))
        == objetivo
    ]
    return out or products


def _aviso_no_lo_tengo(term: str, ausente: str) -> str:
    """Aviso cuando el cliente pidió algo que NO está en el catálogo pero hay lista.

    POR QUÉ HACE FALTA: el motor del catálogo exige todos los tokens y, si no los encuentra,
    reintenta con OR — así que una marca que no existe se cae SILENCIOSAMENTE y el cliente
    recibe una lista de otras marcas bajo el TÍTULO DE LO QUE PIDIÓ.

    Caso real (conv 2109, provider 19, 2026-10-17 12:01):
        cliente: "Buenas tardes tiene disponible crema dental sensodyne y que costo porf"
        agente : "CREMA DENTAL SENSODYNE" seguido de 8 productos... NINGUNO de Sensodyne
                 (Colgate Triple Acción, Colgate Kids, Tantum Verde...).
    `sensodyne` da 0 productos en ese catálogo: el título prometía algo que la lista no
    cumplía. Re-listar sin decir el NO es no responder — la misma regla que el aviso de
    envase.

    Devuelve '' si no hay nada que avisar.
    """
    if not term or not ausente:
        return ""
    # La LETRA de la vitamina no es "algo que no tengo": 'vitamina c' producía
    #     ⚠️ No tengo C. De VITAMINA hay esto:
    # encima de una lista de VITAMINA C (provider 32, 2026-10-29).
    if len(ausente.strip()) < 2 or _es_letra_de_vitamina(ausente.strip().lower(), term):
        return ""
    resto = " ".join(w for w in term.split() if w.lower() != ausente.lower())
    if resto.strip():
        return f"⚠️ No tengo {ausente.upper()}. De {resto.upper()} hay esto:"
    return f"⚠️ No tengo {ausente.upper()}."


def _lista_con_aviso(
    productos: list[dict[str, Any]], term: str, cierre: str = ""
) -> str:
    """La lista canónica precedida del aviso, si el cliente pidió algo que NO está.

    POR QUÉ: el motor del catálogo exige todos los tokens y, si no los encuentra, reintenta
    con OR — así que una marca que no existe se cae SILENCIOSAMENTE y el cliente recibe
    otras marcas bajo el TÍTULO DE LO QUE PIDIÓ. Y hay un backstop que EMPEORA el problema:
    cuando el LLM niega disponibilidad y hay productos, le pisa la negativa con la lista.
    Si el LLM tenía razón, pisarlo es exactamente lo que engaña al cliente.

    Caso real (conv 2109, provider 19, 2026-10-17 12:01):
        cliente: "Buenas tardes tiene disponible crema dental sensodyne y que costo porf"
        el LLM : negó disponibilidad — CORRECTAMENTE, 'sensodyne' da 0 productos
        backstop: "el LLM negó disponibilidad pese a 8 productos — reemplazo con lista real"
        cliente: "CREMA DENTAL SENSODYNE" + 8 Colgate y Tantum. NINGUNO de Sensodyne.

    Se usa en TODOS los backstops que reemplazan el texto por la lista: si uno avisa y otro
    no, el mismo mensaje da respuestas distintas según qué backstop salte.

    El título pasa sin el token ausente, para no prometer lo que la lista no trae.
    """
    ausente = _nucleo_ausente(term, productos)
    resto = (
        " ".join(w for w in term.split() if w.lower() != ausente.lower()).strip()
        if ausente
        else ""
    )
    avisos_lista, titulo_lista = _avisos_de_lista(productos, resto or term)
    # La FORMA pedida (suspensión, tabletas, gotas…) también se avisa aquí: es el punto
    # único de los seis backstops. Los productos llegan ya acotados por la tool.
    _fam_f, _etq_f = _forma_pedida(resto or term)
    # Se acota TAMBIÉN aquí (no solo avisar): la tool ya devuelve los productos filtrados,
    # así que esto es idempotente — pero deja el punto único de los backstops correcto por
    # sí mismo, sin depender de que quien llama lo haya hecho antes.
    if _fam_f and productos:
        productos = _filtrar_por_forma(productos, _fam_f)
    aviso_forma_lista = _aviso_forma(productos, resto or term, _etq_f) if _fam_f else ""
    if aviso_forma_lista:
        avisos_lista = " ".join(
            a for a in (aviso_forma_lista, avisos_lista) if a
        )
        # El encabezado pierde la forma que se acaba de negar. La comparación va sin
        # tildes: el término y el título del cliente no tienen por qué coincidir en
        # acentos ('APIRET SUSPENSION' vs etiqueta 'suspensión').
        _etq_norm = _normalizar_tildes((_etq_f or "").lower())
        if _etq_norm and _etq_norm in _normalizar_tildes((titulo_lista or "").lower()):
            titulo_lista = _termino_sin_forma(titulo_lista)
    # El encabezado no puede prometer un nombre que ningún producto lleva.
    titulo_lista = _titulo_por_marcas(productos, titulo_lista)
    cuerpo = _formatear_lista_productos(productos, titulo_lista)
    if ausente:
        cuerpo = _aviso_no_lo_tengo(term, ausente) + "\n\n" + cuerpo
    if avisos_lista:
        cuerpo = avisos_lista + "\n\n" + cuerpo
    return cuerpo + cierre


def _titulo_sin_dosis(termino: str, dosis: str) -> str:
    """Quita del TÍTULO la concentración que el catálogo no tiene.

    Sin esto el mensaje se contradice: avisa de que no hay 600 mg y el encabezado de la
    lista sigue diciendo 'ACETAMINOFEN 600 MG' sobre productos de 500 y 650.
    """
    m = re.match(r"(\d+(?:[.,]\d+)?)\s*([a-z]+)", (dosis or "").strip().lower())
    if not m or not termino:
        return termino
    patron = re.compile(
        rf"\b{re.escape(m.group(1))}\s*{re.escape(m.group(2))}\b", re.IGNORECASE
    )
    limpio = re.sub(r"\s{2,}", " ", patron.sub("", termino)).strip(" ,-")
    return limpio or termino


def _titulo_por_marcas(productos: list[dict[str, Any]], term: str) -> str:
    """Encabezado con la MARCA real cuando NINGÚN producto lleva el nombre pedido.

    Mismo principio que el título sin dosis: el mensaje no puede encabezar con algo que
    acaba de negar. Caso real (provider 32, 2026-10-28): «Metronidazol ovulos» → aviso
    "No tengo METRONIDAZOL en óvulos con ese nombre" y debajo el encabezado
    "METRONIDAZOL OVULOS", cuando el único producto era OVUMET. Encabeza OVUMET.
    """
    if not productos or not term:
        return term
    nucleo = ((_termino_sin_forma(term) or term).split() or [""])[0].lower()
    titulos = [str(p.get("producto") or p.get("nombre") or "") for p in productos]
    if not nucleo or any(
        _normalizar_tildes(nucleo) in _normalizar_tildes(x.lower()) for x in titulos if x
    ):
        return term  # algún producto sí lleva el nombre: el encabezado está bien
    marcas: list[str] = []
    for x in titulos:
        primera = (x.split() or [""])[0].strip(" -.,:;()[]")
        if len(primera) >= 3 and primera.upper() not in marcas:
            marcas.append(primera.upper())
    return " / ".join(marcas[:2]) if marcas else term


def _avisos_de_lista(productos: list[dict[str, Any]], term: str) -> tuple[str, str]:
    """Avisos que van ENCIMA de una lista y el término que debe usar el TÍTULO.

    Devuelve `(avisos, termino_del_titulo)`. Los avisos son los que pueden aplicar:
    concentración pedida que no está (`_aviso_dosis_agotada`) y envase pedido que no está
    (`_aviso_envase_agotado`). Cuando la concentración falta, el título pierde esa
    concentración: si no, el mensaje avisa del NO y a la vez encabeza con lo que pidió.

    POR QUÉ AQUÍ: `_lista_con_aviso` es el punto único de los seis backstops que
    reemplazan el texto del LLM por la lista real. Si el aviso se añade en el camino de la
    receta pero no aquí, el mismo mensaje se responde distinto según qué backstop salte.

    Caso real (conv 7, provider 05, 2026-10-27): "Acetaminofen de 600mg" no está en el
    catálogo, y la respuesta salió por el backstop de omisión con 11 productos de 650/500
    mg bajo el título "ACETAMINOFEN 600 MG", sin decir que el de 600 no existe.
    """
    if not productos or not term:
        return "", term
    partes: list[str] = []
    titulo = term
    dosis = _extraer_dosis(term)
    if dosis:
        aviso = _aviso_dosis_agotada(productos, dosis)
        if aviso:
            partes.append(aviso)
            titulo = _titulo_sin_dosis(term, dosis)
    envase = _envase_pedido_en_linea(term)
    if envase:
        aviso = _aviso_envase_agotado(productos, envase)
        if aviso:
            partes.append(aviso)
    return " ".join(p for p in partes if p), titulo


# Concentración escrita en el título de un producto: '650 MG', '120MG/5ML', '1 G'.
# La unidad es de DOSIS o peso; nunca TAB/CAP, que son envase.
_RE_CONCENTRACION_DE_NOMBRE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|gr|ml|cc|ui)\b"
    r"(?:\s*/\s*(\d+(?:[.,]\d+)?)\s*(ml|cc|g|gr)\b)?",
    re.IGNORECASE,
)


def _concentracion_de_nombre(nombre: object) -> str:
    """La concentración de un título de producto: 'ACETAMINOFEN 120MG/5ML X 120ML' → '120 mg/5 ml'.

    Se salta los números de ENVASE: los que van tras la 'x' ('X 10 TAB', 'X 120ML') no
    son concentración. Conserva la forma con volumen cuando la hay, porque '120 mg/5 ml'
    de un jarabe no se puede comparar con '500 mg' de una tableta.
    """
    texto = str(nombre or "")
    for m in _RE_CONCENTRACION_DE_NOMBRE.finditer(texto):
        previo = texto[max(0, m.start() - 3): m.start()].strip().lower()
        if previo.endswith("x"):
            continue
        num = m.group(1).replace(",", ".")
        unidad = m.group(2).lower()
        base = f"{num} {unidad}"
        if m.group(3):
            base += f"/{m.group(3).replace(',', '.')} {m.group(4).lower()}"
        return base
    return ""


# Palabras que la CAJA imprime pero que no son el nombre del medicamento.
# 'Medicamento Genérico' ocupa una línea entera de la caja y el OCR la devuelve pegada al
# nombre ('Medicamento Fexofenadina Clorhidrato Calox'). Caso real 2026-10-27.
_PALABRAS_DE_CAJA = {
    "medicamento", "medicamentos", "generico", "genericos", "generica", "genericas",
    # Etiquetas: el OCR devuelve el rótulo del campo pegado al nombre. Caso real: la misma
    # caja de FEXOFENADINA se leyó 'Nombre del Fexofenadina Clorhidrato Calox'.
    "nombre", "del", "descripcion", "producto", "presentacion",
}


def _cierra_siempre_los_titulos(token: str, productos: list[dict[str, Any]]) -> bool:
    """¿Ese token aparece en los títulos y SIEMPRE como última palabra?

    Es la firma del LABORATORIO: la marca se escribe al final del título
    ('ACETAMINOFEN 500 MG X 10 TAB CALOX') y el fármaco, nunca — el fármaco abre el título
    o va dentro de un paréntesis. Basta UNA aparición: en una búsqueda de 3 productos puede
    ser que la marca aparezca en uno solo.

    Caso en que NO debe disparar: 'acido folico', donde 'FOLICO' cierra algunos títulos
    ('NONAFOL 5 MG X 30 TAB (ACIDO FOLICO)') pero en otros va en medio → no siempre cierra.
    """
    if not token:
        return False
    apariciones = 0
    for p in productos:
        toks = _tokens_de_titulo(
            _titulo_normalizado(p.get("producto") or p.get("nombre") or p.get("title"))
        )
        if token in toks:
            apariciones += 1
            if toks[-1] != token:
                return False
    return apariciones >= 1


# ── ¿EL EQUIPO VIENE CON LOS MATERIALES? ──────────────────────────────────────────
# El catálogo no guarda el CONTENIDO de la caja: en `products-providers` el doc de un
# glucómetro solo trae ProductTitle ("GLUCOMETRO GLM-78 X 1UND GLUCARE"), precio y stock.
# Así que el agente no puede asegurar qué incluye — pero sí puede ofrecer los CONSUMIBLES
# que vende por separado, que es lo que hace un humano de mostrador.
#
# Caso real (provider 32, 2026-10-27):
#   "Digo q si el Glucómetro viene con todos los materiales o solo el aparato"
_EQUIPOS_CONSUMIBLES: dict[str, tuple[str, ...]] = {
    "glucometro": ("tiras reactivas", "lancetas"),
    "glucometros": ("tiras reactivas", "lancetas"),
}

_RE_PREGUNTA_CONTENIDO = re.compile(
    r"\b(viene\s+con|vienen\s+con|incluye|incluyen|trae|traen|contiene|contienen|"
    r"completo|completa|kit|accesorios?|materiales?|aditamentos?|"
    r"solo\s+el\s+aparato|sola\s+la\s+maquina|unicamente\s+el\s+aparato)\b",
    re.IGNORECASE,
)


def _pregunta_por_contenido(texto: str) -> bool:
    """¿El cliente pregunta qué INCLUYE el producto (caja/kit/accesorios)?"""
    return bool(_RE_PREGUNTA_CONTENIDO.search(_normalizar_tildes((texto or "").lower())))


def _familia_de_equipo(nombre: str) -> str | None:
    """Familia de equipo del término/producto, si es un aparato con consumibles."""
    n = _normalizar_tildes((nombre or "").lower())
    for familia in _EQUIPOS_CONSUMIBLES:
        if re.search(rf"\b{re.escape(familia)}\b", n):
            return familia
    return None


# ── ¿EL CLIENTE PIDE UNA FOTO DEL PRODUCTO? ───────────────────────────────────────
# El agente NO puede enviar fotos desde el catálogo (los productos del tenant no traen
# imagen utilizable), así que decir "aquí tienes la foto" es una alucinación; y buscar
# "fotos" en el catálogo devuelve cualquier cosa (FOTORRETIN, linternas).
#
# Caso real (provider 32, 2026-10-27):
#   "Tendrás una foto"            → "Aquí tienes la foto del producto Atamel…" (falsa)
#   "Me puede enviar unas fotos"  → "Actualmente no tenemos linternas en nuestro catálogo"
_RE_PIDE_FOTO = re.compile(
    r"\b(?:"
    r"puede[sn]?|podrias?|podrian|podria|tendras|tendra|tienes|tiene[n]?|"
    r"enviar|envias|envies|enviame|enviarme|mandar|mandas|mandame|mandarme|"
    r"dame|necesito|quisiera|quiero|solicito|solicitar|consigo|hay"
    r")\b[^.?!]{0,45}\b(?:foto|fotos|fotografia|imagen|imagenes)\b"
    r"|\b(?:foto|fotos|imagen|imagenes)\b[^.?!]{0,25}\?",
    re.IGNORECASE,
)

RESPUESTA_FOTO = (
    "¡Con gusto! En un momento le enviaremos la foto del producto solicitado. 🙌"
)


# ── RECETA ESCRITA A MANO ─────────────────────────────────────────────────────────
# La letra a mano NO se lee con fiabilidad: el OCR adivina nombres y el agente acaba
# cotizando fármacos que nadie recetó. Caso real (provider 32, 2026-10-28): receta
# manuscrita de ISTRILAB ("Amonicilina… Broncef… Budicort gotas… Nistatina… Vit D3…").
#
# Regla del usuario: cuando la receta es manuscrita, NO se traduce — se avisa que un
# auxiliar atiende. El marcador lo pone `media.MARCA_MANUSCRITA` cuando el OCR declara
# que el texto es manuscrito.
PREGUNTA_RECOMENDACION = (
    "Para buscarlo bien necesito el nombre o la marca del producto. 🙏 "
    "¿Cuál es? (por ejemplo el champú, la crema o el tratamiento que usas)"
)


RESPUESTA_RECETA_MANUSCRITA = (
    "Recibí tu receta. 🙏 Como está escrita a mano, prefiero no interpretarla antes que "
    "darte un medicamento equivocado.\n"
    "En un momento un auxiliar te atiende y te la cotiza. ✅"
)

RESPUESTA_RECETA_DUDOSA = (
    "Recibí tu receta. 🙏 No logro leerla con seguridad y prefiero no arriesgarme a darte "
    "un medicamento equivocado.\n"
    "En un momento un auxiliar te atiende y te la cotiza. ✅"
)


def _receta_ilegible(user_text: str) -> str | None:
    """Tipo de receta que NO se transcribe: 'mano', 'dudosa' o None.

    El marcador lo pone `media` cuando el OCR parece receta y la comprobación de
    legibilidad dice que no se puede leer con seguridad.
    """
    t = user_text or ""
    if "RECETA ESCRITA A MANO" in t:
        return "mano"
    if "RECETA DE LECTURA DUDOSA" in t:
        return "dudosa"
    return None


def _pide_foto(texto: str) -> bool:
    """¿El cliente pide una FOTO/imagen del producto? (no cuando él la envía)."""
    t = _normalizar_tildes((texto or "").lower())
    if not t:
        return False
    # 'foto' tiene que aparecer; el patrón exige además un verbo de petición o pregunta.
    if "foto" not in t and "imagen" not in t and "fotografia" not in t:
        return False
    return bool(_RE_PIDE_FOTO.search(t))


def _familia_de_pregunta_contenido(
    user_text: str, last_term: str | None, last_product: Any
) -> str | None:
    """Familia del equipo si el cliente pregunta por el CONTENIDO de ESE equipo.

    Devuelve None si no aplica. El caso «el cliente repite el mismo aparato» NO es una
    consulta nueva: `_trae_medicamento_propio` devuelve True porque el mensaje nombra un
    producto ('Digo q si el Glucómetro viene con todos los materiales…'), y sin esta
    excepción el guard no disparaba nunca.
    """
    if not _pregunta_por_contenido(user_text):
        return None
    en_ctx = str(last_term or "")
    if isinstance(last_product, dict):
        en_ctx = f"{en_ctx} {last_product.get('producto') or last_product.get('nombre') or ''}"
    # El equipo puede estar en el contexto (el turno anterior) o nombrado en el propio
    # mensaje: "¿el glucómetro viene con las tiras?" tiene que funcionar también como
    # primera consulta, sin un turno previo que lo haya listado.
    familia = _familia_de_equipo(en_ctx) or _familia_de_equipo(user_text)
    if not familia:
        return None
    # Si además pide OTRO medicamento, manda el flujo normal: no se puede dejar su
    # petición sin atender por contestar la duda del aparato.
    for term in _medicamentos_enumerados(user_text):
        if (
            term
            and _termino_es_medicamento_plausible(term)
            and _familia_de_equipo(term) != familia
        ):
            return None
    return familia


def _respuesta_contenido_equipo(producto: str, consumibles: list[dict[str, Any]]) -> str:
    """Respuesta humana a «¿el aparato viene con los materiales?».

    Honesta en lo que el catálogo NO dice (el contenido de la caja) y ÚTIL en lo que sí
    sabe (los insumos que se venden aparte). Un mostrador contesta exactamente así.
    """
    nombre = (producto or "el aparato").strip().upper()
    partes = [
        f"Te cuento: en el catálogo el {nombre} figura como una unidad y no detalla qué "
        "trae la caja, así que no puedo confirmarte si viene con todos los materiales. "
        "Si quieres, se lo consulto a la farmacia. 🙏"
    ]
    if consumibles:
        partes.append("")
        partes.append("Lo que sí tengo por separado:")
        # Se reutiliza el formateador canónico y se descarta su encabezado: aquí ya lo
        # pone la frase de arriba.
        bloque = _formatear_lista_productos(consumibles, "_")
        partes.append(bloque.split("\n", 1)[1] if "\n" in bloque else bloque)
        partes.append("")
        partes.append("¿Te los agrego al pedido?")
    return "\n".join(partes)


def _titulo_de_lista(titulo: str, productos: list[dict[str, Any]]) -> str:
    """Encabezado de la lista sin el texto de la caja ni el laboratorio.

    Caso real (conv 7, provider 05, 2026-10-27): la foto de una caja de
    `Fexofenadina Clorhidrato CALOX` se leyó como `Medicamento Fexofenadina Clorhidrato
    Calox`, y el encabezado salió tal cual: 'MEDICAMENTO' delante ('Medicamento Genérico'
    es texto de la caja) y la marca detrás.

    El laboratorio NO se detecta por una lista de marcas: se confirma contra los productos
    devueltos con `_es_nombre_de_laboratorio`, que exige que ese token CIERRE la mayoría de
    los títulos. Así 'CALOX' se quita y 'FEXOFENADINA' nunca se toca, porque el fármaco abre
    el título, no lo cierra. Nunca se deja el título vacío ni con una sola palabra.
    """
    palabras = [w for w in re.split(r"\s+", (titulo or "").strip()) if w]
    if not palabras:
        return titulo
    limpio = [
        w for w in palabras
        if _normalizar_tildes(w.lower()).strip(" -.,;:()[]") not in _PALABRAS_DE_CAJA
    ]
    if not limpio:
        return titulo
    if len(limpio) > 2:
        ultimo = _normalizar_tildes(limpio[-1].lower()).strip(" -.,;:()[]")
        if ultimo and (
            _es_nombre_de_laboratorio(ultimo, productos)
            or _cierra_siempre_los_titulos(ultimo, productos)
        ):
            limpio = limpio[:-1]
    return " ".join(limpio).strip() or titulo


def _aviso_dosis_agotada(products: list[dict[str, Any]], dosis: str) -> str:
    """Aviso cuando el cliente pidió una CONCENTRACIÓN que el catálogo no tiene.

    POR QUÉ HACE FALTA: `_filtrar_por_dosis` tiene el mismo fallback que el de envase —
    si nada coincide, devuelve la lista completa para no negar el medicamento. Pero sin
    este aviso el cliente pide 'Acetaminofen de 600 mg' y recibe una lista de 650 y 500 mg
    bajo el título 'ACETAMINOFEN 600MG': cree que le están ofreciendo lo que pidió. Es el
    fallo de «re-listar sin decir el NO», el mismo que ya se arregló para el envase.

    Caso real (conv 7, provider 05, 2026-10-27):
        "Acetaminofen de 600mg" → 11 productos de 650, 500, 120, 150 y 180 mg, sin aviso.

    Devuelve '' si la concentración pedida SÍ está, o si no se puede leer ninguna del
    catálogo (en ese caso no hay nada cierto que prometer).
    """
    if not dosis or not products:
        return ""
    objetivo = dosis.replace(",", ".").strip()
    m = re.match(r"(\d+(?:\.\d+)?)", objetivo)
    if not m:
        return ""
    num = m.group(1)
    solidos: list[str] = []
    liquidos: list[str] = []
    for p in products:
        titulo = str(p.get("producto") or p.get("nombre") or p.get("title") or "")
        conc = _concentracion_de_nombre(titulo)
        if not conc:
            continue
        es_liquido = "/" in conc or re.search(
            r"\b(?:jbe|jarabe|susp|suspension|suspensión|gotas|gts|solucion|solución)\b",
            titulo,
            re.IGNORECASE,
        )
        destino = liquidos if es_liquido else solidos
        if conc not in destino:
            destino.append(conc)
    if not solidos and not liquidos:
        return ""
    vistos = {c.split()[0].replace(",", ".") for c in solidos + liquidos}
    if num in vistos:
        return ""

    def orden(lista: list[str]) -> list[str]:
        try:
            return sorted(lista, key=lambda c: float(c.split()[0].replace(",", ".")))
        except (ValueError, IndexError):
            return lista

    solidos, liquidos = orden(solidos), orden(liquidos)
    # La pedida se muestra con unidad si el cliente la escribió; si no, con la unidad de
    # la primera alternativa disponible (es la familia del mismo fármaco).
    referencia = (solidos or liquidos)[0]
    pedida = objetivo if re.search(r"[a-z]", objetivo) else f"{num} {referencia.split()[1]}"
    if solidos and liquidos:
        detalle = (
            f"{', '.join(solidos)}. También en jarabe/gotas: {', '.join(liquidos)}"
        )
    else:
        detalle = ", ".join(solidos or liquidos)
    return f"⚠️ No tengo de {pedida}. Lo tengo en: {detalle}."


def _aviso_envase_agotado(products: list[dict[str, Any]], envase: str) -> str:
    """Aviso cuando el cliente pidió un envase que NO existe: 'Lo tengo en: 30 unidades.'

    POR QUÉ HACE FALTA: `_filtrar_por_envase` tiene un fallback —si ningún producto tiene el
    envase pedido, devuelve la lista completa— para no negar el medicamento. Pero sin este
    aviso el cliente ve el envase de 30 SIN QUE NADIE LE DIGA que el de 60 que pidió no
    está: no puede distinguir «no lo tengo en 60» de «no me entendió». Es el mismo fallo que
    ya se arregló en el guard de envase (`_respuesta_envase_agotado`): re-listar sin decir el
    NO es no responder.

    Caso real (conv 2109, provider 19): el cliente pidió 'flextrin x 60' y el catálogo solo
    tiene FLEXTRIN X 30 CAPS BLANDAS. Recibía el de 30 a secas.

    Devuelve '' si no hay nada que avisar (el envase pedido SÍ está, o no se pidió ninguno).
    """
    if not envase or not products:
        return ""
    tamanos: list[str] = []
    for p in products:
        tam = _envase_de_nombre(
            p.get("producto") or p.get("nombre") or p.get("title")
        )
        if tam and tam not in tamanos:
            tamanos.append(tam)
    if not tamanos or envase in tamanos:
        return ""
    try:
        tamanos = sorted(tamanos, key=int)
    except ValueError:
        tamanos = sorted(tamanos)
    detalle = ", ".join(f"{t} unidades" for t in tamanos)
    return f"⚠️ No tengo envase de {envase} unidades. Lo tengo en: {detalle}."


def _envase_pedido_en_linea(linea: str) -> str:
    """El envase que el cliente pidió en ESA línea de la consulta.

    Se prueba primero `_pedido_de_envase` (que exige la forma escrita: 'de 10 pastillas',
    '30 tabletas') y solo si no hay nada se cae a la forma 'X N', que es como escribe la
    mayoría: 'x 30 Tab', 'metformina de 500 x 30', 'flextrin x 60'.

    POR QUÉ NO SE TOCA `_pedido_de_envase`: esa función alimenta el GUARD de envase
    agotado, que decide cuándo se le responde al cliente "no lo tengo en envase de N".
    Ampliarla cambiaría cuándo dispara ese mensaje. Aquí solo se necesita saber el número
    para filtrar, así que la ampliación se queda local.
    """
    pedido = _pedido_de_envase(linea)
    if pedido:
        return pedido
    # La forma 'X N' se resuelve con `_envase_de_nombre`, que ya sabe que la 'x' final
    # del nombre NO es separador ('evigax 125 mg' → 125 es DOSIS, no 125 cajas) y que
    # un número con unidad de peso/volumen tampoco es envase. Antes había aquí una
    # segunda copia del parseo, sin esas salvaguardas: el mismo texto se leía bien por
    # un camino y mal por el otro (caso conv 7, provider 05).
    return _envase_de_nombre(linea)


# Regex de la DOSIS escrita ANTES del envase: en "<nombre> <n> x <m>", el número que
# precede al 'x' es la dosis y el que sigue es el tamaño de caja.
_RE_DOSIS_ANTES_DE_ENVASE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*x\s*\d+\b", re.IGNORECASE)


def _dosis_antes_del_envase(texto: str) -> str:
    """La DOSIS del patrón '<nombre> <dosis> x <envase> <forma>' — sin unidad escrita.

    El cliente suele escribir la dosis SIN 'mg' cuando ya la acompaña del envase:
        'candesatan 16 x 30 Tab'   → 16 es la dosis (candesartán 16 mg), 30 el envase
        'metformina de  500 x 30'  → 500 la dosis, 30 el envase
    `_extraer_dosis` exige la unidad (`4 MG`), así que en estos dos casos no devolvía
    nada y el filtro de concentración NO se aplicaba: el cliente pedía metformina 500 y
    recibía 850, 1000 y hasta 1 MG mezclados (caso real conv 2109, provider 19).

    El 'x' es el desambiguador que da el propio cliente: el número ANTES es la dosis y
    el DE DESPUÉS el envase (`_pedido_de_envase`). Sin 'x' no se asume nada:
    'flextrin x 60' no tiene dosis, y '2 cajas de metformina 500' lo cubre el guard de
    cantidad de compra.
    """
    m = _RE_DOSIS_ANTES_DE_ENVASE.search(texto or "")
    return m.group(1) if m else ""


# --- RECLAMO: el cliente corrige lo que YA recibió; no pide nada nuevo ---
# Caso real (conv cv_5xcp1rqrsxiegmd4evcz):
#     cliente: "Buenos días, eso no es lo que pedi"
#     agente : "⚠️ No disponibles en el catálogo: DÍAS" + "PEDI" + 20 PEDIATRICOS
# El mensaje es un RECLAMO. `_parece_lista_medicamentos` no lo cubría porque solo excluye
# negativa y despedida, y un reclamo no es ninguna de las dos. Al trocearlo por la coma, las
# piezas ('días', 'pedi') se consultaban como medicamentos.
_RE_RECLAMO = re.compile(
    r"(?:eso|esto|ese|esa)\s+no\s+(?:es|era|fue)\s+lo\s+que"
    r"(?:\s+(?:pedi|pedí|queria|quería|quiero|solicite|solicité|pedia|pedir|mande|envie))?"
    r"|no\s+es\s+lo\s+que\s+(?:pedi|pedí|queria|quería|quiero|solicite|solicité)"
    r"|no\s+(?:era|es)\s+(?:eso|esto|lo\s+que)"
    r"|yo\s+(?:pedi|pedí|queria|quería)\s+(?:otra|otro|esto|eso)"
    r"|me\s+(?:diste|mando|enviaste|dieron)\s+otra\s+cosa"
    r"|(?:otra|otro)\s+vez\s+(?:mal|equivocado)"
    r"|(?:esta|está)\s+mal\s+(?:la\s+)?(?:respuesta|opción|busqueda|búsqueda)"
    r"|no\s+era\s+(?:eso|ese|esta|este)",
    re.IGNORECASE,
)

# --- CORTESÍA EXPLICATIVA: bendiciones y buenos deseos, sin datos de producto ---
# Caso real (misma conversación, 02-Oct), cliente:
#     "Buen día, y dios le bendiga su día, si puedes por favor verificar el nombre se
#      escribe así somazina, es del grupo leti"
# el agente buscó 'DEL GRUPO LETI' y 'DÍA' y respondió con 20 PEDIATRICOS. La consulta real
# (SOMAZINA) ya se había atendido; esto es conversación.
#
# NO se filtra 'día'/'dias' como palabra: medido, es token de 'LINOFEME COMPRIMIDOS 21 DIAS'.
# Se detecta la FRASE de bendición.
_RE_BENDICION = re.compile(
    r"(?:dios\s+le\s+bendiga|que\s+dios|bendiciones|dios\s+te\s+bendiga"
    r"|que\s+(?:tengas?|pases?)\s+(?:un\s+)?(?:buen|feliz|lindo))",
    re.IGNORECASE,
)


def _es_reclamo_o_cortesia(texto: str) -> bool:
    """True si el mensaje es un RECLAMO o CORTESÍA, no una consulta de medicamento.

    POR QUÉ: estos mensajes son conversación y el troceo por comas convierte sus pedazos en
    "medicamentos" que se consultan y se presentan como no disponibles, con una lista de
    productos que nadie pidió. Dos casos reales (conv cv_5xcp1rqrsxiegmd4evcz):
        "Buenos días, eso no es lo que pedi"          → buscó 'días', 'pedi'
        "Buen día, y dios le bendiga su día, ... es del grupo leti" → buscó 'del grupo leti', 'día'
    en ambos el cliente recibió 20 productos PEDIATRICOS.

    NO basta con filtrar las palabras: medido contra el catálogo, 'pedi' es token de
    'APETININ JBE PEDI' y 'dias' de 'LINOFEME COMPRIMIDOS 21 DIAS'. Hay que reconocer el
    MENSAJE, no la palabra.

    Un reclamo o una bendición no piden un producto NUEVO. Si además trae un fármaco real
    ('Buenos días, necesito atamel forte'), el término se busca por el camino normal.
    """
    if not texto:
        return False
    t = texto.strip()
    return bool(_RE_RECLAMO.search(t) or _RE_BENDICION.search(t))


def _medicamentos_con_dosis(texto: str) -> list[tuple[str, str]]:
    """Medicamentos de una consulta enumerada, cada uno con la dosis que el cliente pidió.

    Devuelve [(termino_limpio, dosis_sin_unidad)]. La dosis va SEPARADA del término a
    propósito: el término debe ir limpio para que la cascada de typos resuelva el nombre
    (medido: pegar la dosis convierte 'hidrocoticida 12.5 mg' en 'hidrocoticida 12 5 mg' y
    ya no encuentra el fármaco), y la dosis se aplica después filtrando los productos.

    Caso real (provider 05): "Dame precio de hidrocoticida de 12.5 / Cardesartan de 16 /
    Cardevidol 6.25" — el cliente dijo la dosis de cada uno y recibía todas las
    concentraciones mezcladas.
    """
    if not texto:
        return []
    pares: list[tuple[str, str]] = []
    vistos: set[str] = set()
    for linea in _lineas_lista_medicamentos(texto):
        limpia = _sin_motivo(linea)
        term = _extraer_termino_medicamento(limpia)
        if not term or not _termino_es_medicamento_plausible(term):
            continue
        # La dosis se busca en la LÍNEA (donde vive el número), no en el término ya limpio.
        # OJO: tiene que ser la LÍNEA. El término limpio ya perdió el número cuando el
        # cliente escribió la dosis sin unidad ('candesatan 16 x 30 Tab' → 'candesatan
        # 30 tab'), así que ahí no hay nada que encontrar.
        # Y `_dosis_antes_del_envase` va ANTES de los respaldos sobre el TÉRMINO: el
        # término puede acabar con el tamaño de caja ('metformina 30') y colarse como si
        # fuera la concentración, pisando el '500' que sí está en la línea.
        dosis = (
            _extraer_dosis(linea)
            or _dosis_antes_del_envase(linea)
            or _dosis_sin_unidad(limpia)
            or _dosis_sin_unidad(term)
        )
        if term in vistos:
            continue
        vistos.add(term)
        pares.append((term, dosis))
    return pares


def _parece_lista_medicamentos(texto: str) -> bool:
    """True si la consulta ENUMERA 2+ medicamentos (aunque pida el precio).

    'esoz, leprit y evigax'                          → True
    'Precio de valsartan 80 hidroclorotiazida y omeprazol' → True  ← el caso reportado
    'tienes atamel forte?'                          → False (consulta simple)
    'precio del atamel'                             → False (un solo medicamento)

    OJO — el verbo de consulta NO descalifica la lista. Antes esto cortaba con False en
    cuanto aparecía 'precio'/'tienes', así que pedir el precio de VARIOS medicamentos se
    trataba como consulta simple y el agente respondía solo UNO. Lo que decide es
    CUÁNTOS medicamentos distintos menciona, no el motivo por el que los pide.
    """
    if not texto:
        return False
    # Una NEGATIVA o despedida NUNCA es una lista. Sin este guard,
    # "No gracias no las voy a comprar y disculpe" se partía por la 'y' y el agente
    # respondía "No disponibles: DISCULPE VOY COMPRAR" más una lista de chocolates.
    if _es_negativa_o_despedida(texto):
        return False
    # UN RECLAMO O UNA CORTESÍA TAMPOCO ES UNA LISTA. Casos reales (conv
    # cv_5xcp1rqrsxiegmd4evcz): "Buenos días, eso no es lo que pedi" buscó 'días' y 'pedi', y
    # "y dios le bendiga su día ... es del grupo leti" buscó 'del grupo leti' y 'día'. En
    # ambos el cliente recibió 20 productos PEDIATRICOS que no pidió. Es conversación, y sus
    # pedazos parecen medicamentos solo por el troceo.
    #
    # OJO: si el mensaje trae TAMBIÉN un fármaco real, sigue siendo consulta. Se exige que
    # no haya un término de medicamento plausible en el texto.
    if _es_reclamo_o_cortesia(texto):
        # Se QUITAN los tramos que son reclamo/bendición y se mira si queda algún fármaco.
        # Sin quitarlos, 'Buenos días, eso no es lo que pedi' dejaba el resto
        # 'buenos dias pedi', que pasa el filtro de plausibilidad (3 palabras, ninguna
        # funcional) aunque no sea un medicamento. Al quitar el reclamo queda 'Buenos días,'
        # y ahí sí se ve que no pide nada.
        #
        # Así "no es lo que pedí, necesito atamel forte" SIGUE siendo consulta: al quitar el
        # reclamo queda 'necesito atamel forte', con un fármaco real.
        resto = _RE_BENDICION.sub(" ", _RE_RECLAMO.sub(" ", texto))
        resto = _limpiar_termino_medicamento(_sin_motivo(resto)) or ""
        if not _termino_es_medicamento_plausible(resto):
            return False

    # Con separadores de enumeración: contar los medicamentos de cada trozo.
    if _RE_SEP_LISTA.search(texto):
        return len(_medicamentos_enumerados(texto)) >= 2

    # Sin separadores: varias LÍNEAS con medicamento (receta escrita en vertical).
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    if len(lineas) >= 2:
        return len(_parsear_medicamentos_receta(_sin_motivo(texto))) >= 2
    return False


# Conectores de una consulta con DOS medicamentos en la misma frase.
_CONECTORES_MULTI = re.compile(
    r"\s*(?:,|;|\by\b|\btambien\b|\btambién\b|\bademas\b|\bademás\b)\s*",
    re.IGNORECASE,
)

# Ingredientes de CO-FORMULACIÓN: cuando un lado del conector tiene SOLO estos
# tokens, no es un medicamento aparte sino parte del mismo producto
# (amoxicilina + ácido clavulánico = AUGMENTIN; losartán + hidroclorotiazida;
# vitamina C + zinc). Partirlos mostraría dos listas ajenas al pedido y
# declararía "no disponible" uno de los componentes.
_COFORMULACION = {
    "acido", "clavulanico", "clavulanic", "tazobactam", "sulbactam",
    "cilastatina", "hidroclorotiazida", "trimetoprim", "sulfametoxazol",
    "carbidopa", "benserazida", "cafeina", "zinc", "calcio", "magnesio",
    "hierro", "potasio", "sodio", "fenilefrina", "clorfeniramina", "pseudoefedrina",
}


def _partir_por_conectores(texto: str) -> list[str]:
    """F14.5 — divide por conectores cuando AMBOS lados tienen fármaco.

    Caso real del cliente (C5): "Buenas tardes!! Mi amor que precio tiene la
    nitrofurantoina de 50mg y la vitamina C" son DOS consultas, pero se
    concatenaban en un término ("amor nitrofurantoina 50mg vitamina") y el
    catálogo buscaba una frase que no existe.

    Solo se parte si, tras limpiar cada lado, los DOS tienen un token con
    cuerpo de fármaco (así "tienes atamel y su precio" sigue siendo UNA
    consulta) y ninguno es solo co-formulación. Devuelve [] si no aplica.
    """
    trozos = [p.strip() for p in _CONECTORES_MULTI.split(texto) if p.strip()]
    if len(trozos) < 2:
        return []
    terminos: list[str] = []
    vistos: set[str] = set()
    for trozo in trozos:
        term = _extraer_termino_medicamento(trozo)
        if not term:
            continue
        tokens = tokens_posible_farmaco(term)
        if not tokens:
            continue
        if all(t in _COFORMULACION for t in tokens):
            # Un lado que es SOLO co-formulación: no es una consulta aparte.
            return []
        if term not in vistos:
            vistos.add(term)
            terminos.append(term)
    return terminos if len(terminos) >= 2 else []


def _partir_consulta_multi(texto: str) -> list[str]:
    """Divide una consulta multi-medicamento en UNA línea sin separadores.

    F14.5 — PRIMERO por conectores con fármaco a ambos lados:
    'que precio tiene la nitrofurantoina de 50mg y la vitamina C'
    → ['nitrofurantoina 50mg', 'vitamina c']

    Patrón clásico: 'de <dosis>' repetido 2+ veces separa medicamentos.
    'quiero saber si disponen de clopidogrel de 75 losartan de 50
    atorvastatina de 30 nifedipina de 10 mg'
    → ['clopidogrel', 'losartan', 'atorvastatina', 'nifedipina']

    Devuelve [] si hay menos de 2 medicamentos (consulta simple).
    """
    if not texto:
        return []
    t = texto.strip()
    por_conector = _partir_por_conectores(t)
    if len(por_conector) >= 2:
        return por_conector
    # Ocurrencias de 'de <número>' (dosis): cada una precede a un medicamento.
    matches = list(
        re.finditer(
            r"\bde\s+(\d{1,3})(?:\s*(?:mg|g|mcg|ml|mili|gramos))?", t, re.IGNORECASE
        )
    )
    if len(matches) < 2:
        return []
    # El primer medicamento está ANTES del primer match; los siguientes entre
    # matches consecutivos. La dosis ('de 75') pertenece al medicamento que la
    # precede ('clopidogrel de 75' → 'clopidogrel 75').
    trozos: list[str] = []
    inicio = 0
    for i, m in enumerate(matches):
        trozos.append(t[inicio:m.start()])
        inicio = m.end()
    trozos.append(t[inicio:])  # resto tras el último match
    dosis = [m.group(1) for m in matches]  # 75, 50, 30, 10
    terminos: list[str] = []
    vistos: set[str] = set()
    for i, trozo in enumerate(trozos[:-1]):
        term = _extraer_termino_medicamento(trozo)
        if not term:
            continue
        # La dosis que sigue es parte del término (si no está ya incluida).
        if not re.search(r"\b" + dosis[i] + r"\b", term):
            term = f"{term} {dosis[i]}"
        if term not in vistos:
            vistos.add(term)
            terminos.append(term)
    return terminos if len(terminos) >= 2 else []


def _lineas_lista_medicamentos(texto: str) -> list[str]:
    """Convierte una lista de medicamentos en texto en líneas individuales.

    'esoz, leprit y evigax' → ['esoz', 'leprit', 'evigax']
    'ESOZ\nLEPRIT\nEVIGAX' → ['ESOZ', 'LEPRIT', 'EVIGAX']
    """
    t = texto.strip()
    if re.search(r"[,;]", t) or re.search(r"\s+y\s+", t.lower()):
        # Separar por comas/puntos y coma, luego por 'y' como conector.
        trozos = re.split(r"[,;]+|\s+y\s+", t, flags=re.IGNORECASE)
        return [p.strip() for p in trozos if p.strip()]
    return [l.strip() for l in t.splitlines() if l.strip()]


def _es_linea_notificacion_admin(linea: str) -> bool:
    """True si la línea es METADATO de una notificación del sistema (reserva,
    confirmación, recordatorio) y NO un medicamento.

    Caso real: la notificación de reserva de demo llegaba al WhatsApp del agente
    y se procesaba como RECETA —

        Se ha realizado una reserva para una demo:
        *Fecha:* 2/10/2026 a las 10:00 AM
        *Nombre:* Madelaine Altamiranda
        *Farmacia:* FARMAUNO

    → el agente respondía "⚠️ No disponibles en el catálogo: FECHA, NOMBRE
    MADELAINE ALTAMIRANDA, FARMACIA FARMAUNO".

    Detecta: (a) etiquetas con dos puntos ("Fecha:", "*Nombre:*", "Teléfono ="…),
    y (b) los propios nombres de campo de una reserva, sin necesidad de dos
    puntos (una línea suelta "Farmacia FARMAUNO" tampoco es un fármaco).
    """
    if not linea:
        return True
    t = linea.strip().lower()
    if not t:
        return True
    # (a) Etiqueta: "algo:" / "*algo:*" / "algo = valor" al inicio de la línea.
    # Cualquier campo con dos puntos es un metadato, no un nombre de fármaco
    # (los medicamentos no se escriben "ESOZ:" en una receta).
    if re.match(r"^[\s*_>-]*[a-záéíóúüñ][a-záéíóúüñ\s]{1,24}[\s*_]*\s*[:=]", t):
        return True
    # (b) Campos típicos de una reserva/notificación, con o sin dos puntos.
    if re.match(
        r"^[\s*_>-]*(?:fecha|nombre|nombres|apellido|apellidos|farmacia|"
        r"tel[eé]fono|telefonos?|celular|whatsapp|contacto|correo|email|e-?mail|"
        r"direcci[oó]n|hora|horario|d[ií]a|sede|sucursal|ciudad|pa[ií]s|"
        r"c[eé]dula|rif|responsable|paciente|cliente|asunto|motivo|referencia|"
        r"c[oó]digo|reserva|pedido|cita|demo|precio|total|monto|estatus|estado|"
        r"observaci[oó]n(?:es)?|nota|comentario)\b",
        t,
    ):
        return True
    # (c) Encabezados de la propia notificación.
    if re.search(
        r"(se ha realizado una reserva|reserva para una demo|"
        r"ha reservado una demo|nueva reserva|reserva confirmada|"
        r"reserva de demo)",
        t,
    ):
        return True
    return False


def _es_solo_presentacion(linea: str) -> bool:
    """True si la línea es SOLO dosis/presentación sin nombre de fármaco.

    '120 MG' / '10 TABLETAS RECUBIERTAS' / 'X 10 TAB' → True (fragmentos de
    presentación del MISMO medicamento, no medicamentos nuevos).
    'FEXOFENADINA CLORHIDRATO 120 MG' → False (tiene el fármaco).

    Un OCR de una caja de un solo medicamento suele dividirse en líneas:
    'FEXOFENADINA CLORHIDRATO' / '120 MG' / '10 TABLETAS RECUBIERTAS'.
    Las dos últimas no son medicamentos independientes — se descartan para
    no consultar '120 mg' ni '10 tabletas recubiertas' como si fueran
    fármacos (devolvían resultados irrelevantes).
    """
    if not linea:
        return True
    t = linea.strip().lower()
    # Quitar números y unidades de dosis/presentación.
    palabras = re.findall(r"[a-záéíóúüñ]+", t)
    if not palabras:
        return True  # solo números/símbolos
    # Palabras de presentación/dosis genéricas (no identifican fármaco).
    presentacion = {
        "mg", "ml", "g", "mcg", "ui", "x", "tab", "tabs", "tableta",
        "tabletas", "comprimido", "comprimidos", "capsula", "capsulas",
        "cap", "ampolla", "ampollas", "amp", "frasco", "frascos", "vial",
        "viales", "sobre", "sobres", "tubo", "tubos", "jarabe", "susp",
        "suspension", "gotas", "gota", "crema", "unguento", "polvo",
        "recubierta", "recubiertas", "recubierto", "recubiertos", "ped",
        "pediatrico", "pediatrica", "oral", "topica", "topico", "solucion",
        "inyectable", "spray", "inhalador", "granulado", "granulados",
        "pastilla", "pastillas", "blister", "blíster", "gragea", "grageas",
        "unidad", "unidades", "pieza", "piezas", "pack", "fco", "fcos",
    }
    # Si TODAS las palabras son de presentación → es solo presentación.
    return all(w in presentacion for w in palabras)


def _parsear_medicamentos_receta(texto: str) -> list[str]:
    """Extrae la lista de medicamentos de un texto de receta (OCR o lista en
    texto). Cada línea con contenido es un medicamento candidato; se normaliza
    con _extraer_termino_medicamento y se descartan líneas sin sustancia.

    'ESOZ 40 MG\\nLEPRIT 25 MG\\nBUMETIN RETARD 300 MG'
    → ['esoz 40 mg', 'leprit 25 mg', 'bumetin retard 300 mg']
    """
    if not texto:
        return []
    out: list[str] = []
    vistos: set[str] = set()
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        # Línea que es SOLO dosis/presentación (sin fármaco): fragmento del
        # MISMO medicamento (OCR de caja), no un medicamento nuevo.
        if _es_solo_presentacion(linea):
            continue
        # Metadato de una notificación del sistema (reserva/cita/confirmación):
        # "*Fecha:* ...", "*Nombre:* ...", "*Farmacia:* ..." NO son medicamentos.
        if _es_linea_notificacion_admin(linea):
            continue
        # Líneas que parecen instrucciones de la receta, no medicamentos.
        if re.fullmatch(
            r"[\d.,\s/]+|(?:tomar|tomese|aplicar|aplicarse|por\s+las?\s+"
            r"(?:manana|tarde|noche)|cada\s+\d+|una?\s+(?:vez|tableta|"
            r"capsula|sobre)s?\s+al\s+d[ií]a).*",
            linea.lower(),
        ):
            continue
        # Campos de ETIQUETA del producto (no medicamentos): los OCR de
        # prospectos/cajas capturan "CONCENTRACIÓN 120 MG, PRESENTACIÓN 10
        # TABLETAS RECUBIERTAS" como líneas — descartarlas.
        if re.match(
            r"^\s*(?:concentraci[oó]n|presentaci[oó]n|registro\s+(?:sanitario|n[oó])|"
            r"laboratorio|fabricante|casa\s+(?:farmac|productor)|"
            r"via\s+de\s+administraci[oó]n|condici[oó]n\s+de\s+(?:venta|dispensaci[oó]n)|"
            r"uso\s+(?:oral|topico|t[oó]pico)|indicaci[oó]n|contraindicaci[oó]n|"
            r"precaucion|advertencia|conservaci[oó]n|fecha\s+de\s+(?:vencimiento|elaboraci[oó]n)|"
            r"lote|expediente|principio\s+activo\s*:?)\b",
            linea.lower(),
        ):
            continue
        term = _extraer_termino_medicamento(linea)
        # Descartar términos que NO parecen medicamentos (frases de contexto,
        # conceptos como 'contrato', 'página', 'chat', 'comparador'). Sin este
        # filtro, un mensaje como "mañana conversamos para dar inicio formal
        # del contrato de la página y el chat y el comparador" se trataba como
        # receta y el agente respondía con una lista de medicamentos.
        if term and not _termino_es_medicamento_plausible(term):
            continue
        # LA DOSIS CON UNIDAD SE CONSERVA; LA QUE VA SIN UNIDAD **NO** SE PEGA AQUÍ.
        #
        # `_extraer_termino_medicamento` ya conserva '20 mg' (número + unidad). Pero
        # cuando el cliente escribe la dosis SIN 'mg' ("Cardesartan de 16"), el número se
        # descarta y el término queda sin dosis.
        #
        # NO se reinyecta aquí a propósito: se MEDIÓ que pegar la dosis rompe la corrección
        # de typos. `_quitar_saludos` convierte el decimal en un espacio
        # ('hidrocoticida 12.5 mg' → 'hidrocoticida 12 5 mg') y la cascada de typos ya no
        # encuentra el fármaco → 0 productos. El typo necesita el nombre LIMPIO.
        #
        # La dosis se aplica DESPUÉS, filtrando los productos devueltos
        # (`_filtrar_por_dosis` en el bucle de la receta). Ver `_dosis_sin_unidad`.
        if term and term not in vistos:
            vistos.add(term)
            out.append(term)
    return out


def _formatear_receta(
    grupos: list[tuple[str, list[dict[str, Any]]]],
    no_disponibles: list[str] | None = None,
    avisos: dict[str, str] | None = None,
) -> str:
    """Genera la respuesta de receta en formato determinista, con numeración
    GLOBAL corrida entre medicamentos:

        ESOZ
        💊 1. ESOZ (ESOMEPRAZOL) 20 MG X 7 CAP
           $3,47  |  Bs 2.617,23
        💊 2. ESOZ 40MG X 7 CAPSULAS PHARMATIQUE
           $5,54  |  Bs 4.184,62

        LEPRIT
        💊 3. LEPRIT 25 MG X 30 TAB (E) PHARMEQUITE
           $7,57  |  Bs 5.716,90

    Sin nombre de farmacia: se consulta la BD de un solo providerId.
    Si hay medicamentos de la receta que NO están en el catálogo, se avisa al
    inicio (p. ej. "No disponibles: BUMETIN, DAFLON") antes de la lista.
    """
    if not grupos:
        return ""
    lineas: list[str] = []
    if no_disponibles:
        nombres = ", ".join(m.upper() for m in no_disponibles)
        lineas.append(f"⚠️ No disponibles en el catálogo: {nombres}")
        lineas.append("")
    n = 0
    for titulo, products in grupos:
        if not products:
            continue
        ordenados = sorted(
            products,
            key=lambda p: (
                p.get("precio") if isinstance(p.get("precio"), (int, float)) else 0
            ),
        )
        lineas.append(titulo.strip().upper())
        # El aviso va pegado a su medicamento: el cliente acaba de leer el nombre y
        # necesita saber que el envase que pidió no está, ANTES de la lista.
        _aviso = (avisos or {}).get(titulo.strip().upper())
        if _aviso:
            lineas.append(_aviso)
            lineas.append("")
        for p in ordenados:
            n += 1
            nombre = str(p.get("producto") or p.get("title") or "").strip()
            usd = p.get("precio")
            bs = p.get("precioBs")
            usd_s = f"${_fmt_ve(usd)}" if isinstance(usd, (int, float)) else "$—"
            bs_s = f"Bs {_fmt_ve(bs)}" if isinstance(bs, (int, float)) else "Bs —"
            lineas.append(f"💊 {n}. {nombre}")
            lineas.append(f"   {usd_s}  |  {bs_s}")
        lineas.append("")
    # Cierre con el flujo del carrito: cómo pedir cantidades, pedir otro
    # medicamento y ver el resumen.
    lineas.append("")
    lineas.append("👉 Para agregar al carrito: quiero X cajas de la opción Z")
    lineas.append("   Ejemplo: quiero 2 cajas de la opción 3")
    lineas.append("🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.")
    lineas.append("✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.")
    return "\n".join(lineas).rstrip()


def _extraer_termino_medicamento(texto: str) -> str | None:
    """Extrae el término de búsqueda: TODAS las palabras que no son verbos de
    consulta ni relleno, unidas. 'tienes atamel forte?' -> 'atamel forte'.
    'tienes acido folico de 10 mg' -> 'acido folico 10 mg' (incluye mg/ml).

    F14.1: también se quitan los APELATIVOS de cortesía ('mi amor',
    'mami', 'corazón'), que hasta ahora solo se limpiaban en las notas de voz.
    """
    if not texto:
        return None
    # La FRASE de indicación ('tos seca', 'dolor de cabeza') no es parte del nombre: describe
    # para qué sirve. Sin quitarla, el TÍTULO y el término arrastraban la enfermedad
    # ('BROXOL TOS SECA') y la búsqueda se degradaba de 11 productos a 3 basura.
    texto = _sin_frase_de_indicacion(
        texto.lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u")
    )
    # 'de la opción 9' es una REFERENCIA A LA LISTA, no una dosis: el '9' no puede entrar
    # en el término (el catálogo recibía 'macrodantina vitamina c 9').
    texto = re.sub(r"\b(?:opci[oó]n|opciones|n[uú]mero)\s+\d+", " ", texto)
    palabras = re.findall(r"[a-záéíóúüñ0-9]+", texto)
    # La unidad HABLADA se normaliza ANTES de decidir ("nifedipina 30 miligramos"
    # → "nifedipina 30 mg"). Sin esto el 30 se descarta por ir seguido de una
    # palabra que no está en la lista de unidades y la dosis se pierde.
    palabras = [_normalizar_unidad(w) for w in palabras]
    # Verbos de consulta, relleno y apelativos de cortesía: nunca son parte del
    # medicamento.
    verbos = set(re.findall(r"[a-záéíóúüñ]+", _VERBOS_MEDICAMENTO.pattern))
    excluidas = (
        _FILLER | verbos | _VIAS_ADMINISTRACION | _CATEGORIAS_TERAPEUTICAS
        | _APELATIVOS
    )
    # Unidades de dosis que SÍ son parte del término: mg, ml, mcg, gotas, etc.
    unidades = {"mg", "ml", "mcg", "gotas", "ampolla", "ampollas", "jarabe",
                "tabletas", "tab", "capsulas", "cap", "crema", "spray",
                "suspension", "supositorio", "inyectable"}
    terminos: list[str] = []
    for i, w in enumerate(palabras):
        # Unidades (mg, ml, tab...) son cortas pero válidas
        if w in unidades:
            terminos.append(w)
            continue
        # LA LETRA DE LA VITAMINA. 'la vitamina C' se quedaba en 'vitamina' y el cliente
        # recibía 20 productos de Vitamina E, K, B12 y colágeno (provider 32, 2026-10-29).
        if _es_letra_de_vitamina(w, " ".join(palabras)):
            terminos.append(w)
            continue
        if len(w) < 3:
            # Dígito (1 o más cifras) seguido de unidad de dosis: incluir.
            # Cubre "5 mg" (1 cifra) y "10 mg" (2 cifras).
            if (
                w.isdigit()
                and i + 1 < len(palabras)
                and palabras[i + 1] in unidades
            ):
                terminos.append(w)
                continue
            # CÓDIGO del nombre ('d3', 'k2', 'b6'): corto pero identifica el producto.
            # Sin esto, 'vitamina d3/k2' se reducía a 'vitamina' y el cliente recibía
            # multivitaminas, B12 y hasta JABÓN CON PROVITAMINA B5. (Hay una regla
            # gemela en `_limpiar_termino_medicamento`; las dos limpian el término por
            # caminos distintos y las dos tienen que conservarlo.)
            if any(c.isdigit() for c in w):
                terminos.append(w)
            continue
        if _sin_tildes(w) in excluidas:
            continue
        if w.isdigit():
            if i + 1 < len(palabras) and palabras[i + 1] in unidades:
                terminos.append(w)
            continue
        terminos.append(w)
    if not terminos:
        return None
    # PRIMERAS palabras funcionales FUERA: 'el precio DEL fulgran' deja 'del fulgran'.
    # Se quitan solo al INICIO (nunca en medio: 'del' vive dentro de nombres reales
    # como 'JUGO DEL VALLE'). Se midió que ningún producto del catálogo EMPIEZA por
    # estas palabras, así que quitarlas aquí no puede borrar un término legítimo.
    while terminos and terminos[0] in _FILLER_INICIAL:
        terminos.pop(0)
    # Igual al final: 'naproxeno por favor' ya está cubierto por _FILLER, pero una
    # preposición suelta al cierre ('... de') no aporta nada al matcher.
    while terminos and terminos[-1] in _FILLER_INICIAL:
        terminos.pop()
    if not terminos:
        return None
    return " ".join(terminos)
