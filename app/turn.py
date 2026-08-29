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
from app.llm import LlmExhausted
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
from app.tools import ToolRuntime, active_tool_schemas, _formatear_lista_productos, _fmt_ve

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
        # texto venga vacío (foto de receta sin caption).
        if m.image_base64 or m.media_id:
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
    profile = await resolve_profile(ctx)
    system = build_system_prompt(
        profile=profile,
        context=context,
        conv=conv,
        referral_headline=referral,
        offered=offered,
        tz=_agent_tz(settings),
    )

    # --- Resumen de estado determinista (anti-deriva) ---------------------
    # Inyecta el estado real de la conversación (de la DB, no del LLM) para que
    # el modelo sepa exactamente en qué fase está y no invente contexto entre
    # turnos. Esto es lo que contiene el no-determinismo del flujo encadenado.
    cart = await ctx.store.cart_items(
        conv.id, session_hours=ctx.settings.cart_session_hours
    )
    state_block = _build_state_block(conv, cart)
    if state_block:
        system = system + "\n\n" + state_block
    # Se traen más mensajes de los que ve el LLM: el candado de cierre cuenta
    # el hilo COMPLETO del lead, no solo la ventana de contexto.
    recientes = await ctx.store.recent_messages(conv.id, STALL_LOOKBACK)
    history = recientes[-settings.history_window :]
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}] + [
        {"role": m.role, "content": m.content} for m in history
    ]
    # Hostilidad sostenida (AC-18): el CONTEO es determinista — el LLM salió
    # flaky contando entre turnos. Al tercer strike: alerta en el turno y
    # handoff garantizado más abajo aunque el modelo no llame la herramienta.
    streak = hostile_streak([m.content for m in history if m.role == "user"])
    if streak >= 3:
        messages.append({"role": "system", "content": HOSTILITY_ALERT})
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
    # El providerId del catálogo lo define el CRM por tenant (organization.
    # provider_id) y llega en el contexto — NO es variable de entorno fija.
    provider_id = (context or {}).get("providerId") or settings.provider_id or ""
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
    # Lista de opciones persistida del turno anterior (para resolver
    # "quiero X cajas de la opción Z").
    if isinstance(conv.last_options, list):
        runtime.last_options = conv.last_options
    elif isinstance(conv.last_options, str) and conv.last_options.strip():
        try:
            runtime.last_options = json.loads(conv.last_options)
        except Exception:
            runtime.last_options = []
    # Modo farmacia si hay providerId: se exponen las tools de catálogo y se
    # retiran las de agenda.
    farmacia = bool(provider_id)
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

    # Backstop determinista: al tercer strike el handoff SUCEDE, lo haya
    # llamado el modelo o no (la regla de negocio no depende de su humor).
    if streak >= 3 and runtime.handoff_reason is None:
        runtime.handoff_reason = "hostilidad"

    # Backstop determinista: si este turno se buscó un medicamento que NO está
    # en el catálogo y el modelo no escaló solo, escalamos a humano. Un humano
    # del negocio decide si puede conseguirlo; el agente no debe prometer algo
    # que no puede confirmar ni ofrecer una "consulta" inexistente.
    # EXCEPCIONES: (a) turno de RECETA ya respondida (mostró lo disponible);
    # (b) turno donde el backstop de carrito ya resolvió la elección del cliente
    # ("opción Z" o cantidad) — el LLM pudo llamar buscar_medicamento con un
    # término basura ("cajas opción") que dispara med_not_found falsamente.
    # (c) hay CARRITO ACTIVO: el cliente está en medio de un pedido; el LLM
    # pudo llamar handoff tras una negativa ("no") que en realidad cierra la
    # búsqueda, no el pedido.
    cart_activo = bool(
        await ctx.store.cart_items(conv.id, session_hours=settings.cart_session_hours)
    )
    if (
        runtime.med_not_found
        and runtime.handoff_reason is None
        and not runtime.receta_atendida
        and not runtime.cart_forced
        and not cart_activo
    ):
        runtime.handoff_reason = "medicamento_no_disponible"
        logger.info("medicamento no encontrado en el catálogo — handoff garantizado")

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
            runtime.last_products, runtime.last_term or ""
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
            runtime.last_products, runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

    # Backstop de formato no canónico: el LLM enumeró los productos pero con
    # su propio estilo (markdown, $ con punto, sin 💊 ni Bs). Se reemplaza por
    # la lista canónica: mismo orden (precio asc), formato estándar de la
    # farmacia (💊 N. NOMBRE + $X,XX | Bs Y). El cliente SIEMPRE ve el mismo
    # formato, venga lo que venga del LLM.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and _formato_no_canonico(final_text, runtime.last_products)
    ):
        logger.warning(
            "backstop formato: el LLM enumeró %d productos sin el formato canónico — reemplazo con lista estándar",
            len(runtime.last_products),
        )
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

    # Backstop de omisión: si el catálogo devolvió productos pero el texto final
    # NO menciona el medicamento (ni el término ni ningún nombre de producto),
    # el cliente recibiría un "¿Cuál prefieres?" sin contexto. Reemplazamos con
    # la lista real para que la respuesta sea autocontenida.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and not _menciona_producto(final_text, runtime.last_products, runtime.last_term)
    ):
        logger.warning(
            "backstop omisión: el LLM no mencionó el producto pese a %d resultados — reemplazo con lista real",
            len(runtime.last_products),
        )
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_term or ""
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
            runtime.last_products, runtime.last_term or ""
        ) + "\n\n" + MENSAJE_SUGERIDO_CARRITO

    # Backstop de handoff injustificado: si el catálogo SÍ devolvió productos
    # pero el LLM despide al cliente o lo pasa a humano (sin que el medicamento
    # esté agotado), reemplazamos con la lista real. El cliente jamás debe ser
    # despedido cuando hay productos disponibles.
    if (
        farmacia
        and runtime.last_products
        and final_text
        and not runtime.med_not_found
        and _es_despedida_o_handoff(final_text)
    ):
        logger.warning(
            "backstop handoff injustificado: el LLM despidió pese a %d productos — reemplazo con lista real",
            len(runtime.last_products),
        )
        final_text = _formatear_lista_productos(
            runtime.last_products, runtime.last_term or ""
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
        final_text = final_text.rstrip()
        if not final_text.endswith(MENSAJE_SUGERIDO_CARRITO):
            logger.info(
                "backstop bloque carrito: adjuntar MENSAJE_SUGERIDO_CARRITO a la lista de %d productos",
                len(runtime.last_products),
            )
            final_text += "\n\n" + MENSAJE_SUGERIDO_CARRITO

    sent = False
    if final_text and final_text.strip():
        # Sanitiza el markup interno de handoff (si el modelo lo escribió como
        # texto en vez de llamar la tool): jamás debe llegar al cliente.
        clean = _strip_internal_markup(final_text.strip())
        clean = _quitar_ofrecimiento_consulta(clean)
        if clean:
            sent = await _send(ctx, conv.id, str(crm_conv_id), clean)
            if sent:
                await ctx.store.add_message(conv.id, "assistant", clean)

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
        if sent and not conv.followup_sent:
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


async def _tool_loop(
    ctx: AppContext,
    messages: list[dict[str, Any]],
    runtime: ToolRuntime,
    *,
    farmacia: bool = False,
    user_text: str = "",
) -> str | None:
    """Rondas de tool-calling hasta obtener texto final (o rendirse)."""
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
                elecciones = _extraer_eleccion_multiple(user_text)
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
            # Backstop de resumen: si el cliente quiere ver el resumen y el LLM
            # no llamó ver_carrito, lo forzamos (el modelo a veces no lo llama).
            # El "no" suelto (a "¿Deseas buscar otro medicamento?") con carrito
            # activo también cuenta: quiere el resumen, no handoff.
            carrito_activo = bool(await ctx.store.cart_items(
                runtime._conv.id, session_hours=ctx.settings.cart_session_hours
            ))
            if farmacia and _quiere_ver_resumen(user_text, tiene_carrito=carrito_activo) and not runtime.summary_forced:
                runtime.summary_forced = True
                logger.info("backstop resumen: forzando ver_carrito")
                result = await runtime.execute("ver_carrito", {})
                _append_forced_tool(messages, "ver_carrito", {}, result)
                continue
            # Backstop de finalizar: si el cliente confirma el pedido y el LLM
            # no llamó finalizar_pedido, lo forzamos.
            if farmacia and _quiere_finalizar(user_text) and not runtime.finalize_forced:
                runtime.finalize_forced = True
                logger.info("backstop finalizar: forzando finalizar_pedido")
                result = await runtime.execute("finalizar_pedido", {})
                _append_forced_tool(messages, "finalizar_pedido", {}, result)
                continue
            # Backstop de refinamiento: si el cliente responde con un miligramo /
            # presentación (p.ej. "30 mg") y ya consultamos un medicamento antes,
            # forzamos re-consultar el catálogo con ese refinamiento para que el
            # LLM cite los productos reales (no los invente de memoria).
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
                # Lista de medicamentos en TEXTO (sin imagen): si el mensaje del
                # cliente contiene 2+ medicamentos (p. ej. "esoz, leprit y
                # evigax"), se responde con el mismo formato de receta.
                if not medicamentos and _parece_lista_medicamentos(user_text):
                    medicamentos = _parsear_medicamentos_receta(
                        "\n".join(_lineas_lista_medicamentos(user_text))
                    )
                # Consulta multi-medicamento en UNA línea sin separadores:
                # "disponen de clopidogrel de 75 losartan de 50 atorvastatina
                # de 30 nifedipina de 10 mg" — el patrón 'de <dosis>' repetido
                # separa los medicamentos.
                if not medicamentos:
                    medicamentos = _partir_consulta_multi(user_text)
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
                        result = await runtime.execute("buscar_medicamento", {"nombre": med})
                        prods = (result or {}).get("products") or []
                        if prods:
                            grupos.append((med.upper(), prods))
                        else:
                            logger.info("receta: %s no está en el catálogo", med)
                            no_disponibles.append(med)
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
                if _parece_consulta_medicamento(user_text):
                    term = _extraer_termino_medicamento(user_text)
                # Forzar búsqueda si:
                # 1. El LLM no consultó el catálogo (not consulted_catalog)
                # 2. O consultó pero no encontró nada (med_not_found) — quizás
                #    usó un término distinto al deterministicamente correcto.
                #    Re-consultar con el término extraído puede encontrar productos.
                if term and (not runtime.consulted_catalog or (runtime.med_not_found and not runtime.catalog_retried)):
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
}

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
    """Detecta la selección de opciones por número, con o sin cantidad.

    'quiero 1 caja de 1,4,7 y 8' → [(1,1), (1,4), (1,7), (1,8)]  (cantidad 1)
    'quiero 2 cajas de la opción 3' → [(2,3)]
    'la opción 1' → [(1,1)]
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
    # unidad de caja ("1 caja de 1,4,7") o separada por comas/y, o "quiero"
    # seguido de lista de números ("quiero 1,6,12,20 y 31").
    es_eleccion = ("opci" in t) or (
        re.search(r"\b(caja|cajas|unidad|unidades)\b", t)
        and re.search(r"\d{1,2}\s*[,y]\s*\d{1,2}", t)
    ) or (
        re.search(r"\b(quiero|quisiera|necesito|dame|me das)\b", t)
        and re.search(r"\d{1,2}\s*[,y]\s*\d{1,2}", t)
    )
    if not es_eleccion:
        return None
    # Cantidad: número + unidad de caja (default 1).
    cantidad = 1
    m_cant = re.search(r"(\d+)\s*(?:caja|cajas|unidad|unidades)", t)
    if m_cant:
        cantidad = max(1, int(m_cant.group(1)))
    # Quitar la cantidad del texto para no contarla como opción.
    resto = t
    if m_cant:
        resto = t.replace(m_cant.group(0), " ", 1)
    # Opciones: números de 1-2 dígitos en el resto.
    opciones: list[int] = []
    for n in re.findall(r"\b(\d{1,2})\b", resto):
        v = int(n)
        if v >= 1 and v not in opciones:
            opciones.append(v)
    if not opciones:
        return None
    return [(cantidad, o) for o in opciones]


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
    es una cantidad, no la elección de una opción."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            texto = str(msg["content"]).lower()
            return bool(
                re.search(r"cu[aá]ntas?\s+(?:cajas?|unidades?|blister|ampollas?)", texto)
                or re.search(r"qu[eé] cantidad", texto)
            )
        if msg.get("role") == "user":
            break
    return False


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


def _extraer_refinamiento(texto: str) -> str:
    """Extrae SOLO el refinamiento de presentación del texto: el número+unidad
    de dosis o la forma ('10 mg', '30 tabletas', 'gotas', 'ampolla').

    'tienes acido folico de 10 mg' → '10 mg'
    '30 mg' → '30 mg'
    'quiero gotas' → 'gotas'

    Devuelve '' si no hay un refinamiento claro. NUNCA incluye verbos de
    consulta ni el nombre del medicamento (evita duplicar el término)."""
    if not texto:
        return ""
    t = texto.strip().lower()
    # Número + unidad de dosis/presentación.
    m = re.search(r"\b(\d+(?:[,.]\d+)?)\s*(mg|ml|g|mcg|gotas|tabletas|tab|comprimidos|cápsulas|capsulas)\b", t)
    if m:
        return f"{m.group(1)} {m.group(2)}".strip()
    # Presentación sin número (solo si está al final o es el foco del mensaje).
    m = re.search(r"\b(gotas|jarabe|jbe|tabletas|comprimidos|inyectable|ampolla|crema|ungüento)\b", t)
    if m:
        return m.group(1)
    return ""


def _es_refinamiento_presentacion(texto: str) -> bool:
    """True si el texto es un refinamiento de presentación ('30 mg', '50 mg',
    'gotas', 'jarabe') más que una nueva búsqueda de medicamento."""
    if not texto:
        return False
    t = texto.strip().lower()
    # Número + unidad de dosis/presentación.
    if re.search(r"\b\d+\s*(mg|ml|g|gotas|tabletas|comprimidos|cápsulas|capsulas)\b", t):
        return True
    # Presentación sin número.
    if re.search(r"\b(gotas|jarabe|jbe|tabletas|comprimidos|inyectable|ampolla|crema|ungüento)\b", t):
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
    # Sin verbo de consulta: no es una búsqueda (p. ej. "2 cajas" respondiendo
    # la pregunta del agente, o un saludo suelto).
    return False


def _build_state_block(conv: Conversation, cart: list[CartItem]) -> str:
    """Resumen de estado determinista inyectado en el system prompt.

    Le dice al LLM exactamente en qué fase está la conversación y qué datos
    reales hay (producto consultado, carrito), para que no invente contexto
    entre turnos. Esto contiene el no-determinismo del flujo encadenado."""
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

    # Carrito
    if cart:
        items_str = "; ".join(f"{it.producto} x{it.cantidad}" for it in cart)
        total = sum((it.precio_usd or 0) * it.cantidad for it in cart)
        lines.append(f"- Carrito actual: {items_str}. Total parcial: ${total:.2f}.")

    # Si no hay estado relevante (sin producto, término ni carrito), no inyectar
    if not conv.last_product and not conv.last_term and not cart:
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
    """
    if not texto or len(products) < 2:
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
    # _extraer_termino_medicamento limpia verbos de consulta y relleno
    # ("quería saber atamel forte" → "atamel forte").
    term = _extraer_termino_medicamento(texto)
    if not term:
        return None
    # Si lo que queda son SOLO palabras de relleno ("nada", "gracias"),
    # no es una consulta de medicamento.
    palabras = set(re.findall(r"[a-záéíóúüñ0-9]+", term.lower()))
    if palabras and palabras <= _FILLER:
        return None
    return term


