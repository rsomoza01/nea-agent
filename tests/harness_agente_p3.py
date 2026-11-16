"""Arnés de agente para el Paquete 3 (G13 — afirmación de acción sin acción).

Contrato del CRM copiado de la sonda (`eval/qa-2026-11-06/sonda_bucle.py`).
Nombre propio del paquete (`_p3`). LLM guionado determinista (coste 0).
"""
from __future__ import annotations

from typing import Any

from app import turn as turn_mod
from app.config import Settings
from app.state import AppContext, InboundMessage, MemoryStore


class _TC:
    def __init__(self, tc_id: str, name: str, arguments: dict) -> None:
        self.id = tc_id
        self.name = name
        self.arguments = arguments


class _R:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


def tc(n: int, nombre: str, args: dict) -> _R:
    return _R(None, [_TC(f"call-{n}", nombre, args)])


def txt(contenido: str) -> _R:
    return _R(contenido, None)


class CrmStub:
    def __init__(self) -> None:
        self.enviados: list[str] = []
        self.handoffs: list[str] = []

    async def get_context(self, wa_identity: str, conversation_id: str | None = None) -> dict:
        return {
            "conversation": {"id": f"conv-{wa_identity}", "aiEnabled": True, "windowOpen": True},
            "providerId": "demo",
            "contact": {"name": "QA Probe", "phone": wa_identity},
        }

    async def get_products(self, provider_id: str, q: str | None = None, limit: int = 20) -> dict:
        return {"products": [], "provider": {"id": provider_id, "name": "Farmacia QA"},
                "paymenType": "Efectivo", "hours": "8:00-18:00"}

    async def send_message(self, *a: Any, **k: Any) -> dict:
        self.enviados.append(str(a))
        return {"ok": True}

    async def post_typing(self, *a: Any, **k: Any) -> None:
        return None

    async def post_paused(self, *a: Any, **k: Any) -> None:
        return None

    async def post_reset(self, *a: Any, **k: Any) -> None:
        return None

    async def post_handoff(self, *a: Any, **k: Any) -> None:
        self.handoffs.append(str(a))
        return None

    async def put_ficha(self, *a: Any, **k: Any) -> None:
        return None

    async def get_media(self, *a: Any, **k: Any) -> Any:
        return None

    async def get_providers(self, *a: Any, **k: Any) -> list:
        return []

    async def get_availability(self, *a: Any, **k: Any) -> list:
        return []

    async def create_booking(self, *a: Any, **k: Any) -> dict:
        return {}

    async def reschedule_booking(self, *a: Any, **k: Any) -> dict:
        return {}


class FakeLlm:
    def __init__(self, guion: list[_R]) -> None:
        self.guion = guion
        self.i = 0

    async def complete(self, messages, tools=None):
        idx = min(self.i, len(self.guion) - 1)
        r = self.guion[idx]
        self.i += 1
        return r


async def correr(nombre: str, guion: list[_R], mensaje: str) -> str:
    """Corre un turno real y devuelve el texto que el cliente habría recibido (o '')."""
    ctx = AppContext(
        settings=Settings(intent_layer=False),
        store=MemoryStore(),
        crm=CrmStub(),
        llm=FakeLlm(guion),
        profile=None,
    )
    identidad = f"p3-{nombre.lower().replace(' ', '-')}"
    previo, ctx.lab_outbox = ctx.lab_outbox, []
    try:
        await turn_mod.handle_flush(
            ctx, identidad,
            [InboundMessage(wa_message_id=f"wamid-{nombre}", identity=identidad,
                            type="text", text=mensaje)])
    except Exception as exc:  # noqa: BLE001
        ctx.lab_outbox.append(f"<EXCEPCION {type(exc).__name__}: {exc}>")
    finally:
        resp = list(ctx.lab_outbox or [])
        ctx.lab_outbox = previo
    return resp[0] if resp else ""
