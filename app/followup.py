"""Seguimiento: UN empujón suave si el lead se quedó callado.

Loop asyncio cada 60 s. Un empujón a las `FOLLOWUP_HOURS` (default 4) si la
fase lo amerita (hubo conversación, sin resultado, sin handoff). `followup_sent`
se marca ANTES de enviar: a lo sumo uno, incluso con crash a media operación.
Ventana cerrada o IA pausada → se omite con log (v1 no maneja plantillas).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from app.crm import CrmConflict, CrmError
from app.llm import LlmExhausted
from app.profile import resolve_profile
from app.prompt import FOLLOWUP_INSTRUCTION, build_system_prompt
from app.state import AppContext, Conversation, utcnow
from app.turn import conversation_lock
from app.turn import _agent_tz

logger = logging.getLogger("nea.followup")


def _dia_permitido(dia: int | None, config: str) -> bool:
    """¿Se permite el seguimiento en este día de la semana? 0=lunes … 6=domingo.

    `config` es un CSV ("0,1,2,3,4,5") o vacío/None = todos los días.
    """
    if dia is None:
        return False
    texto = (config or "").strip()
    if not texto:
        return True
    try:
        permitidos = {int(x) for x in texto.split(",") if x.strip() != ""}
    except ValueError:
        # Config mal escrita: no bloquees el seguimiento por eso.
        return True
    return dia in permitidos


class FollowupWorker:
    INTERVAL = 60.0

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            try:
                await self.tick()
            except Exception:
                logger.exception("followup: fallo en el barrido")

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        s = self._ctx.settings
        if not s.followup_enabled:
            return
        # NUNCA fuera del horario del negocio. Medido en producción, 12 de 40 empujones
        # salieron fuera de las 8-20 h (uno a las 4 de la mañana). Un mensaje que
        # despierta al cliente es peor que el silencio.
        local = now.astimezone(_agent_tz(s))
        if not (s.followup_hour_start <= local.hour < s.followup_hour_end):
            logger.debug(
                "followup: %02d h locales — fuera de la franja %d-%d, se omite",
                local.hour, s.followup_hour_start, s.followup_hour_end,
            )
            return
        # Días permitidos (0=lunes … 6=domingo). El domingo la farmacia está cerrada:
        # un seguimiento que no se puede atender solo genera frustración.
        if not _dia_permitido(local.weekday(), s.followup_dias):
            logger.debug("followup: %s — día no permitido, se omite",
                         local.strftime("%A"))
            return
        for conv in await self._ctx.store.due_followups(now):
            # Claim atómico ANTES de enviar: jamás un segundo empujón.
            if not await self._ctx.store.claim_followup(conv.id):
                continue
            try:
                # Mismo candado que los turnos: sin él, el empujón puede salir
                # encimado con la respuesta de un turno vivo (dos mensajes del
                # agente a la vez). El valor se copia ANTES del candado porque
                # el Store puede devolver el mismo objeto vivo (MemoryStore) y
                # compararlo contra sí mismo no detectaría nada.
                ultimo_inbound = conv.last_inbound_at
                async with conversation_lock(self._ctx, conv.wa_identity):
                    await self._push(conv, ultimo_inbound)
            except Exception:
                logger.exception(
                    "followup de %s falló — queda consumido (a lo sumo uno)",
                    conv.wa_identity,
                )

    async def _push(
        self, conv: Conversation, ultimo_inbound: datetime | None
    ) -> None:
        ctx = self._ctx
        # Si mientras esperábamos el candado el lead escribió, el empujón sobra:
        # ya hay conversación viva y "¿seguimos?" quedaría fuera de lugar.
        fresca = await ctx.store.get_or_create_conversation(conv.wa_identity)
        if fresca.last_inbound_at != ultimo_inbound:
            logger.info(
                "followup %s: el lead escribió mientras tanto — omitido",
                conv.wa_identity,
            )
            return
        try:
            context = await ctx.crm.get_context(conv.wa_identity)
        except CrmError as exc:
            logger.warning("followup %s: CRM inaccesible (%s) — omitido", conv.wa_identity, exc)
            return
        if context is None:
            logger.warning("followup %s: sin contexto — omitido", conv.wa_identity)
            return
        info = context.get("conversation") or {}
        crm_conv_id = info.get("id") or conv.crm_conversation_id
        if not crm_conv_id:
            logger.warning("followup %s: sin conversationId — omitido", conv.wa_identity)
            return
        if not info.get("aiEnabled", False):
            logger.info("followup %s: IA pausada — omitido", conv.wa_identity)
            return
        if not info.get("windowOpen", False):
            logger.info(
                "followup %s: ventana de 24 h cerrada — omitido con registro",
                conv.wa_identity,
            )
            return

        # LA CONDICIÓN DE NEGOCIO: el empujón es para el PEDIDO que no se cerró, no un
        # "¿sigues ahí?" genérico. Se omite si:
        #   · el cliente YA cerró el pedido (`cart_closed`) → ya compró, insistirle molesta
        #   · el carrito está VACÍO → no hay pedido a retomar
        # Antes esto no existía: el seguimiento se agendaba con CUALQUIER turno enviado
        # (una consulta de precio, un "gracias", una reserva de demo), así que llegaba
        # a quien nunca mostró intención de comprar. Medido: 40 empujones, y el que
        # reportó el negocio era sobre una RESERVA DE DEMO, no un pedido.
        #
        # `cart_closed` es un flag LOCAL de bot_conversation (no viene del CRM): se
        # activa solo en finalizar_pedido. Es la marca fiable de "esto fue una venta".
        if getattr(conv, "cart_closed", False):
            logger.info(
                "followup %s: el pedido YA se cerró (venta hecha) — omitido",
                conv.wa_identity,
            )
            return
        # Antigüedad del pedido: se usa `followup_max_age_hours` (48 h), NO la ventana
        # del carrito operativo (2 h en producción). Con la ventana operativa el pedido
        # ya había expirado justo cuando toca el empujón de las 4 h → 0 seguimientos.
        carrito = await ctx.store.cart_items(
            conv.id, session_hours=ctx.settings.followup_max_age_hours
        )
        if not carrito:
            logger.info(
                "followup %s: sin pedido armado — no es seguimiento de pedido, omitido",
                conv.wa_identity,
            )
            return

        system = build_system_prompt(
            profile=await resolve_profile(ctx, str(crm_conv_id)),
            context=context,
            conv=conv,
            offered=[],
            tz=_agent_tz(ctx.settings),
        )
        history = await ctx.store.recent_messages(
            conv.id, ctx.settings.history_window
        )
        messages: list[dict[str, Any]] = (
            [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in history]
            + [{"role": "system", "content": FOLLOWUP_INSTRUCTION}]
        )
        try:
            reply = await ctx.llm.complete(messages, tools=None)
        except LlmExhausted as exc:
            logger.warning("followup %s: LLM agotado (%s) — omitido", conv.wa_identity, exc)
            return
        text = (reply.content or "").strip()
        if not text:
            logger.warning("followup %s: LLM sin texto — omitido", conv.wa_identity)
            return
        try:
            await ctx.crm.send_message(str(crm_conv_id), text)
        except CrmConflict as exc:
            logger.info("followup %s: bloqueado por el CRM (%s)", conv.wa_identity, exc.code)
            return
        except CrmError as exc:
            logger.warning("followup %s: envío falló (%s)", conv.wa_identity, exc)
            return
        await ctx.store.add_message(conv.id, "assistant", text)
        logger.info("followup %s: empujón único enviado", conv.wa_identity)
