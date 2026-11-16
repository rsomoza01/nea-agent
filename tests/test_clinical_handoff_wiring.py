"""E2E del cableado de la derivación clínica (G14 / UC-08) en `run_turn` (Paso 8).

Corre un turno REAL vía `turn.handle_flush` con LLM guionado + CRM de mentira (mismo
contrato que el arnés P1), y verifica el comportamiento observable:
  - una consulta clínica se DERIVA: el bot responde con el mensaje de recepción
    autorizado (NO consejo clínico) y se registra/pide el handoff al humano;
  - si el CRM falla al registrar el handoff, el turno NO se cae y la derivación queda
    registrada igual en NEA (fail-safe);
  - una doble solicitud NO duplica el handoff (anti-duplicado);
  - una consulta clínica con carrito activo igual se deriva (la seguridad prevalece);
  - una consulta COMERCIAL normal NO se deriva (no se debilita el flujo de catálogo).

NO toca F14/F15/F15.1 ni los guards existentes: solo añade la ruta clínica.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import guards
from app import turn as turn_mod
from app.config import Settings
from app.crm import CrmError
from app.state import AppContext, InboundMessage, MemoryStore


class _CrmStub:
    def __init__(self, handoff_falla: bool = False) -> None:
        self.enviados: list[str] = []
        self.handoffs: list[tuple] = []
        self.handoff_falla = handoff_falla

    async def get_context(self, wa_identity: str, conversation_id: str | None = None) -> dict:
        return {
            "conversation": {"id": f"conv-{wa_identity}", "aiEnabled": True, "windowOpen": True},
            "providerId": "demo",
            "contact": {"name": "QA", "phone": wa_identity},
        }

    async def get_products(self, provider_id: str, q: str | None = None, limit: int = 20) -> dict:
        return {"products": [], "provider": {"id": provider_id, "name": "Farmacia QA"},
                "paymenType": "Efectivo", "hours": "8-18"}

    async def send_message(self, *a: Any, **k: Any) -> dict:
        self.enviados.append(str(a))
        return {"ok": True}

    async def post_handoff(self, *a: Any, **k: Any) -> None:
        if self.handoff_falla:
            raise CrmError("CRM caído al registrar handoff")
        self.handoffs.append(a)

    async def post_typing(self, *a: Any, **k: Any) -> None: ...
    async def post_paused(self, *a: Any, **k: Any) -> None: ...
    async def post_typing_final(self, *a: Any, **k: Any) -> None: ...
    async def post_reset(self, *a: Any, **k: Any) -> None: ...
    async def put_ficha(self, *a: Any, **k: Any) -> None: ...
    async def get_media(self, *a: Any, **k: Any) -> Any: return None
    async def get_providers(self, *a: Any, **k: Any) -> list: return []
    async def get_availability(self, *a: Any, **k: Any) -> list: return []
    async def create_booking(self, *a: Any, **k: Any) -> dict: return {}
    async def reschedule_booking(self, *a: Any, **k: Any) -> dict: return {}


class _R:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeLlm:
    """Devuelve SIEMPRE una respuesta de texto (un intento de consejo clínico que el
    guard debe sustituir). Así se prueba que la derivación pisa el texto del LLM."""

    def __init__(self, texto: str = "Para la dosis te recomiendo tomar dos cada 8 horas.") -> None:
        self.texto = texto
        self.llamadas = 0

    async def complete(self, messages, tools=None):
        self.llamadas += 1
        return _R(self.texto, None)


def _ctx(crm: _CrmStub) -> AppContext:
    return AppContext(
        settings=Settings(intent_layer=False),
        store=MemoryStore(),
        crm=crm,
        llm=_FakeLlm(),
        profile=None,
    )


async def _turno(ctx: AppContext, identidad: str, texto: str, wamid: str) -> list[str]:
    previo, ctx.lab_outbox = ctx.lab_outbox, []
    try:
        await turn_mod.handle_flush(
            ctx, identidad,
            [InboundMessage(wa_message_id=wamid, identity=identidad, type="text", text=texto)],
        )
        return list(ctx.lab_outbox or [])
    finally:
        ctx.lab_outbox = previo


def _junto(respuestas: list[str]) -> str:
    return "\n".join(respuestas)


def test_consulta_clinica_se_deriva_y_no_aconseja():
    crm = _CrmStub()
    ctx = _ctx(crm)
    resp = asyncio.run(_turno(ctx, "p8-clin", "¿qué dosis de amoxicilina me tomo?", "w1"))
    texto = _junto(resp)
    # El bot responde con el mensaje de recepción autorizado, NO con consejo clínico.
    assert guards.TPL_DERIVACION_CLINICA in texto
    assert "cada 8 horas" not in texto  # el texto del LLM fue sustituido
    # Se pidió el handoff al humano y se registró la derivación en NEA.
    assert len(crm.handoffs) == 1
    assert len(ctx.store.clinical_handoffs) == 1
    h = next(iter(ctx.store.clinical_handoffs.values()))
    assert h.category.value == "dosis"


def test_derivacion_sobrevive_fallo_del_crm():
    # Aunque post_handoff reviente, el turno no se cae y la derivación queda registrada.
    crm = _CrmStub(handoff_falla=True)
    ctx = _ctx(crm)
    resp = asyncio.run(_turno(ctx, "p8-crmfail", "estoy embarazada, ¿puedo tomar esto?", "w1"))
    assert guards.TPL_DERIVACION_CLINICA in _junto(resp)   # el cliente sí recibe respuesta
    assert crm.handoffs == []                              # el CRM falló
    assert len(ctx.store.clinical_handoffs) == 1           # evidencia durable en NEA


def test_doble_solicitud_no_duplica_handoff():
    crm = _CrmStub()
    ctx = _ctx(crm)
    r1 = asyncio.run(_turno(ctx, "p8-doble", "¿puedo tomar losartán con ibuprofeno?", "w1"))
    r2 = asyncio.run(_turno(ctx, "p8-doble", "¿y qué dosis me tomo entonces?", "w2"))
    # Anti-duplicado: una sola derivación abierta y un ÚNICO handoff al CRM (el 2º
    # mensaje clínico NO vuelve a pedir handoff).
    assert len(ctx.store.clinical_handoffs) == 1
    assert len(crm.handoffs) == 1
    assert guards.TPL_DERIVACION_CLINICA in _junto(r1)
    # El 2º turno responde con el mensaje de ESTADO autorizado (no reanuda consejo).
    assert guards.TPL_DERIVACION_CLINICA_EN_CURSO in _junto(r2)


def test_consulta_clinica_con_carrito_activo_igual_deriva():
    crm = _CrmStub()
    ctx = _ctx(crm)
    identidad = "p8-carrito"
    conv = asyncio.run(ctx.store.get_or_create_conversation(identidad))
    asyncio.run(ctx.store.cart_add(conv.id, "p1", "PARACETAMOL 500", "TAB", "LAB", 2, 1.5, 200.0))
    resp = asyncio.run(
        _turno(ctx, identidad, "¿puedo tomar esto si estoy embarazada?", "w1")
    )
    # La seguridad clínica prevalece sobre la continuidad del pedido: se deriva igual.
    assert guards.TPL_DERIVACION_CLINICA in _junto(resp)
    assert len(ctx.store.clinical_handoffs) == 1


def test_consulta_comercial_no_se_deriva():
    crm = _CrmStub()
    ctx = _ctx(crm)
    asyncio.run(_turno(ctx, "p8-comercial", "¿tienen paracetamol y cuánto cuesta?", "w1"))
    # Ninguna derivación clínica ni handoff: el flujo comercial no se toca.
    assert ctx.store.clinical_handoffs == {}
    assert crm.handoffs == []
