"""Compara las dos corridas pareadas del Laboratorio (pre-F15.1 vs con F15.1)."""
import json
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[0]  # eval/
resumen = {}
for et in ("pre-f151", "con-f151"):
    archivo = RAIZ / f"lab-ab-f151-{et}.json"
    d = json.loads(archivo.read_text(encoding="utf-8"))
    r, casos = d["run"], d["cases"]
    print(f"--- {et}: run={r['id']} status={r['status']} score={r['score']}")
    print("    ", dict(Counter(x["status"] for x in casos)))
    resumen[et] = {x["persona"]: x.get("veredicto") for x in casos}
    for x in casos:
        v = x.get("veredicto")
        if v != "verde":
            ev = ((x.get("hallazgos") or [{}])[0].get("evidencia") or "")[:120]
            print(f"    {v}: {x['persona']} :: {ev}")

print("\n=== persona por persona ===")
personas = sorted(set(resumen["pre-f151"]) | set(resumen["con-f151"]))
for p in personas:
    a, b = resumen["pre-f151"].get(p), resumen["con-f151"].get(p)
    marca = "" if a == b else "   <-- CAMBIA"
    print(f"  {p:<22} pre={a:<8} con={b}{marca}")

for et in ("pre-f151", "con-f151"):
    txt = (RAIZ / f"lab-ab-f151-{et}-intent.txt")
    if txt.exists():
        lineas = [l for l in txt.read_text(encoding="utf-8").splitlines() if "|" in l and "fuente" not in l]
        rechazos = [l for l in lineas if l.split("|")[2].strip() == "f"]
        print(f"\n{et}: decisiones={len(lineas) - 2} rechazos={len(rechazos)}")
        print("   rechazados:", " | ".join(l.split("|")[3].strip() for l in rechazos))
