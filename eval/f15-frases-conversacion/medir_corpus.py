"""F15 — medición REAL de la capa de comprensión (corpus de 40 frases).

Corre `app.intencion.entender` contra un modelo real vía el proxy Nous
(`http://127.0.0.1:11500/v1`, modelos con "/" → Nous). Ninguna frase se inventa:
salen de las quejas del cliente, del log `med_queries` y de la vida real de una
farmacia por WhatsApp.

Métrica que importa (spec §5):
- **Falsos negativos** (una consulta legítima que NO se busca) = el error caro:
  el cliente pide un medicamento y el agente lo trata como charla.
- **Falsos positivos** (basura buscada) = el error que originó F15.

Uso:
    python eval/f15-frases-conversacion/medir_corpus.py            # modelo por defecto
    python eval/f15-frases-conversacion/medir_corpus.py --model deepseek/deepseek-v4.1-flash
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, RAIZ)

from app.intencion import entender  # noqa: E402
from app.turn import _extraer_termino_medicamento  # noqa: E402

PROXY = os.environ.get("INTENT_PROXY", "http://127.0.0.1:11500/v1")

# (frase, ¿debe buscar?, término esperado (subcadena, en minúsculas) o None)
CORPUS: list[tuple[str, bool, str | None]] = [
    # --- las 4 quejas + primos cercanos: NO deben buscar ---
    ("No puede ser Indu.", False, None),
    ("No puede ser Indu, esa marca no la quiero", False, None),
    ("Vivo en Ciudad Bolivar.", False, None),
    ("Vivo en Ciudad Bolivar, cerca de la plaza", False, None),
    ("Solo una marca?", False, None),
    ("Solo una marca o hay varias?", False, None),
    ("Por ahroa no", False, None),
    ("Por ahora no necesito nada", False, None),
    ("No, gracias, mas tarde te escribo", False, None),
    ("Buenos dias mi amor, como estas?", False, None),
    ("gracias, linda", False, None),
    ("que Dios te bendiga", False, None),
    ("esto me parece muy caro", False, None),
    ("me vendieron un medicamento vencido", False, None),
    ("mi nombre es Maria y tengo 45 anos", False, None),
    ("paso manana por la farmacia", False, None),
    ("tienen delivery?", False, None),
    ("aceptan zelle?", False, None),
    ("a que hora cierran hoy?", False, None),
    ("si, esa es la presentacion que quiero", False, None),
    # --- charla CON verbo de consulta (el fallo que retiró el camino rápido por verbo) ---
    ("tienen delivery?", False, None),
    ("aceptan zelle?", False, None),
    ("a que hora cierran hoy?", False, None),
    ("Solo una marca o hay varias?", False, None),
    ("Por ahora no necesito nada", False, None),
    ("cuanto es el minimo para delivery?", False, None),
    ("hacen envios a San Cristobal?", False, None),
    ("tienen sucursal en el centro?", False, None),
    ("me pueden mandar la factura por correo?", False, None),
    ("necesito hablar con una persona", False, None),
    # --- consultas legítimas: SÍ deben buscar (los falsos negativos son caros) ---
    ("tienen atamel forte?", True, "atamel"),
    ("precio de la macrodantina de 100", True, "macrodantina"),
    ("necesito metformina 850", True, "metformina"),
    ("buenas tardes, precio de la nitrofurantoina de 50mg y la vitamina C", True, "nitrofurantoina"),
    ("vivacina", True, "vivacina"),
    ("shampo dreene", True, "shampo dreene"),
    ("omeprasol 20", True, "omeprasol"),
    ("lorsatan", True, "lorsatan"),
    ("y el diclofencao de 50 cuanto sale?", True, "diclofencao"),
    ("solunoval solucion bucal y pinvex solucion bucal", True, "solunoval"),
    ("amoxicilina y acido clavulanico", True, "amoxicilina"),
    ("esoz, leprit y evigax", True, "esoz"),
    ("tienen vitamina c?", True, "vitamina c"),
    ("digame el precio del losartan de 50", True, "losartan"),
    ("fexofenasina para la alergia esta sirviendo?", True, "fexofenasina"),
    ("busco algo para el dolor de cabeza", True, None),
    ("venden jarabe para la tos?", True, None),
    ("hay crema para las manchas?", True, None),
    ("que antibioticos tienen?", True, None),
    ("mándame el precio de la metformina", True, "metformina"),
]


class LlmProxy:
    """Adaptador mínimo: mismo `complete()` que usa la app, contra el proxy local."""

    def __init__(self, modelo: str) -> None:
        self.modelo = modelo
        self.llamadas = 0
        self.tokens = 0

    async def complete(self, messages, tools=None):  # noqa: ANN001
        cuerpo = json.dumps({"model": self.modelo, "messages": messages,
                             "max_tokens": 1200}).encode()
        req = urllib.request.Request(f"{PROXY}/chat/completions", data=cuerpo,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        self.llamadas += 1

        def _hacer():
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())

        datos = await asyncio.get_event_loop().run_in_executor(None, _hacer)
        self.tokens += (datos.get("usage") or {}).get("total_tokens", 0)

        class _R:
            content = (datos.get("choices") or [{}])[0].get("message", {}).get("content")

        return _R()


def _lev(a: str, b: str) -> int:
    """Levenshtein mínimo (corrección de typos: 'shampo'→'shampoo' = 1)."""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _termino_ok(esperado: str, obtenido: str) -> bool:
    """El término esperado aparece en el obtenido tolerando la corrección de typo.

    'shampo dreene' vs 'shampoo Drene' → cada palabra esperada tiene una palabra
    obtenida a distancia ≤2. Es exactamente lo que la capa debe hacer (F14.4).
    """
    esp = esperado.lower().split()
    obt = obtenido.lower().split()
    return all(any(_lev(e, o) <= 2 for o in obt) for e in esp)


class Settings:
    def __init__(self, **kw):
        self.intent_layer = True
        self.intent_model = ""
        self.intent_api_key = ""
        self.intent_base_url = ""
        self.intent_timeout = 30.0
        self.openai_api_key = ""
        self.openai_base_url = ""
        for k, v in kw.items():
            setattr(self, k, v)


async def medir(modelo: str) -> int:
    llm = LlmProxy(modelo)
    settings = Settings()
    aciertos = 0
    fn: list[tuple] = []   # consultas legítimas tratadas como charla (CARO)
    fp: list[tuple] = []   # basura buscada (el bug original)
    ter: list[tuple] = []  # decisión bien, término imperfecto (se reporta aparte)
    inc: list[tuple] = []  # inconcluyente (timeout/JSON/fail-safe): NO es error de decisión
    filas = []
    t0 = time.time()
    for frase, debe, termino in CORPUS:
        candidato = _extraer_termino_medicamento(frase)
        ent = await entender(frase, llm=llm, settings=settings,
                             termino_candidato=candidato,
                             producto_previo="MACRODANTINA 100 MG X 20 CAP")
        # El veredicto puede venir del camino rápido o de la capa; ambos cuentan.
        obtenido = ent.hay_consulta
        ok = obtenido == debe if obtenido is not None else False
        term_ok = True
        if debe and termino and ent.termino:
            term_ok = _termino_ok(termino, ent.termino)
        aciertos += 1 if (ok and (term_ok or not debe)) else 0
        if obtenido is None:
            # Inconcluyente: la capa no pudo decidir (timeout, JSON, fail-safe). NO es un
            # error de decisión — con el fail-closed el turno pregunta en vez de buscar.
            inc.append((frase, f"{ent.fuente}: {ent.motivo}"))
        elif not ok:
            if obtenido is False and debe:
                fn.append((frase, f"tratada como charla ({ent.acto}: {ent.motivo})"))
            elif obtenido is True and not debe:
                fp.append((frase, f"buscó {ent.termino!r} ({ent.acto}: {ent.motivo})"))
        elif debe and termino and ent.termino and not term_ok:
            # Decisión correcta, término imperfecto: se cuenta aparte (no es falso negativo).
            ter.append((frase, f"término {ent.termino!r} vs esperado {termino!r}"))
        filas.append({
            "frase": frase, "esperado_buscar": debe, "obtenido": obtenido,
            "acto": ent.acto, "termino": ent.termino, "fuente": ent.fuente,
            "confianza": ent.confianza, "motivo": ent.motivo,
        })
    seg = time.time() - t0
    total = len(CORPUS)
    print(f"\nmodelo: {modelo} | frases: {total} | llamadas: {llm.llamadas} | "
          f"tokens: {llm.tokens} | {seg:.1f}s")
    print(f"ACIERTOS: {aciertos}/{total}  ({100 * aciertos / total:.1f}%)")
    print(f"falsos NEGATIVOS (consulta legítima tratada como charla): {len(fn)}")
    for f, m in fn:
        print(f"   ! {f!r} — {m}")
    print(f"falsos POSITIVOS (basura buscada): {len(fp)}")
    for f, m in fp:
        print(f"   ! {f!r} — {m}")
    print(f"INCONCLUYENTES (no es error de decisión: timeout/JSON/fail-safe): {len(inc)}")
    for f, m in inc[:6]:
        print(f"   ? {f!r} — {m}")
    print(f"términos imperfectos (decisión correcta): {len(ter)}")
    for f, m in ter:
        print(f"   ~ {f!r} — {m}")
    salida = {
        "modelo": modelo, "frases": total, "aciertos": aciertos,
        "pct": round(100 * aciertos / total, 1), "falsos_negativos": fn,
        "falsos_positivos": fp, "terminos_imperfectos": ter, "inconclusivos": inc,
        "segundos": round(seg, 1), "filas": filas,
    }
    destino = os.path.join(RAIZ, "eval", "f15-frases-conversacion",
                           f"corpus-{modelo.replace('/', '_')}.json")
    with open(destino, "w", encoding="utf-8") as fh:
        json.dump(salida, fh, ensure_ascii=False, indent=2)
    print("guardado:", destino)
    return 0 if not fn else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    args = ap.parse_args()
    return asyncio.run(medir(args.model))


if __name__ == "__main__":
    raise SystemExit(main())
