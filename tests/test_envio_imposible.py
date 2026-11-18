"""Envío imposible: la instancia de la farmacia no puede mandar.

Cuando el CRM responde 409 con `reconnect_required` o `not_connected`, el token
de la instancia de Evolution ya no vale (instancia recreada, número
desconectado) o el número no está conectado. NO es transitorio.

Antes ese caso llegaba como 502 y `_send` lo trataba como un fallo de red:

    intento 1..4 → 502 → encolado en pending_send
    sender worker → reintento cada 30 s, 60 s, 120 s… hasta 24 h

Mensajes girando 24 h por una causa que no se arregla reintentando, y el cliente
esperando en silencio. Ahora: sin reintentos, sin cola, y handoff a un humano
para que alguien lo vea en el CRM.
"""

import asyncio

from app.crm import CrmConflict, CrmError
from app.turn import SEND_FATAL_CODES, _send


class CrmFake:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.enviados: list[str] = []
        self.handoffs: list[tuple[str, str]] = []

    async def send_message(self, conversation_id: str, text: str) -> dict:
        raise self._exc

    async def post_handoff(self, conversation_id: str, reason: str) -> None:
        self.handoffs.append((conversation_id, reason))


class CtxFake:
    def __init__(self, exc: Exception) -> None:
        self.crm = CrmFake(exc)
        self.lab_outbox = None


def test_instancia_sin_token_no_reintenta_y_deriva_a_humano():
    for codigo in ("reconnect_required", "not_connected"):
        ctx = CtxFake(CrmConflict(codigo))
        ok = asyncio.run(_send(ctx, 1, "cv_x", "Tengo estas opciones 👇"))
        assert ok is False
        assert ctx.crm.handoffs == [("cv_x", "error")], codigo


def test_ai_paused_sigue_siendo_silencio_respetuoso():
    """Un humano tomó la conversación: no se deriva ni se reintenta."""
    ctx = CtxFake(CrmConflict("ai_paused"))
    assert asyncio.run(_send(ctx, 1, "cv_x", "hola")) is False
    assert ctx.crm.handoffs == []


def test_window_closed_tampoco_deriva():
    ctx = CtxFake(CrmConflict("window_closed"))
    assert asyncio.run(_send(ctx, 1, "cv_x", "hola")) is False
    assert ctx.crm.handoffs == []


def test_la_lista_de_codigos_fatales_es_la_esperada():
    assert SEND_FATAL_CODES == frozenset({"reconnect_required", "not_connected"})


def test_error_transitorio_sigue_siendo_transitorio():
    """Un 502 de verdad (CRM caído, Evolution lento) NO debe derivar a humano:
    para eso está la cola. Aquí solo se comprueba que no entra en los fatales."""
    assert "channel_unavailable" not in SEND_FATAL_CODES
    assert "meta_error" not in SEND_FATAL_CODES
    _ = CrmError("messages devolvió 502")  # sigue siendo la vía de reintento
