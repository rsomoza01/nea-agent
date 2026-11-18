"""Sonda determinista de la linea A (bucle de herramientas) sobre el arbol de
`desarrollo` (5b7815e). QA NEA 2026-11-06.

NO usa modelo real: inyecta un LLM guionado (determinista, coste 0) para forzar
cada caso del guion de QA (A1..A7). Corre el codigo REAL (handle_flush -> run_turn
-> _tool_loop -> herramientas -> vetos) con un CRM/catalogo de mentira y captura
lo que el cliente habria recibido (`ctx.lab_outbox`).

Uso:  python sonda_bucle.py            (desde H:/cualquiera)
      el repo se importa via REPO en sys.path
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

REPO = r"D:/Proyectos/GenteFarma/agentNEA"
sys.path.insert(0, REPO)

logging.basicConfig(level=logging.WARNING, format="    [log] %(levelname)s %(message)s")

from app import turn as turn_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.state import AppContext, InboundMessage, MemoryStore  # noqa: E402

CATALOGO = {
    "losartan": [
        {"producto": "LOSARTAN 50 mg x 30 tabletas", "nombre": "LOSARTAN",
         "productId": "p-losartan-1", "precio": 3.5, "precioBs": 120.0, "presentacion": "30 tabletas"},
        {"producto": "LOSARTAN 100 mg x 30 tabletas", "nombre": "LOSARTAN",
         "productId": "p-losartan-2", "precio": 5.0, "precioBs": 170.0, "presentacion": "30 tabletas"},
    ],
}
CATALOGO_DEFECTO = [
    {"producto": "PRODUCTO GENERICO X x 20 tabletas", "nombre": "GENERICO X",
     "productId": "p-gen-1", "precio": 1.9, "precioBs": 65.0, "presentacion": "20 tabletas"},
]


# --------------------------------------------------------------------------
# Stub del CRM (contrato de eval/f14-busqueda/simular_conversaciones.py)
# --------------------------------------------------------------------------
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
        q_norm = (q or "").lower()
        prods = None
        for clave, lista in CATALOGO.items():
            if clave in q_norm:
                prods = lista
                break
        if prods is None:
            prods = CATALOGO_DEFECTO
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


# --------------------------------------------------------------------------
# LLM guionado
# --------------------------------------------------------------------------
class _TC:
    def __init__(self, tc_id: str, name: str, arguments: dict) -> None:
        self.id = tc_id
        self.name = name
        self.arguments = arguments


class _R:
    def __init__(self, content: str | None = None, tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeLlm:
    """Devuelve las respuestas del guion en orden; cuando se agotan, repite la ultima."""

    def __init__(self, guion: list[_R]) -> None:
        self.guion = guion
        self.i = 0
        self.llamadas: list[dict] = []

    async def complete(self, messages, tools=None):
        idx = min(self.i, len(self.guion) - 1)
        r = self.guion[idx]
        self.i += 1
        self.llamadas.append({
            "n": self.i,
            "tools_ofrecidas": len(tools or []),
            "devuelve": "texto" if (r.content and not r.tool_calls) else
                        f"tool_calls:{[tc.name for tc in (r.tool_calls or [])]}",
        })
        return r


def _tc(n: int, nombre: str, args: dict) -> _R:
    return _R(None, [_TC(f"call-{n}", nombre, args)])


async def correr(nombre: str, guion: list[_R], mensaje: str) -> dict:
    ctx = AppContext(
        settings=Settings(intent_layer=False),
        store=MemoryStore(),
        crm=CrmStub(),
        llm=FakeLlm(guion),
        profile=None,
    )
    identidad = f"qa-probe-{nombre.lower().replace(' ', '-')}"
    previo, ctx.lab_outbox = ctx.lab_outbox, []
    try:
        await turn_mod.handle_flush(
            ctx, identidad,
            [InboundMessage(wa_message_id=f"wamid-{nombre}", identity=identidad,
                            type="text", text=mensaje)],
        )
    except Exception as exc:  # noqa: BLE001
        ctx.lab_outbox.append(f"<EXCEPCION {type(exc).__name__}: {exc}>")
    finally:
        respuestas = list(ctx.lab_outbox or [])
        ctx.lab_outbox = previo
    return {"escenario": nombre, "mensaje": mensaje,
            "llamadas_llm": ctx.llm.llamadas, "respuestas": respuestas}


ESCENARIOS = [
    # A1 — llamadas validas todas las rondas, sin texto final
    ("A1 agotamiento normal", [_tc(i, "buscar_medicamento", {"nombre": m})
                               for i, m in enumerate(
                                   ["losartan 50 mg", "metformina 850 mg", "ibuprofeno 400 mg",
                                    "omeprazol 20 mg", "amoxicilina 500 mg", "vitamina c"], 1)],
     "busco losartan 50 mg"),
    # A2 — llamadas repetitivas identicas
    ("A2 llamadas repetitivas", [_tc(i, "buscar_medicamento", {"nombre": "losartan 50 mg"})
                                 for i in range(1, 7)],
     "busco losartan 50 mg"),
    # A3 — argumentos vacios repetidos
    ("A3 argumentos vacios", [_tc(i, "buscar_medicamento", {}) for i in range(1, 7)],
     "busco losartan 50 mg"),
    # A4 — varios medicamentos, una llamada valida por ronda (6 > MAX_TOOL_ROUNDS=5)
    ("A4 seis medicamentos", [_tc(i, "buscar_medicamento", {"nombre": m})
                              for i, m in enumerate(
                                  ["losartan 50 mg", "metformina", "ibuprofeno", "omeprazol",
                                   "amoxicilina", "vitamina c"], 1)],
     "necesito losartan, metformina, ibuprofeno, omeprazol, amoxicilina y vitamina c"),
    # A7 — respuesta de texto valida justo en la ultima ronda permitida (5)
    ("A7 texto en la ronda 5", [
        _tc(1, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        _tc(2, "buscar_medicamento", {"nombre": "losartan 100 mg"}),
        _tc(3, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        _tc(4, "buscar_medicamento", {"nombre": "losartan 100 mg"}),
        _R("Tengo LOSARTAN 50 mg (3.5 USD) y LOSARTAN 100 mg (5.0 USD). ¿Cuál te sirvo?", None),
        _R("Tengo LOSARTAN 50 mg (3.5 USD) y LOSARTAN 100 mg (5.0 USD). ¿Cuál te sirvo?", None),
    ], "busco losartan"),
    # Control — una sola llamada y luego texto (flujo normal)
    ("C0 control flujo normal", [
        _tc(1, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        _R("Tengo LOSARTAN 50 mg x 30 tabletas a 3.5 USD (120 Bs). ¿Te la agrego?", None),
    ], "busco losartan 50 mg"),
]


async def main() -> None:
    resultados = []
    for nombre, guion, mensaje in ESCENARIOS:
        print(f"\n=== {nombre} (cliente: {mensaje!r})")
        res = await correr(nombre, guion, mensaje)
        resultados.append(res)
        print(f"    llamadas al LLM: {len(res['llamadas_llm'])}  "
              f"rondas con tool_calls: {sum(1 for l in res['llamadas_llm'] if l['devuelve'].startswith('tool'))}")
        if res["respuestas"]:
            for r in res["respuestas"]:
                print(f"    RESPUESTA AL CLIENTE: {r[:300]!r}")
        else:
            print("    RESPUESTA AL CLIENTE:  <NINGUNA — turno sin respuesta (silencio)>")
    print("\n\n=== RESUMEN ===")
    for r in resultados:
        print(f"{r['escenario']:<28} respuestas={len(r['respuestas'])}")
    with open(r"C:/Users/arqui/AppData/Local/hermes/cache/scratch/sonda_bucle.json", "w",
              encoding="utf-8") as fh:
        json.dump(resultados, fh, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    asyncio.run(main())
