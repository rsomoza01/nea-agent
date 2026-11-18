"""Sonda de la capa de comprensión (F15) — sin LLM, solo caminos deterministas.

Uso: python eval/f15-frases-conversacion/probe_capa.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.intencion import camino_rapido, _extraer_json, _veredicto_desde_json  # noqa: E402

CERCA = "```"


def main() -> int:
    print("== parseo del contrato JSON ==")
    con_cerca = "ok\n" + CERCA + 'json\n{"acto":"saludo","hay_consulta":false,"termino":null,'
    con_cerca += '"confianza":0.9,"motivo":"x"}\n' + CERCA
    print("  con cercas:", bool(_extraer_json(con_cerca)))
    print("  con prólogo:", _veredicto_desde_json(
        _extraer_json('Claro: {"acto":"dilatoria","hay_consulta":false,"termino":null,'
                      '"confianza":0.8,"motivo":"por ahora"}')))
    print("  acto inválido → otro:", _veredicto_desde_json({"acto": "xx", "hay_consulta": False}).acto)
    print("  hay_consulta 'true' (str):", _veredicto_desde_json(
        {"acto": "consulta_medicamento", "hay_consulta": "true", "termino": "atamel 500"}).hay_consulta)
    print("  JSON roto:", _extraer_json("{no es json"))
    print("  llaves dentro de cadena:", _extraer_json('{"motivo":"dice { que no }","acto":"otro"}'))

    print("\n== camino rápido ==")
    casos = [
        ("que precio tiene la nitrofurantoina de 50mg?", "nitrofurantoina 50mg", "consulta"),
        ("tienen atamel forte?", "atamel forte", "consulta"),
        ("vivacina", "vivacina", "nombre"),
        ("shampo dreene", "shampo dreene", "nombre"),
        ("No puede ser Indu.", "ser indu", None),
        ("Vivo en Ciudad Bolivar.", "vivo ciudad bolivar", None),
        ("Solo una marca?", "marca", None),
        ("Por ahroa no", "ahroa", None),
        ("gracias, linda", None, None),
    ]
    ok = True
    for texto, candidato, fuente in casos:
        ent = camino_rapido(texto, candidato)
        obtenido = ent.fuente if ent is not None else None
        bien = obtenido == fuente
        ok = ok and bien
        print(f"  {'OK ' if bien else 'MAL'} {texto!r:45} candidato={candidato!r:22} "
              f"→ {obtenido} (esperado {fuente})")
    print("\nresultado:", "todo como se esperaba" if ok else "REVISAR los MAL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
