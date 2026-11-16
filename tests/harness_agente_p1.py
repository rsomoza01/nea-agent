"""Arnés de agente para el Paquete 1 (bucle de herramientas).

Copiado y adaptado del contrato de `eval/qa-2026-11-06/sonda_bucle.py`: LLM
guionado determinista (coste 0) + CRM/catálogo de mentira. Nombre propio del
paquete (`_p1`) para que las tres ramas se mergeen sin colisiones de arnés.

Lo que la sonda captura en `ctx.lab_outbox` es lo que el cliente habría recibido.
"""
from __future__ import annotations

import logging
from typing import Any

from app import turn as turn_mod
from app.config import Settings
from app.crm import CrmError
from app.state import AppContext, InboundMessage, MemoryStore


# --------------------------------------------------------------------------
# Catálogo de mentira (clave = substring del término; lista = productos reales)
# --------------------------------------------------------------------------
CATALOGO: dict[str, list[dict[str, Any]]] = {
    "losartan": [
        {"producto": "LOSARTAN 50 mg x 30 tabletas", "nombre": "LOSARTAN",
         "productId": "p-los-1", "precio": 3.5, "precioBs": 120.0, "presentacion": "30 tabletas"},
        {"producto": "LOSARTAN 100 mg x 30 tabletas", "nombre": "LOSARTAN",
         "productId": "p-los-2", "precio": 5.0, "precioBs": 170.0, "presentacion": "30 tabletas"},
    ],
    "metformina": [
        {"producto": "METFORMINA 850 mg x 30 tabletas", "nombre": "METFORMINA",
         "productId": "p-met-1", "precio": 2.1, "precioBs": 70.0, "presentacion": "30 tabletas"},
    ],
    "ibuprofeno": [
        {"producto": "IBUPROFENO 400 mg x 20 tabletas", "nombre": "IBUPROFENO",
         "productId": "p-ibu-1", "precio": 1.8, "precioBs": 60.0, "presentacion": "20 tabletas"},
    ],
    "omeprazol": [
        {"producto": "OMEPRAZOL 20 mg x 14 cápsulas", "nombre": "OMEPRAZOL",
         "productId": "p-ome-1", "precio": 2.4, "precioBs": 80.0, "presentacion": "14 cápsulas"},
    ],
    "amoxicilina": [
        {"producto": "AMOXICILINA 500 mg x 21 cápsulas", "nombre": "AMOXICILINA",
         "productId": "p-amx-1", "precio": 3.0, "precioBs": 100.0, "presentacion": "21 cápsulas"},
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
    """Ronda de SOLO herramienta (sin texto)."""
    return _R(None, [_TC(f"call-{n}", nombre, args)])


def txt(contenido: str) -> _R:
    """Ronda de SOLO texto."""
    return _R(contenido, None)


class CrmStub:
    """Contrato mínimo del CRM (igual que la sonda). `errores` = substrings de
    término que simulan un catálogo caído (CrmError = error/timeout)."""

    def __init__(self, errores: set[str] | None = None) -> None:
        self.enviados: list[str] = []
        self.handoffs: list[str] = []
        self.errores = errores or set()
        self.consultas: list[str] = []

    async def get_context(self, wa_identity: str, conversation_id: str | None = None) -> dict:
        return {
            "conversation": {"id": f"conv-{wa_identity}", "aiEnabled": True, "windowOpen": True},
            "providerId": "demo",
            "contact": {"name": "QA Probe", "phone": wa_identity},
        }

    async def get_products(self, provider_id: str, q: str | None = None, limit: int = 20) -> dict:
        q_norm = (q or "").lower()
        self.consultas.append(q_norm)
        for marca in self.errores:
            if marca in q_norm:
                raise CrmError(f"catálogo caído para {q_norm!r}")
        prods: list[dict[str, Any]] = []
        for clave, lista in CATALOGO.items():
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
    """Devuelve las respuestas del guion en orden; al agotarse, repite la última
    (igual que la sonda: así un término no autorizado que repite texto se mide)."""

    def __init__(self, guion: list[_R]) -> None:
        self.guion = guion
        self.i = 0
        self.llamadas = 0

    async def complete(self, messages, tools=None):
        idx = min(self.i, len(self.guion) - 1)
        r = self.guion[idx]
        self.i += 1
        self.llamadas += 1
        return r


async def correr(nombre: str, guion: list[_R], mensaje: str,
                 errores: set[str] | None = None) -> dict:
    """Corre un turno real y devuelve {respuestas, llamadas_llm, consultas, excepcion}."""
    crm = CrmStub(errores=errores)
    ctx = AppContext(
        settings=Settings(intent_layer=False),
        store=MemoryStore(),
        crm=crm,
        llm=FakeLlm(guion),
        profile=None,
    )
    identidad = f"p1-{nombre.lower().replace(' ', '-')}"
    previo, ctx.lab_outbox = ctx.lab_outbox, []
    excepcion = None
    try:
        await turn_mod.handle_flush(
            ctx, identidad,
            [InboundMessage(wa_message_id=f"wamid-{nombre}", identity=identidad,
                            type="text", text=mensaje)],
        )
    except Exception as exc:  # noqa: BLE001
        excepcion = f"{type(exc).__name__}: {exc}"
    finally:
        respuestas = list(ctx.lab_outbox or [])
        ctx.lab_outbox = previo
    return {
        "respuestas": respuestas,
        "llamadas_llm": ctx.llm.llamadas,
        "consultas": crm.consultas,
        "excepcion": excepcion,
    }
