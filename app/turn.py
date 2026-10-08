"""Orquestación del turno conversacional.

Gate → contexto del CRM → LLM con tools → envío vía CRM → ficha/fase/seguimiento.
Degradación silenciosa: cualquier fallo termina en silencio + log (y handoff
`error` si el LLM se agotó) — jamás texto roto al lead (Constitución IV).
"""
from __future__ import annotations

import asyncio
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
from app.relevancia import es_relevante
from app.stall import ALERTA as STALL_ALERT, racha_vacia, sin_rumbo
from app.profile import resolve_profile
from app.prompt import build_system_prompt
from app.state import (
    AppContext,
    Conversation,
    InboundMessage,
    CartItem,
    utcnow,
)
from app.tools import (
    ToolRuntime,
    active_tool_schemas,
    _formatear_lista_productos,
    _fmt_ve,
    _normalizar_tildes,
    _normalizar_unidad,
    _termino_es_medicamento_plausible,
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
        if m.text:
            parts.append(m.text)
            continue
        # Un mensaje con imagen (base64 inyectada por el puente Evolution o
        # media_id de Meta) se procesa como media aunque type sea "text" y el
        # texto venga vacío (foto de receta sin caption). Igual para audio.
        if m.image_base64 or m.media_id or m.audio_base64:
            part = await media.describe_item(ctx, m)
            if part.text:
                parts.append(part.text)
            if part.image_data_uri:
                image_uris.append(part.image_data_uri)
            continue
        if m.type in ("text", "button", "interactive"):
            continue  # texto vacío raro: nada que procesar
        part = await media.describe_item(ctx, m)
        if part.text:
            parts.append(part.text)
        if part.image_data_uri:
            image_uris.append(part.image_data_uri)
    if not parts and not image_uris:
        logger.info("turno %s: nada procesable en la ráfaga — silencio", identity)
        return

    user_text = "\n".join(parts)
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
    prev_last_ocr_term = runtime.last_ocr_term
    # Lista de opciones persistida del turno anterior (para resolver
    # "quiero X cajas de la opción Z").
    if isinstance(conv.last_options, list):
        runtime.last_options = conv.last_options
    elif isinstance(conv.last_options, str) and conv.last_options.strip():
        try:
            runtime.last_options = json.loads(conv.last_options)
        except Exception:
            runtime.last_options = []
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
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_products_term or runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

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
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_products_term or runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

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
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_products_term or runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

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
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_products_term or runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

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
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_products_term or runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

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
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_products_term or runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

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
    if farmacia and runtime.cart_summary_text:
        logger.info(
            "backstop resumen determinista: reemplazando texto del LLM por el resumen canónico del carrito"
        )
        final_text = runtime.cart_summary_text

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
        env_pedido = _pedido_de_envase(user_text)
        if env_pedido and runtime.last_term:
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
    if (
        farmacia
        and eleccion_prev
        and runtime.last_options
        and not runtime.cart_forced
        and not runtime.consulted_catalog
    ):
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
            if farmacia and _quiere_ver_resumen(user_text, tiene_carrito=carrito_activo) and not runtime.summary_forced:
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
            if farmacia and not runtime.summary_forced and not runtime.finalize_forced:
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
                    logger.info(
                        "backstop receta: %d medicamentos detectados — consultando todos",
                        len(medicamentos),
                    )
                    grupos: list[tuple[str, list[dict[str, Any]]]] = []
                    no_disponibles: list[str] = []
                    for med in medicamentos:
                        runtime.corregido_desde = None
                        runtime.corregido_a = None
                        result = await runtime.execute("buscar_medicamento", {"nombre": med})
                        prods = (result or {}).get("products") or []
                        # EL TÍTULO LLEVA LA GRAFÍA DEL CATÁLOGO, no la que escribió el
                        # cliente. Si hubo corrección por typo ('hidrocoticida' →
                        # 'hidroclorotiazida'), encabezar con el typo repetiría el error en
                        # la respuesta y el cliente no sabría qué producto se le está
                        # ofreciendo. El encabezado identifica el grupo; debe ser el nombre
                        # con el que el producto existe.
                        titulo = (
                            runtime.corregido_a.upper() if runtime.corregido_a
                            else med.upper()
                        )
                        if prods:
                            grupos.append((titulo, prods))
                        else:
                            logger.info("receta: %s no está en el catálogo", med)
                            no_disponibles.append(titulo)
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
                        receta_final = _formatear_receta(grupos, no_disponibles)
                        await _send(
                            ctx,
                            runtime._conv.id,
                            runtime._crm_conv_id,
                            receta_final,
                        )
                        return None  # turno atendido: no dejar que el LLM reescriba
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
                elif _parece_consulta_medicamento(user_text):
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
                    anterior = runtime.last_ocr_term or prev_last_ocr_term
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
    partes = _partir_mensaje_largo(text, WA_MAX_CHARS)
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
    r"\b(tienen|tenéis|hay|consigo|me dan|me consigues|tienes|busco|buscando|"
    r"buscar|buscas|necesito|quisiera|quiero|quería|queremos|saber|"
    r"venden|vendes|disponible|disponibles|disponen|disponemos|cuesta|cuestan|precio|"
    r"pueden conseguir|traen|consigues|conseguir|tengo|tiene|hay|mande|"
    r"dime|digan|preguntar|pregunto|estaba|estaban|estuve|andaba)\b",
    re.IGNORECASE,
)

