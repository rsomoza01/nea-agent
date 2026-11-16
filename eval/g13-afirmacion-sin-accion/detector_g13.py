"""G13 — sonda de AFIRMACIÓN DE ACCIÓN SIN ACCIÓN (propuesta, read-only).

Qué hace: recoge los textos del AGENTE de las corridas del Laboratorio y de las respuestas del
camino real, y marca las frases candidatas a veto. NO decide: produce la lista para medir falsos
positivos contra lo que de verdad ocurrió en el turno (logs del contenedor: handoff, carrito,
cierre de pedido). El veto solo debe dispararse cuando la frase aparece Y la acción NO ocurrió.

Uso: PYTHONPATH=<repo> .venv-test/Scripts/python.exe eval/g13-afirmacion-sin-accion/detector_g13.py <runId> [runId...]
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

# Familias de frase. `senal` = lo que hay que comprobar en el turno; si la acción SÍ ocurrió,
# la frase es legítima y el veto NO debe dispararse (ese es el falso positivo a evitar).
FAMILIAS = {
    "escalado_ya_avisado": {
        # Ya cubierto por G11 cuando NO hubo handoff; aquí la frase suele ser legítima
        # (el handoff ocurrió). Se mide para no duplicar el veto.
        "frases": r"(ya (avis|notific|inform)\w* al equipo|ya (le )?pas\w+ (tu|el) (caso|mensaje)|"
                  r"el equipo ya (est|fue) avisad\w+|ya (te )?comuniqu\w+ con)",
        "senal": "handoff_reason (si es None, la afirmación es falsa)",
    },
    "pedido_registrado": {
        "frases": r"(qued[oó] (registrad|anotad)\w*|tu pedido (ya )?(qued|est)\w* (registrad|listo)|"
                  r"(ya )?(finalic|cerr)\w+ (tu|el) pedido|pedido (finalizad|cerrad)\w*|"
                  r"lo dej\w+ (todo )?listo|listo, qued[oó])",
        "senal": "finalizar_pedido llamado con éxito EN EL TURNO (hoy no existe flag)",
    },
    "agregado_al_pedido": {
        "frases": r"((ya )?(agregu|a[nñ]ad|sum)\w+ .{0,24}(al|a tu) (pedido|carrito)|"
                  r"lo (dej|pus)\w+ en (tu )?(pedido|carrito)|ya est[aá] en tu pedido)",
        "senal": "cart_add / backstop_added_skus en el turno",
    },
    "apartado_o_agendado": {
        "frases": r"((te )?lo (apart|reserv)\w+|(te )?agend\w+|queda( )?(apartad|reservad)\w+|"
                  r"reservad\w+ a tu nombre)",
        "senal": "book_session / apartado real (en esta farmacia no existe esa herramienta)",
    },
}


def textos_de_corrida(run_id: str) -> list[tuple[str, str, str]]:
    """[(persona, rol, texto)] de una corrida del Laboratorio (transcripts vía API)."""
    try:
        crudo = subprocess.run(
            ["curl", "-sk", "-b", "/tmp/cookies.txt", f"https://localhost/api/lab/runs/{run_id}"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        d = json.loads(crudo)
    except Exception as exc:  # noqa: BLE001
        print(f"  (no pude leer {run_id}: {exc})")
        return []
    salida = []
    for caso in d.get("cases", []):
        for t in caso.get("transcript") or []:
            salida.append((caso.get("persona") or "?", t.get("role") or "?", str(t.get("text") or "")))
    return salida


def textos_camino_real() -> list[tuple[str, str, str]]:
    salida = []
    for carpeta in sorted((RAIZ / "eval" / "f15-frases-conversacion").glob("camino-real-*")):
        archivo = carpeta / "respuestas.jsonl"
        if not archivo.exists():
            continue
        for linea in archivo.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(linea)
            except Exception:  # noqa: BLE001
                continue
            cuerpo = d.get("respuesta") or {}
            for r in (cuerpo.get("replies") or []):
                salida.append((f"{carpeta.name}:{d.get('escenario') or d.get('rep')}", "agente", str(r)))
    return salida


def main() -> int:
    textos: list[tuple[str, str, str]] = []
    for run_id in sys.argv[1:]:
        print(f"=== corrida {run_id} ===")
        textos += textos_de_corrida(run_id)
    textos += textos_camino_real()

    agentes = [t for t in textos if t[1] in ("agente", "assistant", "agent")]
    print(f"\ntextos del agente analizados: {len(agentes)}")
    for nombre, familia in FAMILIAS.items():
        patron = re.compile(familia["frases"], re.IGNORECASE)
        encontrados = [(quien, texto) for quien, _, texto in agentes if patron.search(texto)]
        print(f"\n--- {nombre} ({len(encontrados)} coincidencias) · señal a comprobar: {familia['senal']}")
        for quien, texto in encontrados:
            print(f"   [{quien}] {texto[:220]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
