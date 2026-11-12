"""Contratos del flujo transaccional de pedido (UC-Pedido, Paso 7 — porción no bloqueada).

Formaliza, de forma **aditiva y paralela** (igual que UC-01 en el Paso 4), SOLO la
parte de [ADR-0007](../../docs/architecture/ADR/ADR-0007-fuente-verdad-pedido.md) que
es **diseñable y probable sin tocar el CRM ni el esquema de la DB**:

  1. Separar `preparar pedido` de `confirmar pedido` como fases explícitas (decisión 1).
  2. Confirmación explícita obligatoria antes de registrar algo irreversible (decisión 2).
  3. El estado comunicado al cliente debe proceder del sistema fuente; mientras no exista
     entidad de pedido en el CRM, NEA **no** afirma estados que no puede demostrar
     (decisión 3, sostenida por G13).
  4. Identificador único por **intento lógico** de confirmación (`order_attempt_id`):
     un id por intento, reutilizado en reintentos/doble-webhook del **mismo** intento,
     nuevo id por una confirmación intencional distinta; el **fingerprint del carrito NO
     es la clave de unicidad** (decisión 4, escenarios A/B/C).

**Lo que este módulo NO hace** (BLOCKED por entorno y por preguntas abiertas 1/2/8 del
PLAN; ver `docs/architecture/PR-feat-pharmacy-order-workflow.md`):
  - No crea una entidad de pedido persistida ni migración de esquema (el CRM no expone
    API de pedidos; `finalizar_pedido` sigue siendo una nota de ficha best-effort).
  - No implementa la reserva atómica / correlación de concurrencia (ADR-0007 parte D).
  - No revalida precio/stock contra el CRM al confirmar.
  - NO está cableado en `turn.py`/`tools.py`: es aditivo y paralelo, sin cambiar el
    comportamiento observable del bot. El cableado exige la arquitectura transaccional
    completa, que está bloqueada.

Invariantes (ADR-0002/ADR-0007): la aplicación no autoriza, no llama al LLM y no afirma
un registro que no pueda demostrar. La máquina de estados **extendida** del pedido
(estados del CRM) NO se diseña aquí contra supuestos.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Protocol, runtime_checkable


class OrderPhase(str, Enum):
    """Fases del pedido que NEA sí puede demostrar hoy (ADR-0007 decisión 1/3).

    Son las fases cuyo estado NEA controla localmente (carrito en `bot_cart`, resumen y
    confirmación del cliente). La aceptación por la farmacia NO es una fase afirmable
    hoy: no hay entidad de pedido en el CRM que la respalde, así que se expone como
    `PENDIENTE_FUENTE` en el resultado, nunca como un estado confirmado.
    """

    CARRITO_LOCAL = "carrito_local"          # hay ítems; el cliente aún agrega/ajusta
    PEDIDO_PROVISIONAL = "pedido_provisional"  # resumen mostrado; falta confirmar
    CONFIRMADO_POR_CLIENTE = "confirmado_por_cliente"  # el cliente dijo "SI" explícito


# Transiciones permitidas entre fases locales (decisión 1/2: no se salta la
# confirmación; se puede volver atrás a agregar). Se modela como grafo explícito para
# que una fase nueva no herede permisos por accidente.
_TRANSICIONES: dict[OrderPhase, frozenset[OrderPhase]] = {
    OrderPhase.CARRITO_LOCAL: frozenset(
        {OrderPhase.CARRITO_LOCAL, OrderPhase.PEDIDO_PROVISIONAL}
    ),
    OrderPhase.PEDIDO_PROVISIONAL: frozenset(
        {OrderPhase.CARRITO_LOCAL, OrderPhase.CONFIRMADO_POR_CLIENTE}
    ),
    # Una vez confirmado por el cliente, volver a carrito exige un pedido nuevo
    # (no se "des-confirma" en silencio). Es estado terminal del grafo local.
    OrderPhase.CONFIRMADO_POR_CLIENTE: frozenset(),
}


def transicion_permitida(origen: OrderPhase, destino: OrderPhase) -> bool:
    """¿Se permite pasar de `origen` a `destino`? (función pura, grafo explícito)."""
    return destino in _TRANSICIONES.get(origen, frozenset())


class OrderConfirmStatus(str, Enum):
    """Resultados tipados de un intento de confirmación de pedido.

    `PENDIENTE_FUENTE` es la clave de ADR-0007 decisión 3: cuando no existe entidad de
    pedido en el sistema fuente, NEA registra el intento (nota de ficha) pero NO puede
    afirmar que la farmacia lo aceptó; el estado real queda pendiente de la fuente.
    """

    REQUIERE_CONFIRMACION = "requiere_confirmacion"   # aún no hay "SI" explícito
    CARRITO_VACIO = "carrito_vacio"                   # nada que confirmar
    PENDIENTE_FUENTE = "pendiente_fuente"             # registrado; falta aceptación real
    PROVEEDOR_NO_DISPONIBLE = "proveedor_no_disponible"  # el CRM no respondió
    RECHAZADO = "rechazado"                            # el cliente rechazó/canceló


def nuevo_order_attempt_id() -> str:
    """Genera un `order_attempt_id` único para un **intento lógico** nuevo (UUID4).

    ADR-0007 decisión 4 / escenario C: una confirmación intencional distinta genera un
    id nuevo **aunque** el carrito sea idéntico. Este helper NO deduplica: la reutilización
    del id en reintentos del mismo intento (escenario A) es responsabilidad del
    *persistir-antes-de-llamar* (ver `OrderAttempt.es_reintento_de`), y la coordinación
    ante concurrencia (escenario B/parte D) queda BLOCKED para el diseño transaccional.
    """
    return str(uuid.uuid4())


def cart_fingerprint(items: Iterable[Any]) -> str:
    """Huella estable del contenido del carrito (dato de **contexto**, NO clave de unicidad).

    ADR-0007 decisión 4 / escenario C: el hash del carrito sirve para detectar si el
    contenido cambió entre preparar y confirmar (revalidación), pero **nunca** identifica
    el pedido — dos confirmaciones legítimas del mismo carrito son pedidos distintos.

    Es puro y determinista: ordena por `product_id` para no depender del orden de lectura
    y usa (product_id, cantidad, precio_usd, precio_bs) — la evidencia que respalda el
    total. No incluye timestamps ni ids de fila (ruido no semántico).
    """
    filas = []
    for it in items:
        filas.append(
            (
                str(getattr(it, "product_id", "")),
                int(getattr(it, "cantidad", 0) or 0),
                getattr(it, "precio_usd", None),
                getattr(it, "precio_bs", None),
            )
        )
    filas.sort(key=lambda f: f[0])
    return "|".join(f"{pid}:{qty}:{usd}:{bs}" for pid, qty, usd, bs in filas)


@dataclass(frozen=True)
class OrderAttempt:
    """Un intento lógico de confirmación de pedido (ADR-0007 decisión 4).

    Liga un `order_attempt_id` único a la huella del carrito en el momento del intento.
    La huella es contexto para reconciliar/revalidar, NO la clave. La **persistencia**
    del intento antes de llamar al sistema fuente (y la reserva atómica ante concurrencia)
    es la parte BLOCKED/PENDIENTE del Paso 7.
    """

    order_attempt_id: str
    cart_fingerprint: str
    phase: OrderPhase = OrderPhase.PEDIDO_PROVISIONAL

    @staticmethod
    def nuevo(items: Iterable[Any]) -> "OrderAttempt":
        """Crea un intento nuevo (id único) para el carrito dado."""
        return OrderAttempt(
            order_attempt_id=nuevo_order_attempt_id(),
            cart_fingerprint=cart_fingerprint(items),
        )

    def es_reintento_de(self, otro: "OrderAttempt") -> bool:
        """¿Este intento es el MISMO intento lógico que `otro`? (comparte id).

        Escenario A: reintentos por timeout/doble-webhook/reenvío comparten id → es el
        mismo intento, NO uno nuevo. La igualdad de huella de carrito NO basta ni es
        necesaria (escenario C: mismo carrito puede ser otro pedido).
        """
        return self.order_attempt_id == otro.order_attempt_id


@dataclass(frozen=True)
class OrderConfirmResult:
    """Resultado tipado de un intento de confirmación (paralelo a `CatalogSearchResult`)."""

    status: OrderConfirmStatus
    order_attempt_id: str | None = None
    # Qué quedó pendiente de la fuente / qué NO se puede afirmar (sostén de G13).
    limitaciones: tuple[str, ...] = ()
    detalle: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# Limitación estándar: no hay entidad de pedido en el CRM, el estado final lo tiene el
# humano/farmacia, NEA no lo afirma (G13 / ADR-0007 decisión 3).
LIMITACION_SIN_ENTIDAD_PEDIDO = "pedido_no_confirmado_por_fuente"


def resultado_requiere_confirmacion(items_no_vacio: bool) -> OrderConfirmResult:
    """Pre-confirmación: aún no hay "SI" explícito (decisión 2).

    Si el carrito está vacío no hay nada que confirmar (coincide con el guard
    `carrito_vacio` de `tools._finalizar_pedido`)."""
    if not items_no_vacio:
        return OrderConfirmResult(status=OrderConfirmStatus.CARRITO_VACIO)
    return OrderConfirmResult(status=OrderConfirmStatus.REQUIERE_CONFIRMACION)


# --------------------------------------------------------- reserva atómica (parte D) ---
# Con la decisión del propietario "persistir el pedido en NEA" (ADR-0007, revisión
# 2026-11-12), la parte D (reserva atómica/correlación de concurrencia) deja de estar
# bloqueada por falta de fuente. El mecanismo es el MISMO idioma ya probado en
# `Store.mark_processed` (`db.py:99`): `INSERT … ON CONFLICT (order_attempt_id) DO
# NOTHING RETURNING`, que es atómico e idempotente por id. El puerto describe esa
# capacidad al nivel de almacenamiento, sin acoplar la aplicación a Postgres.


@runtime_checkable
class OrderAttemptStore(Protocol):
    """Capacidad de **reservar atómicamente** un intento lógico de pedido.

    Contrato de `reservar_intento` (idempotente por `order_attempt_id`):
      - Primera reserva de un id: persiste y devuelve el intento nuevo.
      - Reserva repetida del MISMO id (reintento, doble webhook, doble tap que comparten
        id): devuelve el intento YA guardado **sin crear otro y sin sobrescribir** su
        huella (escenarios A/B de ADR-0007). El `cart_fingerprint` de la primera reserva
        es el que queda (base para revalidar si el carrito cambió después).
      - Dos solicitudes concurrentes con el mismo id convergen en UN intento (atómico).
    Un `order_attempt_id` nuevo (confirmación intencional distinta) crea otro intento
    aunque el carrito sea idéntico (escenario C): esa unicidad la decide quien genera el
    id, no el store.
    """

    async def reservar_intento(
        self, *, order_attempt_id: str, conversation_id: int, cart_fingerprint: str
    ) -> OrderAttempt:
        ...

    async def obtener_intento(self, order_attempt_id: str) -> OrderAttempt | None:
        ...


class EnMemoriaOrderAttemptStore:
    """Implementación de referencia en memoria del `OrderAttemptStore`.

    Especifica la semántica exacta que deberá replicar el método de `PgStore` con
    `INSERT … ON CONFLICT DO NOTHING RETURNING` (ver DDL propuesta en
    `PR-feat-pharmacy-order-workflow.md`). Sirve para probar los escenarios A/B/C sin DB.
    El `asyncio.Lock` emula la atomicidad del upsert ante corrutinas concurrentes.
    """

    def __init__(self) -> None:
        self._intentos: dict[str, OrderAttempt] = {}
        self._lock = asyncio.Lock()

    async def reservar_intento(
        self, *, order_attempt_id: str, conversation_id: int, cart_fingerprint: str
    ) -> OrderAttempt:
        async with self._lock:
            existente = self._intentos.get(order_attempt_id)
            if existente is not None:
                # ON CONFLICT DO NOTHING: no se crea otro ni se sobrescribe la huella.
                return existente
            intento = OrderAttempt(
                order_attempt_id=order_attempt_id,
                cart_fingerprint=cart_fingerprint,
                phase=OrderPhase.CONFIRMADO_POR_CLIENTE,
            )
            self._intentos[order_attempt_id] = intento
            return intento

    async def obtener_intento(self, order_attempt_id: str) -> OrderAttempt | None:
        return self._intentos.get(order_attempt_id)


class ConfirmarPedidoUseCase:
    """UC-Pedido: confirma un intento de pedido de forma idempotente (porción no bloqueada).

    Hace cumplir, sin tocar el CRM: (1) carrito no vacío, (2) confirmación explícita
    obligatoria antes de reservar, (3) reserva atómica del intento por `order_attempt_id`
    estable, (4) NO afirmar aceptación por la farmacia mientras no exista entidad de
    pedido en la fuente → `PENDIENTE_FUENTE` + limitación G13.

    `order_attempt_id` debe ser **estable por intento lógico**: el llamador lo persiste al
    detectar la confirmación y lo reutiliza en reintentos/doble-webhook; genera uno nuevo
    solo ante una confirmación intencional distinta (escenario C). Este caso de uso NO
    llama al LLM, NO autoriza y NO está cableado en la ruta caliente.
    """

    def __init__(self, store: OrderAttemptStore) -> None:
        self._store = store

    async def ejecutar(
        self,
        *,
        conversation_id: int,
        items: Iterable[Any],
        order_attempt_id: str,
        confirmacion_explicita: bool,
    ) -> OrderConfirmResult:
        items = list(items)
        if not items:
            return OrderConfirmResult(status=OrderConfirmStatus.CARRITO_VACIO)
        if not confirmacion_explicita:
            return OrderConfirmResult(status=OrderConfirmStatus.REQUIERE_CONFIRMACION)
        intento = await self._store.reservar_intento(
            order_attempt_id=order_attempt_id,
            conversation_id=conversation_id,
            cart_fingerprint=cart_fingerprint(items),
        )
        return OrderConfirmResult(
            status=OrderConfirmStatus.PENDIENTE_FUENTE,
            order_attempt_id=intento.order_attempt_id,
            limitaciones=(LIMITACION_SIN_ENTIDAD_PEDIDO,),
        )
