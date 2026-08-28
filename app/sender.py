"""Reintento diferido de envíos: lo que el turno no pudo entregar, sale después.

Loop asyncio cada 30 s sobre `pending_send` (encolado por app/turn.py cuando el
envío agota sus reintentos). Backoff exponencial con tope de 15 min; se
abandona si el CRM lo rechaza con 409 (ai_paused / window_closed) o al agotar
24 h — en ese caso se registra handoff `error` para que un humano vea la
conversación en el CRM (incidente 2026-08-03: antes la respuesta se descartaba
en silencio y el lead no recibía nada).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.crm import CrmConflict, CrmError
from app.state import AppContext, PendingSend, utcnow

logger = logging.getLogger("nea.sender")


class SenderWorker:
    INTERVAL = 30.0
    MAX_AGE = timedelta(hours=24)  # la ventana de WhatsApp ya habrá cerrado
    BACKOFF_BASE = 30.0
    BACKOFF_CAP = 900.0  # 15 min entre intentos, máximo

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            try:
                await self.tick()
            except Exception:
                logger.exception("sender: fallo en el barrido")

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        for item in await self._ctx.store.due_pending_sends(now):
            if now - item.created_at > self.MAX_AGE:
                logger.error(
                    "sender: pending %d agotó las 24 h sin entregar — abandonado + handoff",
                    item.id,
                )
                await self._ctx.store.mark_pending_send_abandoned(item.id)
                await self._handoff(item.crm_conversation_id)
                continue
            await self._attempt(item, now)

    async def _attempt(self, item: PendingSend, now: datetime) -> None:
        ctx = self._ctx
        # WhatsApp límite 4096: un pending largo (receta de 8 meds) se divide
        # en partes ≤4000 — el mismo trato que el turno en vivo (turn._send).
        from app.turn import WA_MAX_CHARS, _partir_mensaje_largo

        partes = _partir_mensaje_largo(item.content, WA_MAX_CHARS)
        for i, parte in enumerate(partes):
            try:
                await ctx.crm.send_message(item.crm_conversation_id, parte)
            except CrmConflict as exc:
                # ai_paused / window_closed: ya no procede — silencio respetuoso.
                logger.info(
                    "sender: pending %d rechazado por el CRM (%s) — abandonado",
                    item.id,
                    exc.code,
                )
                await ctx.store.mark_pending_send_abandoned(item.id)
                return
            except CrmError as exc:
                attempts = item.attempts + 1
                delay = min(self.BACKOFF_BASE * (2.0**item.attempts), self.BACKOFF_CAP)
                logger.warning(
                    "sender: pending %d falló (parte %d/%d, intento %d): %s — reintento en %.0f s",
                    item.id,
                    i + 1,
                    len(partes),
                    attempts,
                    exc,
                    delay,
                )
                await ctx.store.reschedule_pending_send(
                    item.id, attempts, now + timedelta(seconds=delay)
                )
                return
        await ctx.store.mark_pending_send_delivered(item.id)
        # Al historial local: Nea debe recordar lo que (por fin) dijo.
        await ctx.store.add_message(item.conversation_id, "assistant", item.content)
        logger.info("sender: pending %d entregado (%d parte(s))", item.id, len(partes))

    async def _handoff(self, crm_conv_id: str) -> None:
        try:
            await self._ctx.crm.post_handoff(crm_conv_id, "error")
        except CrmError as exc:
            logger.error("sender: no pude registrar el handoff tras abandono: %s", exc)