# Palabras de relleno (saludos, cortesía, muletillas) que NUNCA son un
# medicamento. Impide que el backstop busque "hola" o "buenas".
_FILLER = {
    "hola", "buenas", "buen", "buenos", "buena", "dia", "dias", "tardes",
    "noches", "gracias", "por", "favor", "quisiera", "podria", "puede",
    "me", "le", "la", "de", "el", "los", "las", "para", "que", "con",
    "una", "un", "en", "y", "o", "a", "si", "no", "como", "cuanto", "es",
    "son", "tiene", "tienen", "hay", "está", "estan", "disponible",
    "disponibles", "precio", "cuesta", "cuestan", "venden", "necesito",
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
        # Opciones: números de 1-2 dígitos en el resto.
        for n in re.findall(r"\b(\d{1,2})\b", resto):
            v = int(n)
            if v >= 1 and (cantidad, v) not in resultado:
                resultado.append((cantidad, v))

    if not resultado:
        return None
    return resultado


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
)
_HANDOFF_VERBOS = (
    "pasa", "pasame", "pásame", "pasen", "pasenme", "pasarme", "paselo", "pasamelo",
    "paseme", "páseme", "pasemelo", "quiero", "quisiera", "necesito", "puedo",
    "deseo", "prefiero", "hablar", "comunicar", "comunicarme", "chatear",
    "atiendanme", "atiendeme", "tratar",
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
    r"\bpasame\s+con\s+(?:alguien|el\s+encargad|el\s+due[ñn]))",
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
    if any(p in t for p in _HANDOFF_PALABRAS_PERSONA) and any(
        v in t for v in _HANDOFF_VERBOS
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

# El "X N" del título del producto: 'BRASARTAN CTDN 80MG/12.5X10 FARMA' → '10'.
_RE_X_ENVASE = re.compile(r"x\s*0*(\d{1,3})\b")


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
    """El tamaño de envase del título de un producto: '... X 30 CAP' → '30'."""
    m = _RE_X_ENVASE.search(str(nombre or "").lower())
    return (m.group(1).lstrip("0") or m.group(1)) if m else ""


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
        if w in _FILLER or w in _MULETILLAS_HABLA:
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
)
# Una línea que empieza por una FORMA farmacéutica es presentación del mismo producto.
_RE_SOLO_FORMA = re.compile(
    r"^\s*\d*\s*(?:óvulos?|ovulos?|supositorios?|cápsulas?|capsulas?|tabletas?|"
    r"comprimidos?|sobres?|ampollas?|frascos?|tubos?|gotas?|crema|gel)\b",
    re.IGNORECASE,
)


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

    if term:
        try:
            productos = await buscar(term)
        except Exception:  # noqa: BLE001
            productos = []
        if productos:
            if not componentes:
                return [term], "caja (el OCR solo trae el nombre)"
            # ¿algún producto del catálogo cubre TAMBIÉN los otros componentes?
            for prod in productos:
                palabras_ok = True
                for comp in componentes:
                    utiles = re.findall(r"[a-záéíóúüñ]{5,}", comp.lower())
                    if not utiles:
                        continue
                    if not any(es_relevante(w, prod) for w in utiles):
                        palabras_ok = False
                        break
                if palabras_ok:
                    return [term], "caja (un producto cubre todos los componentes)"

    # Receta: se devuelven las líneas como medicamentos independientes, con la misma
    # lógica que ya usaba el backstop.
    meds = _parsear_medicamentos_receta(ocr)
    if not meds and _parece_lista_medicamentos(ocr):
        meds = _parsear_medicamentos_receta(
            "\n".join(_lineas_lista_medicamentos(ocr))
        )
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
    r"necesito|busco|quiero|quisiera|dame|me das)\b",
    re.IGNORECASE,
)
# Separadores de ENUMERACIÓN de medicamentos.
_RE_SEP_LISTA = re.compile(r"[,;]|(?:\s+y\s+)|(?:\s+e\s+)|(?:ademas|además)",
                            re.IGNORECASE)


