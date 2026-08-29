"""Estado propio del bot: modelos, contrato de almacenamiento y fake en memoria.

El acceso a datos está abstraído en el protocolo `Store` para que los unit
tests corran con `MemoryStore` (sin Postgres). La implementación real con
asyncpg vive en `app/db.py` (`PgStore`).
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.config import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- modelos ---


@dataclass
class Conversation:
    id: int
    wa_identity: str
    crm_conversation_id: str | None = None
    phase: str = "descubrimiento"  # descubrimiento|insight|salida|agendando|cerrada
    greeted: bool = False
    media_notice_sent: bool = False
    followup_due_at: datetime | None = None
    followup_sent: bool = False
    last_inbound_at: datetime | None = None
    # Último producto consultado (dict JSON). Lo persiste el backstop de carrito
    # para saber qué añadir cuando el cliente responde una cantidad en un turno
    # nuevo (el ToolRuntime no re-consulta el catálogo).
    last_product: dict[str, Any] | None = None
    # Último término consultado con buscar_medicamento. Permite re-consultar el
    # catálogo cuando el cliente refina con miligramo/marca sin repetir el nombre.
    last_term: str | None = None
    # Lista de opciones (productos ordenados por precio) del último
    # buscar_medicamento — para resolver "quiero X cajas de la opción Z".
    last_options: list[dict[str, Any]] | None = None
    # Puesta cuando el agente cierra por conversación sin rumbo: mientras
    # viva, el turno guarda silencio (ver app/stall.py).
    stalled_at: datetime | None = None


@dataclass
class BotMessage:
    id: int
    conversation_id: int
    role: str  # user | assistant
    content: str
    wa_message_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class OfferedSlot:
    conversation_id: int
    start_utc: datetime
    end_utc: datetime | None
    label: str
    offered_at: datetime = field(default_factory=utcnow)


@dataclass
class RelayItem:
    id: int
    body: bytes
    signature: str | None
    attempts: int
    created_at: datetime
    next_retry_at: datetime
    delivered_at: datetime | None = None
    abandoned_at: datetime | None = None


@dataclass
class PendingSend:
    """Respuesta generada cuyo envío al CRM agotó los reintentos del turno.
    Se persiste para reintento diferido — jamás se descarta (incidente 2026-08-03)."""

    id: int
    conversation_id: int
    crm_conversation_id: str
    content: str
    attempts: int
    created_at: datetime
    next_retry_at: datetime
    delivered_at: datetime | None = None
    abandoned_at: datetime | None = None


@dataclass
class CartItem:
    """Ítem del carrito de compra del rol farmacéutico (spec 001, FR-8)."""

    id: int
    conversation_id: int
    product_id: str
    producto: str
    presentacion: str
    laboratorio: str
    cantidad: int
    precio_usd: float | None
    precio_bs: float | None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass
class InboundMessage:
    """Mensaje entrante ya parseado del webhook de Meta."""

    wa_message_id: str | None
    identity: str
    type: str
    text: str | None = None
    referral_headline: str | None = None
    profile_name: str | None = None
    # conversationId de la org correcta (multi-tenant): lo inyecta el puente
    # del CRM para que Nea responda en el hilo correcto, no por identidad
    # (ambigua cuando el mismo número existe en varias farmacias).
    conversation_id: str | None = None
    # Multimedia (spec 002): lo mínimo para procesar el contenido.
    media_id: str | None = None
    media_mime: str | None = None
    media_filename: str | None = None
    media_caption: str | None = None
    media_voice: bool = False
    # Imagen ya descargada (base64) — la inyecta el puente del CRM cuando el
    # canal es Evolution (que no expone mediaId de Meta). Si viene, media.py
    # la usa directamente en vez de llamar a /api/bot/media/{mediaId}.
    image_base64: str | None = None
    image_mime: str | None = None
    # Audio ya descargado (base64) — canal Evolution (el CRM lo inyecta).
    audio_base64: str | None = None
    audio_mime: str | None = None
    location: dict[str, Any] | None = None
    contact_names: list[str] = field(default_factory=list)


# --------------------------------------------------------------- contrato ---


class Store(Protocol):
    """Contrato de persistencia del bot (Postgres real o memoria en tests)."""

    # dedup
    async def mark_processed(self, wa_message_id: str) -> bool:
        """True si el mensaje es nuevo (gana el INSERT); False si ya se procesó."""
        ...

    # cola de relay
    async def enqueue_relay(self, body: bytes, signature: str | None) -> int: ...
    async def due_relays(self, now: datetime) -> list[RelayItem]: ...
    async def mark_relay_delivered(self, relay_id: int) -> None: ...
    async def mark_relay_abandoned(self, relay_id: int) -> None: ...
    async def reschedule_relay(
        self, relay_id: int, attempts: int, next_retry_at: datetime
    ) -> None: ...

    # conversaciones
    async def get_or_create_conversation(self, wa_identity: str) -> Conversation: ...
    async def update_conversation(self, conversation_id: int, **fields: Any) -> None: ...
    async def reset_conversation(self, conversation_id: int) -> None:
        """Borra historial + slots y regresa la conversación a estado inicial
        (comando /reset de la línea de pruebas)."""
        ...

    # historial LLM
    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None: ...
    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]: ...

    # slots ofrecidos
    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None: ...
    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]: ...
    async def clear_offered_slots(self, conversation_id: int) -> None: ...

    # cola de envíos pendientes (respuestas que no pudieron salir en el turno)
    async def enqueue_pending_send(
        self, conversation_id: int, crm_conversation_id: str, content: str
    ) -> int: ...
    async def due_pending_sends(self, now: datetime) -> list[PendingSend]: ...
    async def mark_pending_send_delivered(self, pending_id: int) -> None: ...
    async def mark_pending_send_abandoned(self, pending_id: int) -> None: ...
    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None: ...

    # carrito de compra (spec 001, FR-8)
    async def cart_add(
        self,
        conversation_id: int,
        product_id: str,
        producto: str,
        presentacion: str,
        laboratorio: str,
        cantidad: int,
        precio_usd: float | None,
        precio_bs: float | None,
    ) -> CartItem:
        """Añade o incrementa un ítem al carrito de la conversación."""
        ...
    async def cart_items(
        self, conversation_id: int, session_hours: float | None = None
    ) -> list[CartItem]:
        """Ítems del carrito activos.

        Si session_hours se pasa, SOLO devuelve los ítems tocados dentro de la
        ventana (updated_at >= now - session_hours): el carrito no acumula
        medicamentos de sesiones anteriores del mismo chat.
        """
    async def cart_clear(self, conversation_id: int) -> None: ...
    async def cart_set(
        self,
        conversation_id: int,
        product_id: str,
        cantidad: int,
    ) -> CartItem | None: ...

    # analytics (Fase 1: dashboard de medicamentos más buscados)
    async def log_med_query(
        self,
        conversation_id: int,
        provider_id: str,
        term: str,
        product_id: str | None,
        product_name: str | None,
        result_count: int,
        added_to_cart: bool = False,
    ) -> None: ...
    async def top_med_queries(
        self,
        provider_id: str,
        desde: str | None = None,
        hasta: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    # pacientes crónicos (Fase 2: clasificación automática)
    async def condiciones_para_termino(self, term: str) -> list[str]: ...
    async def registrar_consulta_cronica(
        self,
        provider_id: str,
        wa_identity: str,
        condiciones: list[str],
    ) -> None: ...
    async def chronic_patients(
        self,
        provider_id: str,
        solo_consentidos: bool = False,
    ) -> list[dict[str, Any]]: ...
    async def marcar_consent(
        self, provider_id: str, wa_identity: str, consent: bool = True
    ) -> None: ...

    # seguimiento
    async def due_followups(self, now: datetime) -> list[Conversation]: ...
    async def claim_followup(self, conversation_id: int) -> bool:
        """Marca followup_sent=True atómicamente. True si ESTA llamada lo ganó."""
        ...

    async def ping(self) -> None: ...
    async def aclose(self) -> None: ...


# ------------------------------------------------------- fake en memoria ---


class MemoryStore:
    """Implementación en memoria del Store — solo para tests."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)
        self.processed: set[str] = set()
        self.relays: dict[int, RelayItem] = {}
        self.conversations: dict[int, Conversation] = {}
        self._conv_by_identity: dict[str, int] = {}
        self.messages: list[BotMessage] = []
        self.offered: dict[int, list[OfferedSlot]] = {}
        self.pending_sends: dict[int, PendingSend] = {}
        self.cart: dict[int, list[CartItem]] = {}
        self.med_queries: list[dict[str, Any]] = []
        self.patient_profiles: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}  # (provider_id, wa_identity, condicion) → perfil

    async def mark_processed(self, wa_message_id: str) -> bool:
        if wa_message_id in self.processed:
            return False
        self.processed.add(wa_message_id)
        return True

    async def enqueue_relay(self, body: bytes, signature: str | None) -> int:
        rid = next(self._ids)
        now = utcnow()
        self.relays[rid] = RelayItem(
            id=rid, body=body, signature=signature, attempts=0,
            created_at=now, next_retry_at=now,
        )
        return rid

    async def due_relays(self, now: datetime) -> list[RelayItem]:
        return [
            r for r in sorted(self.relays.values(), key=lambda r: r.id)
            if r.delivered_at is None and r.abandoned_at is None
            and r.next_retry_at <= now
        ]

    async def mark_relay_delivered(self, relay_id: int) -> None:
        self.relays[relay_id].delivered_at = utcnow()

    async def mark_relay_abandoned(self, relay_id: int) -> None:
        self.relays[relay_id].abandoned_at = utcnow()

    async def reschedule_relay(
        self, relay_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        item = self.relays[relay_id]
        item.attempts = attempts
        item.next_retry_at = next_retry_at

    async def get_or_create_conversation(self, wa_identity: str) -> Conversation:
        cid = self._conv_by_identity.get(wa_identity)
        if cid is not None:
            return self.conversations[cid]
        cid = next(self._ids)
        conv = Conversation(id=cid, wa_identity=wa_identity)
        self.conversations[cid] = conv
        self._conv_by_identity[wa_identity] = cid
        return conv

    async def update_conversation(self, conversation_id: int, **fields: Any) -> None:
        conv = self.conversations[conversation_id]
        for key, value in fields.items():
            setattr(conv, key, value)

    async def reset_conversation(self, conversation_id: int) -> None:
        self.messages = [
            m for m in self.messages if m.conversation_id != conversation_id
        ]
        self.offered.pop(conversation_id, None)
        conv = self.conversations[conversation_id]
        conv.phase = "descubrimiento"
        conv.greeted = False
        conv.media_notice_sent = False
        conv.followup_due_at = None
        conv.followup_sent = False
        conv.stalled_at = None

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None:
        self.messages.append(
            BotMessage(
                id=next(self._ids),
                conversation_id=conversation_id,
                role=role,
                content=content,
                wa_message_id=wa_message_id,
            )
        )

    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]:
        msgs = [m for m in self.messages if m.conversation_id == conversation_id]
        return msgs[-limit:]

    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None:
        self.offered[conversation_id] = list(slots)

    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]:
        return list(self.offered.get(conversation_id, []))

    async def clear_offered_slots(self, conversation_id: int) -> None:
        self.offered.pop(conversation_id, None)

    async def enqueue_pending_send(
        self, conversation_id: int, crm_conversation_id: str, content: str
    ) -> int:
        pid = next(self._ids)
        now = utcnow()
        self.pending_sends[pid] = PendingSend(
            id=pid, conversation_id=conversation_id,
            crm_conversation_id=crm_conversation_id, content=content,
            attempts=0, created_at=now, next_retry_at=now,
        )
        return pid

    async def due_pending_sends(self, now: datetime) -> list[PendingSend]:
        return [
            p for p in sorted(self.pending_sends.values(), key=lambda p: p.id)
            if p.delivered_at is None and p.abandoned_at is None
            and p.next_retry_at <= now
        ]

    async def mark_pending_send_delivered(self, pending_id: int) -> None:
        self.pending_sends[pending_id].delivered_at = utcnow()

    async def mark_pending_send_abandoned(self, pending_id: int) -> None:
        self.pending_sends[pending_id].abandoned_at = utcnow()

    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        item = self.pending_sends[pending_id]
        item.attempts = attempts
        item.next_retry_at = next_retry_at

    # ------------------------------------------------------- carrito (FR-8) ---

    async def cart_add(
        self,
        conversation_id: int,
        product_id: str,
        producto: str,
        presentacion: str,
        laboratorio: str,
        cantidad: int,
        precio_usd: float | None,
        precio_bs: float | None,
    ) -> CartItem:
        items = self.cart.setdefault(conversation_id, [])
        existing = next((i for i in items if i.product_id == product_id), None)
        if existing is not None:
            existing.cantidad += cantidad
            existing.updated_at = utcnow()
            return existing
        item = CartItem(
            id=next(self._ids),
            conversation_id=conversation_id,
            product_id=product_id,
            producto=producto,
            presentacion=presentacion,
            laboratorio=laboratorio,
            cantidad=cantidad,
            precio_usd=precio_usd,
            precio_bs=precio_bs,
        )
        items.append(item)
        return item

    async def cart_items(
        self, conversation_id: int, session_hours: float | None = None
    ) -> list[CartItem]:
        items = list(self.cart.get(conversation_id, []))
        if session_hours is not None:
            cutoff = utcnow() - timedelta(hours=session_hours)
            items = [i for i in items if i.updated_at >= cutoff]
        return items

    async def cart_clear(self, conversation_id: int) -> None:
        self.cart.pop(conversation_id, None)

    async def log_med_query(
        self,
        conversation_id: int,
        provider_id: str,
        term: str,
        product_id: str | None,
        product_name: str | None,
        result_count: int,
        added_to_cart: bool = False,
    ) -> None:
        """Registra la consulta en memoria (tests)."""
        self.med_queries.append(
            {
                "conversation_id": conversation_id,
                "provider_id": provider_id,
                "term": term,
                "product_id": product_id,
                "product_name": product_name,
                "result_count": result_count,
                "added_to_cart": added_to_cart,
            }
        )

    # ------------------------------------------------- pacientes crónicos ---
    # In-memory (tests): condiciones de referencia + perfiles.

    condiciones_cronicas: list[dict[str, str]] = [
        {"pattern": "losartan", "condicion": "hipertension"},
        {"pattern": "enalapril", "condicion": "hipertension"},
        {"pattern": "metformina", "condicion": "diabetes"},
        {"pattern": "levotiroxina", "condicion": "hipotiroidismo"},
    ]

    async def condiciones_para_termino(self, term: str) -> list[str]:
        t = (term or "").strip().lower()
        vistos: set[str] = set()
        out: list[str] = []
        for c in self.condiciones_cronicas:
            if c["pattern"] in t and c["condicion"] not in vistos:
                vistos.add(c["condicion"])
                out.append(c["condicion"])
        return out

    async def registrar_consulta_cronica(
        self,
        provider_id: str,
        wa_identity: str,
        condiciones: list[str],
    ) -> None:
        for cond in condiciones:
            key = (provider_id, wa_identity, cond)
            prof = self.patient_profiles.get(key)
            if prof is None:
                self.patient_profiles[key] = {
                    "confianza": 1,
                    "nivel": "bajo",
                    "consent": False,
                }
            else:
                prof["confianza"] += 1
                prof["nivel"] = "alto" if prof["confianza"] >= 2 else "medio"

    async def chronic_patients(
        self,
        provider_id: str,
        solo_consentidos: bool = False,
    ) -> list[dict[str, Any]]:
        out = []
        for (pid, wa, cond), prof in self.patient_profiles.items():
            if pid != provider_id:
                continue
            if solo_consentidos and not prof["consent"]:
                continue
            out.append(
                {
                    "wa_identity": wa,
                    "condicion": cond,
                    "confianza": prof["confianza"],
                    "nivel": prof["nivel"],
                    "consent": prof["consent"],
                }
            )
        return out

    async def marcar_consent(
        self, provider_id: str, wa_identity: str, consent: bool = True
    ) -> None:
        for (pid, wa, _cond), prof in self.patient_profiles.items():
            if pid == provider_id and wa == wa_identity:
                prof["consent"] = consent

    async def top_med_queries(
        self,
        provider_id: str,
        desde: str | None = None,
        hasta: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Top medicamentos más consultados (memoria, tests)."""
        from collections import Counter

        conteo: Counter[str] = Counter()
        for q in self.med_queries:
            if q["provider_id"] != provider_id:
                continue
            conteo[q["term"]] += 1
        return [
            {"term": term, "consultas": n}
            for term, n in conteo.most_common(limit)
        ]

    async def cart_set(
        self,
        conversation_id: int,
        product_id: str,
        cantidad: int,
    ) -> CartItem | None:
        items = self.cart.get(conversation_id, [])
        existing = next((i for i in items if i.product_id == product_id), None)
        if existing is None:
            return None
        existing.cantidad = max(1, cantidad)
        existing.updated_at = utcnow()
        return existing

    async def due_followups(self, now: datetime) -> list[Conversation]:
        return [
            c for c in self.conversations.values()
            if c.followup_due_at is not None
            and c.followup_due_at <= now
            and not c.followup_sent
            and c.phase != "cerrada"
        ]

    async def claim_followup(self, conversation_id: int) -> bool:
        conv = self.conversations[conversation_id]
        if conv.followup_sent:
            return False
        conv.followup_sent = True
        return True

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------- contexto ---


@dataclass
class AppContext:
    """Dependencias vivas de la app — inyectables en tests."""

    settings: "Settings"
    store: Store
    crm: Any  # CrmClient
    llm: Any  # OpenAiLlm o fake con .complete()
    profile: Any | None = None  # ProfileProvider; None en tests = perfil mínimo
    coalescer: Any | None = None
    relay_wake: asyncio.Event = field(default_factory=asyncio.Event)
    # Un candado por identidad: los turnos de UNA conversación se serializan.
    # Sin esto, una ráfaga que llega mientras el turno anterior sigue en vuelo
    # abre un segundo turno con contexto viejo (se reservó una cita antes de
    # leer el mensaje que la corregía, y salieron dos respuestas encimadas).
    turn_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Cuántos turnos tienen tomado (o esperan) el candado de esa identidad.
    turn_lock_users: dict[str, int] = field(default_factory=dict)
    # Modo laboratorio: cuando NO es None, _send() captura las respuestas en
    # esta lista en vez de enviarlas por WhatsApp (el CRM las persiste en una
    # conversación is_test y construye el transcript). Lo setea el endpoint
    # /api/chat y lo restaura tras el turno.
    lab_outbox: list[str] | None = None
