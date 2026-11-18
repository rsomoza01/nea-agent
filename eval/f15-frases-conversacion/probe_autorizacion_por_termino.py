"""F15.1 — sonda de la AUTORIZACIÓN POR TÉRMINO (cierre del PENDIENTE #1 de F15).

Mide dos cosas distintas, y por eso tiene dos modos:

1. **La puerta** (sin modelo): tras un turno autorizado, ¿pasan los términos que el MODELO
   decide después (`vitamina`, `acabo`, `ustedes`)? ¿Y la segunda puerta al catálogo
   (`sugerir_generico`) consulta sin autorización?  → el hueco medido en `be8d8ce`.
2. **La capa** (`--model`): con el modelo real, un término nuevo debe ser aprobado o
   rechazado por la comprensión según lo que pidió el cliente, no según su parecido.

Uso (host):
  PYTHONPATH=<repo> .venv-test/Scripts/python.exe eval/f15-frases-conversacion/probe_autorizacion_por_termino.py
  PYTHONPATH=<repo> OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://127.0.0.1:11500/v1 \
    .venv-test/Scripts/python.exe eval/f15-frases-conversacion/probe_autorizacion_por_termino.py \
    --model deepseek/deepseek-v4.1-flash
Uso (contenedor): PYTHONPATH=/app python eval/f15-frases-conversacion/probe_autorizacion_por_termino.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.environ.get(
    "RAIZ", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
))

from app.tools import ToolRuntime  # noqa: E402


class CrmFake:
    """Catálogo mínimo: registra CADA consulta que le llega."""

    def __init__(self):
        self.consultas: list[str] = []

    async def get_products(self, provider_id, q=None, limit=20):
        self.consultas.append(q or "")
        return {
            "products": [{"producto": "VITAMINA C 500 MG", "nombre": "VITAMINA C 500 MG",
                          "precio": 3.0, "precioBs": 2250.0, "productId": "p1",
                          "generico": "acido ascorbico"}],
            "provider": {"nombre": "QA", "paymeType": None, "hours": None},
        }


class StoreFake:
    def __init__(self):
        self.decisiones: list[dict] = []

    async def log_intent_decision(self, **kw):
        self.decisiones.append(kw)


class CtxFake:
    def __init__(self, crm, store, settings, llm):
        self.crm = crm
        self.store = store
        self.settings = settings
        self.llm = llm


def runtime(*, settings, llm, **campos) -> ToolRuntime:
    rt = ToolRuntime.__new__(ToolRuntime)
    rt.intent_fuente = ""
    rt.intent_acto = ""
    rt.intent_motivo = ""
    rt.intent_confianza = None
    rt.intent_hay_consulta = None
    rt.intent_termino = None
    rt.intent_autorizado = None
    rt.texto_cliente = ""
    rt.last_product = None
    rt._provider_id_val = "demo"
    rt._conv = SimpleNamespace(id=1)
    if not hasattr(rt, "intent_terminos_autorizados"):
        rt.intent_terminos_autorizados = set()
    if not hasattr(rt, "intent_rechazos"):
        rt.intent_rechazos = {}
    rt._ctx = CtxFake(CrmFake(), StoreFake(), settings, llm)

    async def registrar_decision_intencion(candidato=None):
        rt._ctx.store.decisiones.append({"candidato": candidato})

    rt.registrar_decision_intencion = registrar_decision_intencion
    for k, v in campos.items():
        setattr(rt, k, v)
    return rt


class Contador:
    def __init__(self):
        self.fallos = 0
        self.pruebas = 0

    def check(self, etiqueta: str, obtenido: str, esperado: str) -> None:
        self.pruebas += 1
        ok = obtenido.startswith(esperado)
        if not ok:
            self.fallos += 1
        print(f"   {etiqueta:<44} -> {obtenido}"
              f"{'' if ok else '   <-- HUECO (esperado ' + esperado + ')'}")

    def informativo(self, etiqueta: str, obtenido: str) -> None:
        print(f"   {etiqueta:<44} -> {obtenido}   (informativo)")


async def veredicto(rt: ToolRuntime, nombre: str) -> str:
    r = await rt._autorizar_busqueda(nombre)
    return "PASA" if r is None else f"NO PASA ({r.get('error')})"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="",
                    help="modelo real para la capa (vacío = la capa no responde y todo término nuevo cae en fail-closed)")
    args = ap.parse_args()

    llm = object()
    settings = SimpleNamespace(intent_layer=True)
    if args.model:
        from app.llm import OpenAiLlm

        base = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11500/v1")
        llm = OpenAiLlm(os.environ.get("OPENAI_API_KEY", "ollama"), args.model, base_url=base)
        settings = SimpleNamespace(
            intent_layer=True, intent_model=args.model,
            intent_api_key=os.environ.get("OPENAI_API_KEY", "ollama"),
            intent_base_url=base, intent_timeout=30,
        )
        print(f"capa: modelo real {args.model} via {base}")
    else:
        print("capa: SIN modelo (todo termino nuevo cae en fail-closed = NO PASA)")
    c = Contador()

    print("\n== A) turno autorizado por el BACKSTOP (termino='vivacina') ==")
    rt = runtime(
        settings=settings, llm=llm,
        texto_cliente="tienen vivacina?",
        intent_fuente="capa", intent_acto="consulta_medicamento",
        intent_motivo="consulta por vivacina", intent_confianza=0.95,
        intent_hay_consulta=True, intent_termino="vivacina", intent_autorizado=True,
    )
    for termino, esperado in (("vivacina", "PASA"), ("vitamina", "NO PASA"),
                              ("acabo", "NO PASA"), ("ustedes", "NO PASA")):
        c.check(f"modelo busca '{termino}'", await veredicto(rt, termino), esperado)

    print("\n== B) turno autorizado por la CAPA (termino='losartan') ==")
    rt = runtime(
        settings=settings, llm=llm,
        texto_cliente="tienes losartan 50 mg?",
        intent_fuente="llm", intent_acto="consulta_medicamento",
        intent_motivo="consulta legitima", intent_confianza=0.95,
        intent_hay_consulta=True, intent_termino="losartan", intent_autorizado=True,
        # Como lo deja la puerta tras aprobar 'losartan' en este mismo turno.
        intent_terminos_autorizados={"losartan"},
    )
    for termino, esperado in (("losartan 50 mg", "PASA"), ("LOSARTAN  50", "PASA"),
                              ("vitamina", "NO PASA"), ("acabo", "NO PASA")):
        c.check(f"modelo busca '{termino}'", await veredicto(rt, termino), esperado)

    print("\n== C) el backstop NEGO el termino: el modelo no lo rescata ==")
    rt = runtime(
        settings=settings, llm=llm,
        texto_cliente="usted es una estafa, no me llego",
        intent_fuente="capa", intent_acto="reclamo",
        intent_motivo="reclamo, no consulta", intent_confianza=0.9,
        intent_hay_consulta=False, intent_termino="estafa", intent_autorizado=False,
    )
    c.check("modelo busca 'estafa' (ya negado por la capa)",
            await veredicto(rt, "estafa"), "NO PASA")

    print("\n== D) segunda puerta: sugerir_generico sin autorizacion ==")
    rt = runtime(settings=settings, llm=llm, texto_cliente="tienen vivacina?")
    r = await rt._sugerir_generico({"nombre": "vitamina"})
    consultas = rt._ctx.crm.consultas
    c.check("sugerir_generico('vitamina') consulta el catalogo",
            "SI consulto" if consultas else "NO consulto", "NO consulto")
    print(f"   resultado: ok={r.get('ok')} error={r.get('error')}")

    if args.model:
        print("\n== E) dos medicamentos en el mismo turno (no debe romperse) ==")
        rt = runtime(
            settings=settings, llm=llm,
            texto_cliente="necesito losartan y metformin",
        )
        c.check("busca 'losartan'", await veredicto(rt, "losartan"), "PASA")
        c.check("busca 'metformin' (segundo termino)", await veredicto(rt, "metformin"), "PASA")

        print("\n== F) derivacion del producto ya mostrado (informativo) ==")
        rt = runtime(
            settings=settings, llm=llm,
            texto_cliente="tienen depomedrol?",
            intent_fuente="llm", intent_acto="consulta_medicamento",
            intent_motivo="consulta por depomedrol", intent_confianza=0.95,
            intent_hay_consulta=True, intent_termino="depomedrol", intent_autorizado=True,
            last_product={"nombre": "DEPOMEDROL 40 MG/ML X 1 AMP"},
        )
        c.check("busca 'depomedrol' (mismo termino)", await veredicto(rt, "depomedrol"), "PASA")
        c.informativo("pide generico 'metilprednisolona'",
                      await veredicto(rt, "metilprednisolona"))

        # Hueco residual medido en el Laboratorio (imagen F15.1 inicial): la capa aprobaba
        # «el mensaje trae una consulta» y ese sí autorizaba el término del MODELO. Con la
        # PREGUNTA ESTRICTA el veredicto es sobre el candidato.
        print("\n== G) el mensaje SÍ trae consulta, pero el candidato es ruido ==")
        rt = runtime(
            settings=settings, llm=llm,
            texto_cliente="cuanto cuesta la fexofenasina?",
        )
        c.check("candidato 'oiga' (relleno) con consulta real en el mensaje",
                await veredicto(rt, "oiga"), "NO PASA")
        c.check("candidato 'firma' (documento de la receta) tras receta con 3 medicamentos",
                await veredicto(rt, "firma"), "NO PASA")
        rt = runtime(
            settings=settings, llm=llm,
            texto_cliente="les mando la foto de la receta, son tres medicamentos",
        )
        c.check("candidato 'maria fernandez cmp 45231' (dato del paciente)",
                await veredicto(rt, "maria fernandez cmp 45231"), "NO PASA")
        print("\n== H) el nombre mal escrito del MISMO medicamento no debe caer ==")
        rt = runtime(
            settings=settings, llm=llm,
            texto_cliente="cuanto cuesta la fexofenasina?",
        )
        c.check("candidato 'fexofenadina' (variante/typo del pedido)",
                await veredicto(rt, "fexofenadina"), "PASA")

    print(f"\nPRUEBAS: {c.pruebas}   HUECOS: {c.fallos}")
    return 1 if c.fallos else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
