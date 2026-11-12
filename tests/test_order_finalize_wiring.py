"""E2E del cableado de `ConfirmarPedidoUseCase` en `tools._finalizar_pedido` (Paso 7).

Verifica que finalizar un pedido:
  - PRESERVA el comportamiento existente (nota de ficha en el CRM, G13
    `pedido_finalizado`, `cart_clear`, `cart_closed`, limpieza de entrega);
  - AÑADE el registro durable del intento de pedido en NEA (auditoría), y que ese
    registro SOBREVIVE aunque la nota del CRM falle (la mejora buscada);
  - NO registra intento ni cierra nada cuando el carrito está vacío.

Corre con `MemoryStore` real (sin Postgres ni CRM reales).
"""
from __future__ import annotations

import asyncio

from app.application.order_workflow import cart_fingerprint
from app.state import MemoryStore
from app.tools import ToolRuntime


class _Settings:
    cart_session_hours = 12.0


class _CrmOk:
    def __init__(self) -> None:
        self.fichas: list[dict] = []

    async def put_ficha(self, conv_id, ficha):
        self.fichas.append({"conv_id": conv_id, "ficha": ficha})
        return {"ok": True}

    async def get_products(self, provider_id, q="", limit=1):
        return {"paymenType": "Efectivo / Pago móvil"}


class _CrmFalla(_CrmOk):
    async def put_ficha(self, conv_id, ficha):
        raise RuntimeError("CRM caído")


class _Ctx:
    def __init__(self, store, crm):
        self.store = store
        self.crm = crm
        self.settings = _Settings()

    def __getattr__(self, _n):  # cualquier otra dependencia no usada aquí
        async def _noop(*_a, **_k):
            return None

        return _noop


async def _finalizar(crm):
    store = MemoryStore()
    conv = await store.get_or_create_conversation("wa-cliente")
    await store.cart_add(conv.id, "p1", "PARACETAMOL 500 MG", "TAB", "LAB", 2, 1.5, 200.0)
    await store.cart_add(conv.id, "p2", "OMEPRAZOL 20 MG", "CAP", "LAB", 1, 3.0, 400.0)
    rt = ToolRuntime(_Ctx(store, crm), conv, "crm-conv-1", provider_id="demo")
    res = await rt._finalizar_pedido()
    return res, store, conv, rt


def test_finaliza_registra_intento_y_preserva_comportamiento():
    crm = _CrmOk()
    res, store, conv, rt = asyncio.run(_finalizar(crm))

    # Comportamiento existente preservado:
    assert res["ok"] is True
    assert len(crm.fichas) == 1 and crm.fichas[0]["ficha"]["resultado"] == "pedido"
    assert rt.pedido_finalizado is True                 # señal G13
    assert store.cart.get(conv.id) in (None, [])         # cart_clear
    assert conv.cart_closed is True                      # cart_closed
    assert conv.delivery_method is None and conv.delivery_pending == ""  # entrega limpia

    # Mejora añadida: intento de pedido PERSISTIDO en NEA con la huella del carrito.
    assert len(store.order_attempts) == 1
    intento = next(iter(store.order_attempts.values()))
    esperado = cart_fingerprint(
        [
            type("I", (), {"product_id": "p1", "cantidad": 2, "precio_usd": 1.5, "precio_bs": 200.0})(),
            type("I", (), {"product_id": "p2", "cantidad": 1, "precio_usd": 3.0, "precio_bs": 400.0})(),
        ]
    )
    assert intento.cart_fingerprint == esperado


def test_intento_sobrevive_aunque_falle_la_nota_del_crm():
    # La mejora clave: aunque put_ficha reviente, el pedido queda registrado en NEA.
    crm = _CrmFalla()
    res, store, conv, rt = asyncio.run(_finalizar(crm))
    assert res["ok"] is True                   # el turno no se cae (best-effort)
    assert len(store.order_attempts) == 1      # evidencia durable del pedido
    assert conv.cart_closed is True            # y el pedido igual se cierra


def test_carrito_vacio_no_registra_intento_ni_cierra():
    store = MemoryStore()
    conv = asyncio.run(store.get_or_create_conversation("wa-vacio"))
    rt = ToolRuntime(_Ctx(store, _CrmOk()), conv, "crm-conv-2", provider_id="demo")
    res = asyncio.run(rt._finalizar_pedido())
    assert res["ok"] is False and res["error"] == "carrito_vacio"
    assert store.order_attempts == {}
    assert conv.cart_closed is False
