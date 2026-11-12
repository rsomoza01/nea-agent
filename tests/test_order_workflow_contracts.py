"""Pruebas del contrato del flujo de pedido (Paso 7 — porción no bloqueada).

Cubren SOLO la parte diseñable y probable sin CRM ni DB (ADR-0007 decisiones 1-4,
escenarios A/B/C). La parte transaccional (persistencia, reserva atómica, revalidación
de precio/stock, los 7 casos de integración) está BLOCKED por Docker/CRM caídos y por
las preguntas abiertas 1/2/8 del PLAN; ver PR-feat-pharmacy-order-workflow.md.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.application.order_workflow import (
    LIMITACION_SIN_ENTIDAD_PEDIDO,
    ConfirmarPedidoUseCase,
    EnMemoriaOrderAttemptStore,
    OrderAttempt,
    OrderAttemptStore,
    OrderConfirmResult,
    OrderConfirmStatus,
    OrderPhase,
    cart_fingerprint,
    nuevo_order_attempt_id,
    resultado_requiere_confirmacion,
    transicion_permitida,
)


@dataclass
class _Item:
    """Doble de `CartItem` con los campos que usa el fingerprint."""

    product_id: str
    cantidad: int
    precio_usd: float | None = None
    precio_bs: float | None = None


# --------------------------------------------------------------- order_attempt_id ---

def test_attempt_id_es_unico_por_intento():
    # Escenario C: dos intentos lógicos distintos → ids distintos.
    assert nuevo_order_attempt_id() != nuevo_order_attempt_id()


def test_intento_nuevo_con_carrito_identico_es_otro_pedido():
    # Escenario C: mismo carrito, confirmación intencional distinta → NUEVO id.
    items = [_Item("SKU1", 2, 10.0, 70.0)]
    a1 = OrderAttempt.nuevo(items)
    a2 = OrderAttempt.nuevo(items)
    assert a1.cart_fingerprint == a2.cart_fingerprint  # mismo carrito
    assert a1.order_attempt_id != a2.order_attempt_id   # pero pedidos distintos
    assert not a1.es_reintento_de(a2)


def test_reintento_del_mismo_intento_reutiliza_id():
    # Escenario A: reintento por timeout/doble-webhook comparte id → mismo intento.
    items = [_Item("SKU1", 2, 10.0, 70.0)]
    original = OrderAttempt.nuevo(items)
    # Un reintento reutiliza el id persistido, no genera uno nuevo.
    reintento = OrderAttempt(
        order_attempt_id=original.order_attempt_id,
        cart_fingerprint=original.cart_fingerprint,
    )
    assert reintento.es_reintento_de(original)
    assert original.es_reintento_de(reintento)


def test_fingerprint_no_es_la_clave_de_unicidad():
    # El fingerprint NO identifica el pedido: mismo fingerprint, ids distintos.
    items = [_Item("SKU1", 1, 5.0, 35.0)]
    a1 = OrderAttempt.nuevo(items)
    a2 = OrderAttempt.nuevo(items)
    assert a1.cart_fingerprint == a2.cart_fingerprint
    assert a1 != a2  # dataclass frozen: difieren por order_attempt_id


# ------------------------------------------------------------------- fingerprint ---

def test_fingerprint_estable_independiente_del_orden():
    i1 = [_Item("B", 1, 2.0, 14.0), _Item("A", 3, 1.0, 7.0)]
    i2 = [_Item("A", 3, 1.0, 7.0), _Item("B", 1, 2.0, 14.0)]
    assert cart_fingerprint(i1) == cart_fingerprint(i2)


def test_fingerprint_cambia_con_la_cantidad():
    base = [_Item("A", 1, 1.0, 7.0)]
    mas = [_Item("A", 2, 1.0, 7.0)]
    assert cart_fingerprint(base) != cart_fingerprint(mas)


def test_fingerprint_cambia_con_el_precio():
    base = [_Item("A", 1, 1.0, 7.0)]
    reprecio = [_Item("A", 1, 1.5, 10.0)]
    assert cart_fingerprint(base) != cart_fingerprint(reprecio)


def test_fingerprint_carrito_vacio_es_cadena_vacia():
    assert cart_fingerprint([]) == ""


def test_fingerprint_tolera_bs_ausente():
    # Bs puede faltar (no se inventa 0); el fingerprint no debe reventar.
    a = cart_fingerprint([_Item("A", 1, 1.0, None)])
    b = cart_fingerprint([_Item("A", 1, 1.0, None)])
    assert a == b
    assert "None" in a  # el ausente se refleja, no se confunde con 0


# ------------------------------------------------------------------- transiciones ---

def test_transiciones_permitidas():
    assert transicion_permitida(OrderPhase.CARRITO_LOCAL, OrderPhase.PEDIDO_PROVISIONAL)
    assert transicion_permitida(
        OrderPhase.PEDIDO_PROVISIONAL, OrderPhase.CONFIRMADO_POR_CLIENTE
    )
    # Volver atrás a seguir agregando desde el resumen (FLOW-ORDERS §1).
    assert transicion_permitida(OrderPhase.PEDIDO_PROVISIONAL, OrderPhase.CARRITO_LOCAL)


def test_no_se_salta_la_confirmacion():
    # Decisión 2: de carrito NO se pasa directo a confirmado sin resumen/confirmación.
    assert not transicion_permitida(
        OrderPhase.CARRITO_LOCAL, OrderPhase.CONFIRMADO_POR_CLIENTE
    )


def test_confirmado_es_terminal_en_el_grafo_local():
    # No se "des-confirma" en silencio; un pedido nuevo exige otro intento.
    assert not transicion_permitida(
        OrderPhase.CONFIRMADO_POR_CLIENTE, OrderPhase.CARRITO_LOCAL
    )
    assert not transicion_permitida(
        OrderPhase.CONFIRMADO_POR_CLIENTE, OrderPhase.PEDIDO_PROVISIONAL
    )


# ----------------------------------------------------------------------- resultados ---

def test_requiere_confirmacion_con_items():
    r = resultado_requiere_confirmacion(items_no_vacio=True)
    assert r.status is OrderConfirmStatus.REQUIERE_CONFIRMACION


def test_carrito_vacio_no_confirma():
    r = resultado_requiere_confirmacion(items_no_vacio=False)
    assert r.status is OrderConfirmStatus.CARRITO_VACIO


def test_resultado_pendiente_fuente_expone_limitacion_g13():
    # ADR-0007 decisión 3: sin entidad de pedido en el CRM, NEA no afirma aceptación.
    r = OrderConfirmResult(
        status=OrderConfirmStatus.PENDIENTE_FUENTE,
        order_attempt_id=nuevo_order_attempt_id(),
        limitaciones=(LIMITACION_SIN_ENTIDAD_PEDIDO,),
    )
    assert r.status is OrderConfirmStatus.PENDIENTE_FUENTE
    assert LIMITACION_SIN_ENTIDAD_PEDIDO in r.limitaciones


# ------------------------------------------------- reserva atómica del intento (D) ---

def test_store_de_referencia_satisface_el_puerto():
    assert isinstance(EnMemoriaOrderAttemptStore(), OrderAttemptStore)


def test_reserva_repetida_mismo_id_no_crea_otro():
    # Escenario A/B: reintento/doble-webhook con MISMO id → mismo intento, sin duplicar.
    store = EnMemoriaOrderAttemptStore()
    oid = nuevo_order_attempt_id()

    async def run():
        a = await store.reservar_intento(
            order_attempt_id=oid, conversation_id=1, cart_fingerprint="A:1:1.0:7.0"
        )
        # Segunda reserva del mismo id, incluso con otra huella: NO sobrescribe.
        b = await store.reservar_intento(
            order_attempt_id=oid, conversation_id=1, cart_fingerprint="OTRA"
        )
        return a, b

    a, b = asyncio.run(run())
    assert a.order_attempt_id == b.order_attempt_id
    assert b.cart_fingerprint == "A:1:1.0:7.0"  # queda la huella de la PRIMERA reserva


def test_reserva_concurrente_mismo_id_converge():
    # Escenario B: dos solicitudes concurrentes con el mismo id → UN intento.
    store = EnMemoriaOrderAttemptStore()
    oid = nuevo_order_attempt_id()

    async def run():
        res = await asyncio.gather(
            store.reservar_intento(
                order_attempt_id=oid, conversation_id=1, cart_fingerprint="X"
            ),
            store.reservar_intento(
                order_attempt_id=oid, conversation_id=1, cart_fingerprint="Y"
            ),
        )
        return res

    r1, r2 = asyncio.run(run())
    assert r1.order_attempt_id == r2.order_attempt_id
    assert r1.cart_fingerprint == r2.cart_fingerprint  # convergen en una sola huella


def test_ids_distintos_crean_intentos_distintos():
    # Escenario C: confirmación intencional distinta (otro id) → otro intento.
    store = EnMemoriaOrderAttemptStore()

    async def run():
        a = await store.reservar_intento(
            order_attempt_id=nuevo_order_attempt_id(), conversation_id=1,
            cart_fingerprint="A:1:1.0:7.0",
        )
        b = await store.reservar_intento(
            order_attempt_id=nuevo_order_attempt_id(), conversation_id=1,
            cart_fingerprint="A:1:1.0:7.0",  # mismo carrito
        )
        return a, b

    a, b = asyncio.run(run())
    assert a.order_attempt_id != b.order_attempt_id


# --------------------------------------------------------- ConfirmarPedidoUseCase ---

def _uc_result(items, *, confirmado, oid, store=None):
    store = store or EnMemoriaOrderAttemptStore()
    uc = ConfirmarPedidoUseCase(store)
    return asyncio.run(
        uc.ejecutar(
            conversation_id=1,
            items=items,
            order_attempt_id=oid,
            confirmacion_explicita=confirmado,
        )
    ), store


def test_uc_carrito_vacio():
    r, _ = _uc_result([], confirmado=True, oid=nuevo_order_attempt_id())
    assert r.status is OrderConfirmStatus.CARRITO_VACIO


def test_uc_exige_confirmacion_explicita():
    r, _ = _uc_result(
        [_Item("A", 1, 1.0, 7.0)], confirmado=False, oid=nuevo_order_attempt_id()
    )
    assert r.status is OrderConfirmStatus.REQUIERE_CONFIRMACION


def test_uc_confirmado_reserva_y_no_afirma_aceptacion():
    oid = nuevo_order_attempt_id()
    r, store = _uc_result([_Item("A", 1, 1.0, 7.0)], confirmado=True, oid=oid)
    assert r.status is OrderConfirmStatus.PENDIENTE_FUENTE
    assert r.order_attempt_id == oid
    assert LIMITACION_SIN_ENTIDAD_PEDIDO in r.limitaciones
    assert asyncio.run(store.obtener_intento(oid)) is not None


def test_uc_es_idempotente_por_order_attempt_id():
    # Doble confirmación del MISMO intento lógico → un solo intento reservado.
    oid = nuevo_order_attempt_id()
    store = EnMemoriaOrderAttemptStore()
    items = [_Item("A", 1, 1.0, 7.0)]
    r1, _ = _uc_result(items, confirmado=True, oid=oid, store=store)
    r2, _ = _uc_result(items, confirmado=True, oid=oid, store=store)
    assert r1.order_attempt_id == r2.order_attempt_id == oid
