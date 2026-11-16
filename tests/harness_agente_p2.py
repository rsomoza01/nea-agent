"""Arnés de agente para el Paquete 2 (G5 — composición y equivalencia genérica).

Contrato del CRM copiado de `eval/qa-2026-11-06/sonda_bucle.py`, con una vuelta
de tuerca exigida por la orden: el catálogo puede servir productos **con** y
**sin** campo de composición, para medir "con evidencia" vs "sin evidencia".
Nombre propio del paquete (`_p2`). LLM guionado determinista (coste 0).
"""
from __future__ import annotations

from typing import Any

from app import turn as turn_mod
from app.config import Settings
from app.state import AppContext, InboundMessage, MemoryStore


# Catálogo por defecto: ningún producto expone composición (como el real).
CATALOGO_SIN_COMPOSICION: dict[str, list[dict[str, Any]]] = {
    "losartan": [
        {"producto": "LOSARTAN 50 mg x 30 tabletas", "nombre": "LOSARTAN",
         "productId": "p-los-1", "precio": 3.5, "precioBs": 120.0, "presentacion": "30 tabletas"},
        {"producto": "LOSARTAN 100 mg x 30 tabletas", "nombre": "LOSARTAN",
         "productId": "p-los-2", "precio": 5.0, "precioBs": 170.0, "presentacion": "30 tabletas"},
    ],
}

# Catálogo "del futuro": un producto que SÍ expone el principio activo.
CATALOGO_CON_COMPOSICION: dict[str, list[dict[str, Any]]] = {
    "daflon": [
        {"producto": "DAFLON 500 mg x 30 comprimidos", "nombre": "DAFLON",
         "productId": "p-daf-1", "precio": 12.0, "precioBs": 410.0, "presentacion": "30 comprimidos",
         "composicion": "DIOSMINA 450 mg + HESPERIDINA 50 mg"},
    ],
}


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
    def __init__(self, catalogo: dict | None = None) -> None:
        self.catalogo = catalogo if catalogo is not None else CATALOGO_SIN_COMPOSICION
        self.enviados: list[str] = []
        self.handoffs: list[str] = []

    async def get_context(self, wa_identity: str, conversation_id: str | None = None) -> dict:
        return {
            "conversation": {"id": f"conv-{wa_identity}", "aiEnabled": True, "windowOpen": True},
            "providerId": "demo",
            "contact": {"name": "QA Probe", "phone": wa_identity},
        }

    async def get_products(self, provider_id: str, q: str | None = None, limit: int = 20) -> dict:
        q_norm = (q or "").lower()
        prods: list[dict[str, Any]] = []
        for clave, lista in self.catalogo.items():
            if clave in q_norm:
                prods = lista
                break
        return {"products": prods[:limit], "provider": {"id": provider_id, "name": "Farmacia QA"},
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


async def correr(nombre: str, guion: list[_R], mensaje: str,
                 catalogo: dict | None = None) -> str:
    """Corre un turno real y devuelve el texto que el cliente habría recibido (o '')."""
    ctx = AppContext(
        settings=Settings(intent_layer=False),
        store=MemoryStore(),
        crm=CrmStub(catalogo=catalogo),
        llm=FakeLlm(guion),
        profile=None,
    )
    identidad = f"p2-{nombre.lower().replace(' ', '-')}"
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
