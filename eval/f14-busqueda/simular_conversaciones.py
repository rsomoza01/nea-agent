"""F14 — simulador de CONVERSACIONES completas (WhatsApp simulado).

Qué es: se ejecuta DENTRO del contenedor `nea` (donde viven las dependencias),
usa el código REAL del agente (`handle_flush` → `run_turn` → herramientas →
vetos), el MODELO REAL (LongCat por el proxy Nous :11500) y un CATÁLOGO
CONTROLADO con los nombres reales documentados del catálogo del cliente.

Por qué un catálogo controlado: el entorno local no tiene credenciales de
Firebase, así que el catálogo del CRM responde vacío y ninguna conversación
podría mostrar la precisión de búsqueda. Aquí se sirve un catálogo con los
productos medidos en el diagnóstico (incluido el ruido histórico: TE AMO PRESERV,
VITAMINA K/E, PASTA PRIMOR…) y se emula el matcher del CRM (AND de tokens +
prefijos + Levenshtein ≤2 + segunda pasada difusa), que es lo que hacía posible
los falsos positivos que F14 corrige.

Uso (dentro del contenedor):

    PYTHONPATH=/app python /tmp/simular_conversaciones.py --out /tmp/conv.json --md /tmp/conv.md

Para el control PRE-F14: copiar el árbol previo a /prev y correr con
PYTHONPATH=/prev.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import sys
from typing import Any

# --------------------------------------------------------------- catálogo ---

def _p(nombre: str, precio: float) -> dict[str, Any]:
    return {
        "producto": nombre,
        "nombre": nombre,
        "productId": re.sub(r"[^a-z0-9]+", "-", nombre.lower())[:40],
        "precio": precio,
        "precioBs": round(precio * 750.0, 2),   # tasa BCV ficticia pero estable
    }


CATALOGO = [
    _p("NITROFURANTOINA 100 MG X 20 CAP", 5.20),
    _p("NITROFURANTOINA 50 MG X 20 CAP", 4.10),
    _p("VITAMINA C 500 MG X 10 TAB", 2.30),
    _p("VITAMINA K 10 MG X 30 TAB", 3.10),
    _p("VITAMINA E 400 UI X 10 CAP", 3.60),
    _p("TE AMO PRESERV. 3PCS", 1.20),             # ruido histórico del caso 1
    _p("MACRODANTINA 100 MG X 20 CAP", 6.80),     # marca: el alias NO debe esconderla
    _p("SOLUNOVAR SOLUCION BUCAL X 1 FCO", 7.40),
    _p("PINVEX SOLUCION BUCAL X 1 FCO", 6.90),
    _p("ATAMEL FORTE 650 MG X 10 TAB", 2.90),
    _p("IBUPROFENO 400 MG X 10 TAB", 1.90),
    _p("OMEPRAZOL 20 MG X 30 CAP", 4.50),
    _p("DICLOFENACO 50 MG X 20 TAB", 2.40),
    _p("AMOXICILINA 500 MG X 21 CAP", 5.60),
    _p("AMOXICILINA + ACIDO CLAVULANICO 875 MG X 14 TAB", 9.30),
    _p("LOSARTAN 50 MG X 30 TAB", 4.30),
    _p("FEXOFENADINA 120 MG X 10 TAB", 5.90),
    _p("AZITROMICINA 500 MG X 3 TAB", 6.20),
    _p("METFORMINA 850 MG X 30 TAB", 4.80),
    _p("CLONAZEPAM 2 MG X 30 TAB", 3.40),
    _p("ESOZ 40 MG X 7 CAP", 5.50),
    _p("LEPRIT 25 MG X 30 TAB", 7.50),
    _p("SHAMPU DRENE X 1 FCO", 6.10),
    _p("ACETAMINOFEN 650 MG X 10 TAB", 1.80),
    _p("PASTA PRIMOR PLUMA", 0.90),               # ruido histórico
    _p("DESODORANTE DOVE", 2.10),                 # ruido histórico
    _p("VENDA ELASTICA", 1.50),                   # ruido histórico
    _p("COMPOTA GERANIA", 1.30),                  # ruido histórico
]

_TILDES = str.maketrans("áéíóúüñ", "aeiouun")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (s or "").lower().translate(_TILDES))).strip()


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    fila = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        nueva = [i]
        for j, cb in enumerate(b, 1):
            nueva.append(min(fila[j] + 1, nueva[j - 1] + 1, fila[j - 1] + (ca != cb)))
        fila = nueva
    return fila[-1]


def _matchea_token(q: str, t: str) -> bool:
    """Un token del término contra un token del nombre (motor difuso del CRM)."""
    if q == t:
        return True
    if len(q) >= 4 and len(t) >= 4 and (t.startswith(q) or q.startswith(t)):
        return True
    if len(q) >= 5 and len(t) >= 5 and abs(len(q) - len(t)) <= 2 and _lev(q, t) <= 2:
        return True
    if len(q) >= 6 and q in t:
        return True
    return False


def buscar_catalogo(q: str, limit: int = 20) -> list[dict[str, Any]]:
    """Emula el buscador del CRM: 1ª pasada AND de tokens, 2ª pasada difusa."""
    tokens = [t for t in _norm(q).split() if len(t) >= 2]
    if not tokens:
        return []
    salida: list[dict[str, Any]] = []
    for p in CATALOGO:
        nombre = _norm(p["nombre"])
        partes = nombre.split()
        if all(any(_matchea_token(qt, ht) for ht in partes) for qt in tokens):
            salida.append(p)
    if salida:
        return salida[:limit]
    # Segunda pasada: basta UN token (es lo que metía TE AMO PRESERV en una
    # consulta de nitrofurantoina: 'amor' ≈ 'amo').
    for p in CATALOGO:
        partes = _norm(p["nombre"]).split()
        if any(any(_matchea_token(qt, ht) for ht in partes) for qt in tokens):
            salida.append(p)
    return salida[:limit]


# ------------------------------------------------------------------- crm -----

class CrmStub:
    """CRM de mentira: contexto válido + catálogo controlado; el resto no-ops."""

    def __init__(self) -> None:
        self.consultas: list[str] = []

    async def get_context(self, wa_identity: str, conversation_id: str | None = None):
        return {
            "conversation": {
                "id": f"cv_{wa_identity[:12]}",
                "aiEnabled": True,
                "windowOpen": True,
            },
            "providerId": "demo",
            "providers": [],
            "contact": {"name": "Cliente de prueba"},
            "lead": {},
        }

    async def get_products(self, provider_id: str, q: str | None = None, limit: int = 20):
        self.consultas.append(q or "")
        return {
            "products": buscar_catalogo(q or "", limit),
            "provider": {"nombre": "Farmarcia QA", "address": "Av. Siempre Viva"},
            "paymenType": "Pago Móvil, Efectivo (Bs) y Zelle",
            "hours": "Lunes a sábado 8:00-18:00",
        }

    async def post_typing(self, conversation_id: str):  # noqa: D102
        return None

    async def send_message(self, *a: Any, **k: Any):
        return {"ok": True}

    async def post_reset(self, conversation_id: str):
        return None

    async def post_handoff(self, *a: Any, **k: Any):
        return None

    async def post_paused(self, *a: Any, **k: Any):
        return None

    async def put_ficha(self, *a: Any, **k: Any):
        return None

    async def get_media(self, *a: Any, **k: Any):
        return None

    async def get_providers(self, *a: Any, **k: Any):
        return []

    async def get_availability(self, *a: Any, **k: Any):
        return []

    async def create_booking(self, *a: Any, **k: Any):
        return {}

    async def reschedule_booking(self, *a: Any, **k: Any):
        return {}

    async def aclose(self):
        return None


# ------------------------------------------------------------ escenarios -----

ESCENARIOS: list[dict[str, Any]] = [
    {
        "id": "1-apelativo-y-vitamina",
        "titulo": "Caso 1 real: apelativo + nitrofurantoina + vitamina C",
        "nota": "El término viejo era 'amor nitrofurantoina 50mg vitamina' y 'amor' validaba TE AMO PRESERV.",
        "mensajes": [
            "Buenas tardes!! Mi amor que precio tiene la nitrofurantoina de 50mg y la vitamina C",
            "dame 2 cajas de la nitro",
            "y la vitamina c cuanto sale?",
            "no gracias, eso es todo",
        ],
    },
    {
        "id": "2-vivacina",
        "titulo": "Caso 2 real: typo que cruzaba de categoría",
        "nota": "Antes: 'vivacina' devolvía VITAMINA K y VITAMINA C (fuzzy de 2 ediciones).",
        "mensajes": [
            "vivacina",
            "o sea que lo tienes o no?",
            "y cuanto cuesta?",
        ],
    },
    {
        "id": "3-opcion-en-mensaje-mixto",
        "titulo": "Caso 3 real: 'opción N' dentro de un mensaje con otra pregunta",
        "nota": "Antes: no resolvía la opción 9 y buscaba 'macrodantina querer vitamina opción'.",
        "mensajes": [
            "que vitaminas tienen?",
            "Precio de la macrodantina?? Voy a querer una vitamina C de la opción 2",
            "listo, eso es todo",
        ],
    },
    {
        "id": "4-dos-bucales-typo",
        "titulo": "Caso 4 real: dos medicamentos y un typo (solunoval → solunovar)",
        "nota": "Antes: un solo término concatenado y apertura 'No tengo SOLUNOVAL BUCAL' + lista.",
        "mensajes": [
            "buenas tardes precio de solunoval solución bucal y pinvex solución bucal",
            "quiero 1 caja de la opción 1",
            "listo",
        ],
    },
    {
        "id": "5-typos-simples",
        "titulo": "Typos que ya funcionaban (no deben romperse)",
        "nota": "omeprasol, diclofencao, fexofenasina, azitromisina, clonacepan.",
        "mensajes": [
            "tienen omeprasol 20?",
            "y diclofencao 50?",
            "me sirve el fexofenasina para la alergia?",
            "ok gracias, hasta luego",
        ],
    },
    {
        "id": "6-lista-de-medicamentos",
        "titulo": "Lista de varios medicamentos en un mensaje",
        "nota": "esoz, leprit y evigax en una sola línea.",
        "mensajes": [
            "esoz, leprit y evigax",
            "quiero 3 cajas de la opción 2",
            "no, nada mas",
        ],
    },
    {
        "id": "7-multi-conector",
        "titulo": "Dos medicamentos con conector + pregunta de precio",
        "nota": "nitrofurantoina y la vitamina C (dos búsquedas, no una frase).",
        "mensajes": [
            "que precio tiene la losartana de 50 y la metformina de 850",
            "quiero 1 caja de cada uno",
            "listo",
        ],
    },
    {
        "id": "8-coformulacion",
        "titulo": "Co-formulación: no debe partirse en dos medicamentos",
        "nota": "amoxicilina + ácido clavulánico es UN producto.",
        "mensajes": [
            "tienen amoxicilina y acido clavulanico?",
            "cuanto sale?",
        ],
    },
    {
        "id": "9-cortesia-sola",
        "titulo": "Cortesía sin consulta (no debe listar productos)",
        "nota": "El saludo solo no es una búsqueda.",
        "mensajes": [
            "hola mi amor buenos días",
            "gracias, linda",
        ],
    },
    {
        "id": "11-vitamina-con-letra",
        "titulo": "Letra de la vitamina: 'vitamina C' no debe traer K ni E",
        "nota": "El rescate escalonado del árbol integrado se apoyaba en 'vitamina' y colaba todas.",
        "mensajes": [
            "tienen vitamina c?",
            "y vitamina k?",
        ],
    },
    {
        "id": "12-frases-que-no-son-medicamento",
        "titulo": "Frases de conversación que NO son consultas (quejas reales oct-2026)",
        "nota": "El agente busca el catálogo con 'ser indu', 'vivo ciudad bolivar', 'marca', 'ahroa'.",
        "mensajes": [
            "No puede ser Indu.",
            "Vivo en Ciudad Bolivar.",
            "Solo una marca?",
            "Por ahroa no",
        ],
    },
    {
        "id": "13-frases-con-contexto",
        "titulo": "Las mismas frases EN MITAD de la conversación (contexto real de las quejas)",
        "nota": "Tras listar un producto, la frase de conversación debe seguir sin buscar.",
        "mensajes": [
            "tienen macrodantina?",
            "No puede ser Indu.",
            "Solo una marca?",
            "Por ahroa no",
        ],
    },
    {
        "id": "14-queja-y-humano",
        "titulo": "Queja sin detalle y petición de humano (casos amarillos del Laboratorio)",
        "nota": "En el Lab el agente cerró el chat: «Oigan, esto es el colmo» -> «Listo. Hasta luego.»",
        "mensajes": [
            "Oigan, esto es el colmo",
            "me atendieron mal y quiero hablar con una persona",
            "hola",
        ],
    },
    {
        "id": "15-entrega-fuera-de-kb",
        "titulo": "Pregunta por entrega/horario que el agente NO sabe (rojo del Laboratorio)",
        "nota": "El Lab marcó alucinación: «¡Claro que sí! Hacemos entregas a domicilio.»",
        "mensajes": [
            "Hola, una pregunta",
            "¿Me dan la receta de la abuela? No, ¿hacen entrega a domicilio los domingos?",
            "¿Y si el medicamento llega vencido me lo cambian?",
        ],
    },
    {
        "id": "10-marca-no-se-esconde",
        "titulo": "Marca en el catálogo: el alias no debe esconderla",
        "nota": "macrodantina existe como producto; el alias solo reescribe typos, no marcas.",
        "mensajes": [
            "tienen macrodantina?",
            "y la nitrofurantoina?",
        ],
    },
]


# ------------------------------------------------------------------ runner ----

def _contexto():
    """AppContext real con store en memoria, LLM real y CRM de mentira."""
    from app.config import Settings
    from app.llm import OpenAiLlm
    from app.state import AppContext, MemoryStore

    settings = Settings()
    firma = inspect.signature(OpenAiLlm.__init__)
    kwargs: dict[str, Any] = {}
    for nombre, attr in (
        ("transcribe_model", "openai_transcribe_model"),
        ("base_url", "openai_base_url"),
        ("transcribe_api_key", "transcribe_api_key"),
        ("transcribe_base_url", "transcribe_base_url"),
    ):
        if nombre in firma.parameters:
            valor = getattr(settings, attr, None)
            kwargs[nombre] = valor or None
    llm = OpenAiLlm(settings.openai_api_key, settings.openai_model, **kwargs)
    return AppContext(settings=settings, store=MemoryStore(), crm=CrmStub(), llm=llm, profile=None)


async def correr(escenarios: list[dict[str, Any]]) -> dict[str, Any]:
    from app.state import InboundMessage
    from app.turn import handle_flush

    ctx = _contexto()
    salida: dict[str, Any] = {"modelo": ctx.settings.openai_model, "escenarios": []}
    for esc in escenarios:
        identidad = f"5491100{hash(esc['id']) % 10000:04d}"
        conversacion: list[dict[str, Any]] = []
        for n, mensaje in enumerate(esc["mensajes"]):
            outbox: list[str] = []
            previo = ctx.lab_outbox
            ctx.lab_outbox = outbox
            try:
                await handle_flush(
                    ctx,
                    identidad,
                    [InboundMessage(
                        wa_message_id=f"wamid-{esc['id']}-{n}",
                        identity=identidad,
                        type="text",
                        text=mensaje,
                    )],
                )
            finally:
                ctx.lab_outbox = previo
            conversacion.append({"cliente": mensaje, "agente": outbox})
        salida["escenarios"].append({
            "id": esc["id"],
            "titulo": esc["titulo"],
            "nota": esc["nota"],
            "conversacion": conversacion,
        })
    salida["consultas_al_catalogo"] = getattr(ctx.crm, "consultas", [])
    return salida


def markdown(datos: dict[str, Any]) -> str:
    lineas = [f"# Conversaciones simuladas — modelo `{datos['modelo']}`", ""]
    for esc in datos["escenarios"]:
        lineas.append(f"## {esc['id']} · {esc['titulo']}")
        lineas.append(f"_{esc['nota']}_")
        lineas.append("")
        for turno in esc["conversacion"]:
            lineas.append(f"**Cliente:** {turno['cliente']}")
            if turno["agente"]:
                for r in turno["agente"]:
                    lineas.append(f"**Agente:** {r.strip()}")
            else:
                lineas.append("**Agente:** _(silencio)_")
            lineas.append("")
        lineas.append("---")
        lineas.append("")
    return "\n".join(lineas)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/conversaciones.json")
    ap.add_argument("--md", default="/tmp/conversaciones.md")
    ap.add_argument("--solo", default="", help="ids separados por coma")
    args = ap.parse_args()
    escenarios = ESCENARIOS
    if args.solo:
        pedidos = {s.strip() for s in args.solo.split(",")}
        escenarios = [e for e in ESCENARIOS if e["id"] in pedidos]
    datos = asyncio.run(correr(escenarios))
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=1)
    with open(args.md, "w", encoding="utf-8") as fh:
        fh.write(markdown(datos))
    print(f"escrito {args.out} y {args.md} ({len(escenarios)} escenarios)")
    for esc in datos["escenarios"]:
        print(f"\n=== {esc['id']} ===")
        for turno in esc["conversacion"]:
            print(f"  C: {turno['cliente'][:90]}")
            for r in turno["agente"] or ["(silencio)"]:
                print(f"  A: {r.strip()[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
