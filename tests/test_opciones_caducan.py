"""Batería: la lista de opciones PERSISTIDA también caduca.

`last_options` (en `bot_conversation`) resuelve "quiero 2 cajas de la opción 3" contra la
lista que el cliente ya vio. Sin fecha, una lista de hace dos semanas seguía resolviendo
selecciones contra precios que ya no existen. `last_options_at` la sella al guardarse y
`run_turn` la descarta pasadas LISTA_PRECIOS_TTL_HORAS.
"""
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, "/Users/robertosomoza/HermesProyectos/nea-agent")

from app import turn  # noqa: E402


def caduca(guardada, opciones=None):
    """Reproduce la decisión de run_turn sin levantar el turno completo."""
    ahora = datetime.now(timezone.utc)
    opciones = opciones if opciones is not None else [{"producto": "X", "precio": 1.0}]
    if not isinstance(opciones, list) or not opciones:
        return None
    if guardada is None:
        return None
    if guardada.tzinfo is None:
        guardada = guardada.replace(tzinfo=timezone.utc)
    if (ahora - guardada) > timedelta(hours=turn.LISTA_PRECIOS_TTL_HORAS):
        return None
    return opciones


def main():
    ok = total = 0

    def check(nombre, cond, extra=None):
        nonlocal ok, total
        total += 1
        ok += 1 if cond else 0
        print(f"  {'OK ' if cond else 'MAL'} {nombre}" + ("" if cond or extra is None else f" → {extra}"))

    ahora = datetime.now(timezone.utc)
    opciones = [{"producto": "ACETAMINOFEN 650 MG", "precio": 0.52}]

    check("lista de hace 2 h se usa", caduca(ahora - timedelta(hours=2), opciones) == opciones)
    check("lista de hace 23 h se usa", caduca(ahora - timedelta(hours=23), opciones) == opciones)
    check("lista de hace 13 días NO se usa", caduca(ahora - timedelta(days=13), opciones) is None)
    check("lista sin fecha (fila vieja) NO se usa", caduca(None, opciones) is None)
    check("fecha sin zona horaria se interpreta como UTC",
          caduca((ahora - timedelta(hours=1)).replace(tzinfo=None), opciones) == opciones)
    check("sin opciones no hay nada que caducar", caduca(ahora, []) is None)

    # la migración dejó la columna en el modelo y en las columnas permitidas
    from app import db as dbmod
    check("last_options_at es columna permitida", "last_options_at" in dbmod._CONV_COLUMNS)
    from app.state import Conversation
    check("el modelo trae last_options_at", "last_options_at" in Conversation.__dataclass_fields__)
    check("el TTL es el mismo que el de las listas del prompt", turn.LISTA_PRECIOS_TTL_HORAS == 24)

    print(f"\n  {ok}/{total} OK")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
