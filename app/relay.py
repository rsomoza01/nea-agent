"""Relay del webhook al CRM: cola persistente en Postgres + backoff exponencial.

Se reenvía el BODY CRUDO (bytes exactos) con el header `x-hub-signature-256`
original, para que la ingesta idempotente del CRM verifique la firma de Meta
tal cual. Un hipo del CRM no pierde el mensaje: reintentos hasta entregar o
agotar 24 h desde el encolado.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from app.state import RelayItem, Store, utcnow

logger = logging.getLogger("nea.relay")


class RelayWorker:
    MAX_AGE = timedelta(hours=24)  # tope duro de reintentos
    BACKOFF_CAP = 900.0  # 15 min entre intentos, máximo
    IDLE_SCAN = 5.0  # barrido periódico aunque nadie despierte al worker

    def __init__(
        self,
        store: Store,
        webhook_url: str,
        wake: asyncio.Event,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._url = webhook_url
        self._wake = wake
        self._http = http or httpx.AsyncClient(timeout=20.0)

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.IDLE_SCAN)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.process_due()
            except Exception:
                logger.exception("relay: fallo procesando la cola")

    async def process_due(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        for item in await self._store.due_relays(now):
            if now - item.created_at > self.MAX_AGE:
                logger.error(
                    "relay: item %d agotó las 24 h sin entregar — abandonado", item.id
                )
                await self._store.mark_relay_abandoned(item.id)
                continue
            if await self._deliver(item):
                await self._store.mark_relay_delivered(item.id)
            else:
                attempts = item.attempts + 1
                delay = min(2.0**attempts, self.BACKOFF_CAP)
                logger.warning(
                    "relay: item %d falló (intento %d), reintento en %.0f s",
                    item.id,
                    attempts,
                    delay,
                )
                await self._store.reschedule_relay(
                    item.id, attempts, now + timedelta(seconds=delay)
                )

    async def _deliver(self, item: RelayItem) -> bool:
        headers = {"content-type": "application/json"}
        if item.signature:
            headers["x-hub-signature-256"] = item.signature
        try:
            resp = await self._http.post(self._url, content=item.body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("relay: error de red hacia el CRM: %s", exc)
            return False
        return 200 <= resp.status_code < 300

    async def aclose(self) -> None:
        await self._http.aclose()
