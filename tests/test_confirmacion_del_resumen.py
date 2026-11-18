"""El resumen pendiente de confirmación: "No" se atiende, "2" no rompe.

Caso real (provider 04 = Farmacia VIP, 2026-11-18 16:52-16:54). El cliente armó
AFLAMAX ×2 + ATAMEL FORTE ×1, eligió retirar en farmacia y vio el resumen:

    ... ¿Confirmas el pedido con un *SI*, o quieres agregar otro medicamento?

Lo que pasó después:

    cliente: "No"  → el agente repitió el resumen completo (en otra corrida, el menú
                     de entrega: una pregunta que YA estaba contestada)
    cliente: "2"   → "⚠️ Ojo: esas opciones no coinciden con la última lista que te
                     envié"

El cliente quedó atrapado: no podía agregar, no podía cerrar y repetir el número no
servía. La causa es que el resumen deja el pedido esperando una decisión y ese estado
no se guardaba en ninguna parte: la lista de opciones ya no aplica (el resumen no es
una lista) y la pregunta de entrega ya se contestó.

Lo que se fija aquí:
  · la negativa se reconoce y NO repite ni el resumen ni el menú;
  · un "1"/"2" en ese momento es la ENTREGA (no una opción de catálogo);
  · un "si" apaga el sello y deja cerrar el pedido;
  · hablar de otra cosa apaga el sello sin efectos raros.

Y lo que NO debe cambiar: "no tengo la receta" o "no, tráeme atamel" no son
negativas a confirmar (el patrón es fullmatch, no substring).
"""

import re

from app.turn import _es_negativa_de_confirmacion

# El mensaje determinista, tal como lo compone el resolver.
SIGUE = (
    "Sin problema 👍 Dejo tu pedido tal cual (para retirar en farmacia).\n\n"
    "Si quieres agregar otro medicamento, dime cuál y lo busco. "
    "Y cuando quieras cerrarlo, confírmame con un *SI*."
)


def test_la_negativa_se_reconoce():
    for texto in (
        "No", "no", "NO", "No.", "no gracias", "no, gracias", "nop", "nel",
        "no por ahora", "todavía no", "aun no", "no quiero", "no confirmo",
        "dejalo asi", "no por los momentos",
    ):
        assert _es_negativa_de_confirmacion(texto), texto


def test_lo_que_no_es_negativa_no_se_confunde():
    for texto in (
        "no tengo la receta",       # habla de otra cosa
        "no, tráeme atamel",        # quiere AGREGAR, no rechazar
        "no hay problema, sí",      # confirma
        "2", "1",                    # entrega
        "si", "sí", "ok",            # confirmación
        "",
    ):
        assert not _es_negativa_de_confirmacion(texto), texto


def test_la_negativa_no_repite_el_resumen_ni_el_menu():
    # El texto de la negativa no debe contener ni el resumen (productos/total) ni el
    # menú de entrega, que es lo que confundía al cliente.
    assert "AFLAMAX" not in SIGUE
    assert "Total" not in SIGUE
    assert "Delivery (envío" not in SIGUE
    assert "Responde *1* o *2*" not in SIGUE
    # ...pero sí deja claro cómo seguir y cómo cerrar.
    assert "SI" in SIGUE
    assert "agregar otro medicamento" in SIGUE


def test_la_negativa_nombra_la_entrega_ya_elegida():
    """Recordar la entrega evita que el cliente sienta que se le olvidó al agente."""
    assert "retirar en farmacia" in SIGUE