def _extraer_termino_ocr(user_text: str) -> str | None:
    """Extrae el término de medicamento del marcador OCR de una imagen.

    'OCR de la imagen: "ACIDO FOLICO 5 MG X 10 TABLETAS DROTOFARMA"'
    → 'acido folico 5 mg 10 tabletas drotofarma'.
    """
    m = re.search(r'OCR de la imagen:\s*"([^"]+)"', user_text, re.IGNORECASE)
    if not m:
        return None
    return _extraer_termino_medicamento(m.group(1))


def _parece_lista_medicamentos(texto: str) -> bool:
    """True si el texto parece una lista de 2+ medicamentos (receta en texto,
    no una consulta simple). Detecta separadores de lista: comas, 'y',
    'ademas', saltos de línea con nombres propios.

    'esoz, leprit y evigax' / 'ESOZ\nLEPRIT\nEVIGAX' → True
    'tienes atamel forte?' → False (consulta simple)
    """
    if not texto:
        return False
    t = texto.strip().lower()
    # Una LISTA de medicamentos es una enumeración de nombres, SIN verbo de
    # consulta. Si el texto tiene "tiene/tienes/hay/busco..." es una consulta
    # simple (aunque lleve comas y "y": "depomedrol 125mg o 500 MG, marca y
    # precio" NO es una receta).
    if _VERBOS_MEDICAMENTO.search(t):
        return False
    # Separadores de lista explícitos (comas, 'y', 'ademas')
    if re.search(r"[,;]|(?:\s+y\s+)|(?:ademas)", t):
        # Separar por comas primero (una línea puede tener varios medicamentos)
        trozos = re.split(r"[,;]+|\s+y\s+", texto, flags=re.IGNORECASE)
        terminos = _parsear_medicamentos_receta("\n".join(p.strip() for p in trozos if p.strip()))
        return len(terminos) >= 2
    # Múltiples líneas con contenido (sin ser OCR)
    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    if len(lineas) >= 2:
        terminos = _parsear_medicamentos_receta(texto)
        return len(terminos) >= 2
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
    return " ".join(terminos)