def _sin_motivo(texto: str) -> str:
    """Quita el vocabulario del MOTIVO (precio, cuánto cuesta, tienes...)."""
    return _VERBOS_MOTIVO.sub(" ", texto or "")


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
            limpio = " ".join(_sin_motivo(trozo).split())
            if limpio and re.search(r"[a-záéíóúüñ]{5,}", limpio):
                out.append(limpio)
    return out


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

    # Con separadores de enumeración: contar los medicamentos de cada trozo.
    if _RE_SEP_LISTA.search(texto):
        return len(_medicamentos_enumerados(texto)) >= 2

    # Sin separadores: varias LÍNEAS con medicamento (receta escrita en vertical).
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    if len(lineas) >= 2:
        return len(_parsear_medicamentos_receta(_sin_motivo(texto))) >= 2
    return False


def _partir_consulta_multi(texto: str) -> list[str]:
    """Divide una consulta multi-medicamento en UNA línea sin separadores.

    Patrón: 'de <dosis>' repetido 2+ veces separa medicamentos.
    'quiero saber si disponen de clopidogrel de 75 losartan de 50
    atorvastatina de 30 nifedipina de 10 mg'
    → ['clopidogrel', 'losartan', 'atorvastatina', 'nifedipina']

    Devuelve [] si hay menos de 2 dosis (consulta simple).
    """
    if not texto:
        return []
    t = texto.strip()
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
        if term and term not in vistos:
            vistos.add(term)
            out.append(term)
    return out


def _formatear_receta(
    grupos: list[tuple[str, list[dict[str, Any]]]],
    no_disponibles: list[str] | None = None,
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
    'tienes acido folico de 10 mg' -> 'acido folico 10 mg' (incluye mg/ml)."""
    if not texto:
        return None
    palabras = re.findall(r"[a-záéíóúüñ0-9]+", texto.lower())
    # La unidad HABLADA se normaliza ANTES de decidir ("nifedipina 30 miligramos"
    # → "nifedipina 30 mg"). Sin esto el 30 se descarta por ir seguido de una
    # palabra que no está en la lista de unidades y la dosis se pierde.
    palabras = [_normalizar_unidad(w) for w in palabras]
    # Verbos de consulta y relleno: nunca son parte del medicamento.
    verbos = set(re.findall(r"[a-záéíóúüñ]+", _VERBOS_MEDICAMENTO.pattern))
    excluidas = _FILLER | verbos
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
        if w in excluidas:
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
