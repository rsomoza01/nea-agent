"""Batería: el agente no puede reconstruir un pedido viejo.

Dos casos reales del provider 05:

1. 2026-11-12 — reenvió una lista de ACETAMINOFEN de 13 días antes ante un mensaje
   administrativo (cubierto por `test_lista_vieja.py`).
2. 2026-11-13 — a "A mí se me borraron los chats" contestó:
   "Tranquilo, aquí sí quedó todo. Tu pedido es 1 caja de ACETAMINOFEN 500MG TAB X 20
   -ELTER- por $1.80, ya registrado para que un asesor lo procese."
   Ese pedido era de 37 días antes y el carrito estaba VACÍO.

Aquí se prueba la puerta ampliada (`_es_lista_de_precios` reconoce también prosa de pedido)
y la frescura del contexto (`_contexto_fresco` + bloque de estado).
"""
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, "/Users/robertosomoza/HermesProyectos/nea-agent")

from app import turn  # noqa: E402

AHORA = datetime.now(timezone.utc)

MENSAJE_VIEJO = (
    "Tu pedido ha sido registrado exitosamente. Total: **$1.80**. Un humano lo procesará "
    "para su entrega."
)
LISTA = "💊 1. ACETAMINOFEN 650 MG X 10 GENVEN\n   $0,52  |  Bs 462,41"
CORTESIA = "¡Con gusto! Que estés muy bien 🙌"
PREGUNTA = "¿Me confirmas cuántas cajas necesitas?"


def msg(contenido, horas):
    return SimpleNamespace(role="assistant", content=contenido,
                           created_at=AHORA - timedelta(hours=horas))


def main():
    ok = total = 0

    def check(nombre, cond, extra=None):
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print(f"  {'OK ' if cond else 'MAL'} {nombre}" + ("" if cond or extra is None else f" → {extra}"))

    print("── la puerta reconoce la prosa de pedido ──")
    check("resumen de pedido en prosa (caso real) se detecta", turn._es_lista_de_precios(MENSAJE_VIEJO))
    check("lista con 💊 se detecta", turn._es_lista_de_precios(LISTA))
    check("cortesía con emoji no se detecta", not turn._es_lista_de_precios(CORTESIA))
    check("pregunta sin precios no se detecta", not turn._es_lista_de_precios(PREGUNTA))
    check("precio sin palabras de pedido no se detecta",
          not turn._es_lista_de_precios("El envío sale $2,00"))
    check("'total' sin precio no se detecta", not turn._es_lista_de_precios("El total lo ves al pagar"))

    print("\n── el mensaje viejo sale del prompt ──")
    fuera = turn._sin_lista_vieja(msg(MENSAJE_VIEJO, 37 * 24), AHORA)
    check("pedido de 37 días se sustituye", "[lista de precios enviada" in fuera, fuera)
    check("no queda el importe", "$1.80" not in fuera and "1,80" not in fuera, fuera)
    check("no queda 'ya registrado'", "registrad" not in fuera.lower(), fuera)
    check("pedido de hace 2 h se conserva",
          turn._sin_lista_vieja(msg(MENSAJE_VIEJO, 2), AHORA) == MENSAJE_VIEJO)
    check("cortesía vieja se conserva",
          turn._sin_lista_vieja(msg(CORTESIA, 37 * 24), AHORA) == CORTESIA)

    print("\n── frescura del contexto de producto ──")
    check("sin fecha = NO fresco", not turn._contexto_fresco(SimpleNamespace(contexto_at=None)))
    check("de hace 2 h = fresco",
          turn._contexto_fresco(SimpleNamespace(contexto_at=AHORA - timedelta(hours=2))))
    check("de hace 15 días = NO fresco",
          not turn._contexto_fresco(SimpleNamespace(contexto_at=AHORA - timedelta(days=15))))
    check("fecha sin zona se interpreta UTC",
          turn._contexto_fresco(SimpleNamespace(contexto_at=(AHORA - timedelta(hours=1)).replace(tzinfo=None))))

    print("\n── el bloque de estado no cita productos viejos ──")
    conv = SimpleNamespace(
        phase="descubrimiento",
        delivery_method=None,
        delivery_address=None,
        last_product={"producto": "ACETAMINOFEN 500MG TAB X 20 -ELTER-", "precio": 1.8},
        last_term="acetaminofen 600mg",
        delivery_pending="",
        cart_closed=True,
        cart_summary_shown=False,
        greeted=True,
    )
    bloque = turn._build_state_block(conv, [], contexto_fresco=False)
    check("avisa que el contexto es viejo", "SIN contexto de producto reciente" in bloque, bloque)
    check("NO dice cuál fue el último producto", "ACETAMINOFEN" not in bloque, bloque)
    check("NO dice la última búsqueda", "acetaminofen 600mg" not in bloque, bloque)
    bloque_ok = turn._build_state_block(conv, [], contexto_fresco=True)
    check("con contexto fresco SÍ aparece", "ACETAMINOFEN" in bloque_ok, bloque_ok[:120])

    print(f"\n  {ok}/{total} OK")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
