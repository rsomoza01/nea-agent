"""F15 — prueba de la AUTORIZACIÓN CENTRAL de búsquedas (revisión externa #1).

Simula lo que hacía el MODELO del chat: llamar `buscar_medicamento` con un término de
conversación, sin pasar por el backstop. Antes se buscaba en el catálogo (`ladrones`,
`estafa`, `puedo devolverlas` quedaron en el log de producción). Ahora la capa decide y,
si no autoriza, la herramienta no busca: pide aclaración.

Uso (en el contenedor): PYTHONPATH=/app python eval/f15-frases-conversacion/probe_autorizacion.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("RAIZ", "/app"))

from app.state import Conversation  # noqa: E402
from app.tools import ToolRuntime  # noqa: E402

CATALOGO = [
    {"producto": "MACRODANTINA 100 MG X 20 CAP", "nombre": "MACRODANTINA 100 MG X 20 CAP",
     "precio": 6.8, "precioBs": 5100.0, "productId": "p1"},
    {"producto": "ATAMEL FORTE X 30 TAB", "nombre": "ATAMEL FORTE X 30 TAB",
     "precio": 4.5, "precioBs": 3375.0, "productId": "p2"},
]


class Fake:
    """Contexto mínimo: solo lo que usa la autorización y la búsqueda."""

    def __init__(self, store, crm, llm):
        self.store = store
        self.crm = crm
        self.llm = llm
        self.settings = None  # se rellena en main

    def __getattr__(self, _n):
        async def _noop(*_a, **_k):
            return None
        return _noop


class CrmFake:
    def __init__(self):
        self.consultas: list[str] = []

    async def get_products(self, provider_id, q=None, limit=20):
        self.consultas.append(q or "")
        return {"products": list(CATALOGO), "provider": {"nombre": "QA"},
                "paymenType": None, "hours": None}


class Vacio:
    def __getattr__(self, _n):
        async def _noop(*_a, **_k):
            return None
        return _noop


async def main() -> int:
    from app.config import Settings
    from app.llm import OpenAiLlm

    ajustes = Settings()
    # LLM REAL (el del contenedor): sin esto la capa no puede decidir y todo es no-OK.
    llm = OpenAiLlm(ajustes.openai_api_key, ajustes.openai_model,
                    base_url=ajustes.openai_base_url or None)
    ajustes.intent_layer = True
    for frase, termino, esperado_buscar in (
        ("me robaron el celular ayer", "ladrones", False),
        ("usted es una estafa, no me llego", "estafa", False),
        ("por ahora no necesito nada", "ahora", False),
        ("vivo en ciudad bolivar", "vivo ciudad bolivar", False),
        ("tienen macrodantina?", "macrodantina", True),
        ("tienen atamel forte?", "atamel forte", True),
    ):
        crm = CrmFake()
        ctx = Fake(Vacio(), crm, llm)
        ctx.settings = ajustes
        rt = ToolRuntime(ctx, Conversation(id=1, wa_identity="wa"), "cv1", provider_id="demo")
        rt.texto_cliente = frase
        salida = await rt._buscar_medicamento({"nombre": termino})
        busco = bool(crm.consultas)
        marca = "OK " if busco == esperado_buscar else "MAL"
        print(f"  {marca} {frase!r:34} -> buscar_medicamento({termino!r}) "
              f"| buscó={busco} (esperado {esperado_buscar}) | {salida.get('error') or 'ok'}"
              f" | acto={rt.intent_acto or '-'} fuente={rt.intent_fuente or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
