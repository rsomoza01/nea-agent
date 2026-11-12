"""Batería: una lista de precios VIEJA no puede volver al prompt del LLM.

Origen (producción, provider 05, 2026-11-12): el cliente escribió

    "dayana le va a escribir para concretar la intalacion del servicio"

—un mensaje administrativo, sin ningún medicamento— y el agente contestó con la lista de
ACETAMINOFEN del 30 de octubre (13 días antes), copiada del historial, y encima preguntó
por otros productos de esa misma lista vieja.

`_sin_lista_vieja` sustituye por una marca las listas con precio que superan
LISTA_PRECIOS_TTL_HORAS, y deja intacto todo lo demás: cortesías, preguntas, el hilo.
"""
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, "/Users/robertosomoza/HermesProyectos/nea-agent")

from app import turn  # noqa: E402

AHORA = datetime(2026, 11, 12, 22, 0, tzinfo=timezone.utc)

LISTA = """ACETAMINOFÉN
💊 1. ACETAMINOFEN 650 MG X 10 GENVEN
   $0,52  |  Bs 462,41
💊 2. ACETAMINOFEN 500 MG X 10 COMP GENVEN
   $0,52  |  Bs 462,41

👉 Para agregar al carrito: quiero X cajas de la opción Z
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco."""

CORTESIA = "¡Con gusto, José! Cualquier cosa me avisas 🙌"
CONSULTA = "¿Me confirmas cuántas cajas necesitas?"


def msg(contenido, horas):
    return SimpleNamespace(
        role="assistant", content=contenido, created_at=AHORA - timedelta(hours=horas)
    )


def main():
    ok = total = 0

    def check(nombre, cond, extra=None):
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print(f"  {'OK ' if cond else 'MAL'} {nombre}" + ("" if cond or extra is None else f" → {extra}"))

    # 1) lista de 13 días → fuera del prompt
    texto = turn._sin_lista_vieja(msg(LISTA, 13 * 24), AHORA)
    check("lista de 13 días se sustituye", "[lista de precios enviada" in texto, texto[:60])
    check("la marca dice la fecha", "/10" in texto or "/11" in texto, texto)
    check("no queda ni un precio", "$" not in texto and "Bs" not in texto, texto)
    check("no queda el emoji de opción", "💊" not in texto)

    # 2) lista RECIENTE (dentro del TTL) se queda tal cual
    reciente = turn._sin_lista_vieja(msg(LISTA, 2), AHORA)
    check("lista de hace 2 h se conserva", reciente == LISTA, reciente[:50])

    # 3) cortesías y consultas viejas NO se tocan (no son listas)
    check("cortesía de hace 13 días intacta", turn._sin_lista_vieja(msg(CORTESIA, 13 * 24), AHORA) == CORTESIA)
    check("pregunta de hace 13 días intacta", turn._sin_lista_vieja(msg(CONSULTA, 13 * 24), AHORA) == CONSULTA)

    # 4) detección de lista con precio
    check("detecta 💊 + $", turn._es_lista_de_precios(LISTA))
    check("detecta 💊 + Bs sin $", turn._es_lista_de_precios("💊 1. X\n   Bs 100,00"))
    check("no confunde una cortesía", not turn._es_lista_de_precios(CORTESIA))
    check("no confunde un texto con $ suelto sin lista", not turn._es_lista_de_precios("Son $5 en total"))
    check("lista vacía no rompe", not turn._es_lista_de_precios(""))

    # 5) el TTL es de un día
    check("TTL = 24 h", turn.LISTA_PRECIOS_TTL_HORAS == 24, turn.LISTA_PRECIOS_TTL_HORAS)
    check("lista de 23 h se conserva", turn._sin_lista_vieja(msg(LISTA, 23), AHORA) == LISTA)
    check("lista de 25 h se sustituye", "[lista de precios" in turn._sin_lista_vieja(msg(LISTA, 25), AHORA))

    print(f"\n  {ok}/{total} OK")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
