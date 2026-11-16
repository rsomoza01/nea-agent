"""Paso 6 — Partición de mensajes salientes de WhatsApp.

La responsabilidad extraída de `turn.py` a `wtext.py` es la partición de un
mensaje largo en partes enviables (`partir_mensaje_largo` + `WA_MAX_CHARS`).
Función pura, sin estado ni efectos. Estos tests cubren:

- unidad: la lógica de corte (párrafos, tope, bordes) — hoy sin pruebas;
- fachada: `turn.py` sigue exponiendo los nombres antiguos por compatibilidad;
- integración (turno en vivo): `turn._send` formatea y luego parte;
- integración (reenvío diferido): `sender._attempt` usa la misma partición.
"""
from __future__ import annotations

import asyncio
import types

from app import turn as turn_mod
from app.sender import SenderWorker
from app.wtext import WA_MAX_CHARS, a_formato_whatsapp, partir_mensaje_largo


# --------------------------------------------------------------------------- #
# Unidad — partir_mensaje_largo (pura)                                         #
# --------------------------------------------------------------------------- #

def test_tope_por_defecto_es_4000():
    assert WA_MAX_CHARS == 4000


def test_texto_corto_una_sola_parte():
    assert partir_mensaje_largo("hola", 4000) == ["hola"]


def test_vacio_y_solo_espacios_no_devuelven_partes():
    assert partir_mensaje_largo("", 4000) == []
    assert partir_mensaje_largo("   \n\n  ", 4000) == []
    assert partir_mensaje_largo(None, 4000) == []  # type: ignore[arg-type]


def test_corta_por_parrafos_sin_romper_opciones():
    # Dos "opciones" 💊 separadas por línea en blanco; con un tope que no admite
    # ambas juntas, cada opción debe quedar íntegra en su propia parte.
    op1 = "💊 1. PARACETAMOL 500 MG X 10"
    op2 = "💊 2. IBUPROFENO 400 MG X 20"
    texto = f"{op1}\n\n{op2}"
    partes = partir_mensaje_largo(texto, max_chars=len(op1) + 1)
    assert partes == [op1, op2]
    # Ninguna opción quedó partida por la mitad.
    assert all(p.startswith("💊") for p in partes)


def test_acumula_parrafos_mientras_entren():
    partes = partir_mensaje_largo("a\n\nb\n\nc", 4000)
    assert partes == ["a\n\nb\n\nc"]


def test_parrafo_individual_mas_largo_que_el_tope_se_corta_por_lineas():
    parrafo = "L1xxxx\nL2xxxx\nL3xxxx\nL4xxxx"  # 4 líneas de 6 chars + \n
    partes = partir_mensaje_largo(parrafo, max_chars=13)  # entran ~2 líneas
    assert all(len(p) <= 13 for p in partes)
    # Al reunir las líneas no triviales se conserva el contenido.
    assert "".join(partes).replace("\n", "") == parrafo.replace("\n", "")


def test_nunca_devuelve_partes_vacias_y_respeta_tope():
    # Caso tipo "receta de 8 medicamentos": muchos párrafos que exceden 4000.
    bloques = [f"💊 {i}. MEDICAMENTO NUMERO {i} X {i*10}" for i in range(1, 200)]
    texto = "\n\n".join(bloques)
    assert len(texto) > WA_MAX_CHARS
    partes = partir_mensaje_largo(texto, WA_MAX_CHARS)
    assert len(partes) >= 2
    assert all(p for p in partes)              # ninguna vacía
    assert all(len(p) <= WA_MAX_CHARS for p in partes)
    # Todos los bloques originales siguen presentes, en orden.
    reunido = "\n\n".join(partes)
    for b in bloques:
        assert b in reunido


# --------------------------------------------------------------------------- #
# Fachada — turn.py mantiene los nombres antiguos                              #
# --------------------------------------------------------------------------- #

def test_turn_reexporta_la_particion_por_compatibilidad():
    assert turn_mod.WA_MAX_CHARS is WA_MAX_CHARS
    assert turn_mod._partir_mensaje_largo is partir_mensaje_largo


# --------------------------------------------------------------------------- #
# Integración — turno en vivo (turn._send, ruta lab_outbox)                    #
# --------------------------------------------------------------------------- #

def test_send_formatea_y_luego_parte_en_el_turno_vivo():
    # lab_outbox != None corta antes del CRM: capturamos lo que vería el cliente.
    ctx = types.SimpleNamespace(lab_outbox=[])
    # Markdown (**negrita**) + longitud > tope para forzar partición.
    op = "💊 **PARACETAMOL** 500 MG X 10 COMPRIMIDOS"
    texto = "\n\n".join(op for _ in range(300))
    assert len(texto) > WA_MAX_CHARS

    ok = asyncio.run(turn_mod._send(ctx, conv_id=1, crm_conv_id="c1", text=texto))

    assert ok is True
    partes = ctx.lab_outbox
    assert len(partes) >= 2
    assert all(len(p) <= WA_MAX_CHARS for p in partes)
    # El formato de WhatsApp se aplicó ANTES de partir: no quedan ** de Markdown.
    assert all("**" not in p for p in partes)
    assert all("*PARACETAMOL*" in p for p in partes)
    # Equivale a: formatear y luego partir, en ese orden.
    assert partes == partir_mensaje_largo(a_formato_whatsapp(texto), WA_MAX_CHARS)


# --------------------------------------------------------------------------- #
# Integración — reenvío diferido (sender._attempt usa la misma partición)      #
# --------------------------------------------------------------------------- #

class _CrmSpy:
    def __init__(self) -> None:
        self.enviados: list[str] = []

    async def send_message(self, crm_conv_id: str, parte: str) -> None:
        self.enviados.append(parte)


class _StoreSpy:
    def __init__(self) -> None:
        self.entregado: int | None = None
        self.historial: list[tuple[int, str, str]] = []

    async def mark_pending_send_delivered(self, pending_id: int) -> None:
        self.entregado = pending_id

    async def add_message(self, conv_id: int, rol: str, texto: str) -> None:
        self.historial.append((conv_id, rol, texto))


def test_sender_diferido_parte_con_la_misma_funcion():
    crm = _CrmSpy()
    store = _StoreSpy()
    ctx = types.SimpleNamespace(crm=crm, store=store)
    worker = SenderWorker(ctx)  # type: ignore[arg-type]

    contenido = "\n\n".join(f"💊 {i}. MED {i}" for i in range(1, 400))
    assert len(contenido) > WA_MAX_CHARS
    item = types.SimpleNamespace(
        id=7, crm_conversation_id="c9", conversation_id=42,
        content=contenido, attempts=0,
    )

    asyncio.run(worker._attempt(item, now=None))

    # Mismas partes que la función pura, cada una dentro del tope.
    assert crm.enviados == partir_mensaje_largo(contenido, WA_MAX_CHARS)
    assert len(crm.enviados) >= 2
    assert all(len(p) <= WA_MAX_CHARS for p in crm.enviados)
    assert store.entregado == 7
    assert store.historial == [(42, "assistant", contenido)]
