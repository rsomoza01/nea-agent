"""Persistencia real del bot: pool asyncpg + migraciones SQL idempotentes.

Implementa el protocolo `Store` de app/state.py contra Postgres.
"""
from __future__ import annotations

import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

from app.state import (
    BotMessage,
    CartItem,
    Conversation,
    OfferedSlot,
    PendingSend,
    RelayItem,
)

logger = logging.getLogger("nea.db")

_CONV_COLUMNS = frozenset(
    {
        "crm_conversation_id",
        "phase",
        "greeted",
        "media_notice_sent",
        "followup_due_at",
        "followup_sent",
        "last_inbound_at",
        "stalled_at",
        "last_product",
        "last_term",
        "last_options",
    }
)


def _conv_from_row(row: asyncpg.Record) -> Conversation:
    return Conversation(
        id=row["id"],
        wa_identity=row["wa_identity"],
        crm_conversation_id=row["crm_conversation_id"],
        phase=row["phase"],
        greeted=row["greeted"],
        media_notice_sent=row["media_notice_sent"],
        followup_due_at=row["followup_due_at"],
        followup_sent=row["followup_sent"],
        last_inbound_at=row["last_inbound_at"],
        stalled_at=row["stalled_at"],
        last_product=row.get("last_product"),
        last_term=row.get("last_term"),
        last_options=row.get("last_options"),
    )


class PgStore:
    """Store respaldado por Postgres (asyncpg)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PgStore sin conectar — llama connect() primero")
        return self._pool

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)

    async def migrate(self, migrations_dir: Path) -> None:
        """Aplica todas las migraciones en orden. Son idempotentes: re-correr es seguro."""
        files = sorted(migrations_dir.glob("*.sql"))
        async with self.pool.acquire() as conn:
            for path in files:
                logger.info("migración: aplicando %s", path.name)
                await conn.execute(path.read_text(encoding="utf-8"))

    # -------------------------------------------------------------- dedup ---

    async def mark_processed(self, wa_message_id: str) -> bool:
        row = await self.pool.fetchrow(
            """
            INSERT INTO processed_message (wa_message_id) VALUES ($1)
            ON CONFLICT DO NOTHING
            RETURNING wa_message_id
            """,
            wa_message_id,
        )
        return row is not None

    # -------------------------------------------------------------- relay ---

    async def enqueue_relay(self, body: bytes, signature: str | None) -> int:
        row = await self.pool.fetchrow(
            "INSERT INTO relay_queue (body, signature) VALUES ($1, $2) RETURNING id",
            body,
            signature,
        )
        assert row is not None
        return row["id"]

    async def due_relays(self, now: datetime) -> list[RelayItem]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM relay_queue
            WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_retry_at <= $1
            ORDER BY id
            LIMIT 50
            """,
            now,
        )
        return [
            RelayItem(
                id=r["id"],
                body=bytes(r["body"]),
                signature=r["signature"],
                attempts=r["attempts"],
                created_at=r["created_at"],
                next_retry_at=r["next_retry_at"],
                delivered_at=r["delivered_at"],
                abandoned_at=r["abandoned_at"],
            )
            for r in rows
        ]

    async def mark_relay_delivered(self, relay_id: int) -> None:
        await self.pool.execute(
            "UPDATE relay_queue SET delivered_at = now() WHERE id = $1", relay_id
        )

    async def mark_relay_abandoned(self, relay_id: int) -> None:
        await self.pool.execute(
            "UPDATE relay_queue SET abandoned_at = now() WHERE id = $1", relay_id
        )

    async def reschedule_relay(
        self, relay_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        await self.pool.execute(
            "UPDATE relay_queue SET attempts = $2, next_retry_at = $3 WHERE id = $1",
            relay_id,
            attempts,
            next_retry_at,
        )

    # ----------------------------------------------------- conversaciones ---

    async def get_or_create_conversation(self, wa_identity: str) -> Conversation:
        row = await self.pool.fetchrow(
            """
            INSERT INTO bot_conversation (wa_identity) VALUES ($1)
            ON CONFLICT (wa_identity) DO UPDATE SET updated_at = now()
            RETURNING *
            """,
            wa_identity,
        )
        assert row is not None
        return _conv_from_row(row)

    async def update_conversation(self, conversation_id: int, **fields: Any) -> None:
        unknown = set(fields) - _CONV_COLUMNS
        if unknown:
            raise ValueError(f"columnas desconocidas en update_conversation: {unknown}")
        if not fields:
            return
        # last_product / last_options se guardan como JSON string en columnas text.
        for col in ("last_product", "last_options"):
            if col in fields and not isinstance(fields[col], str):
                fields[col] = json.dumps(fields[col], ensure_ascii=False)
        sets = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(fields))
        await self.pool.execute(
            f"UPDATE bot_conversation SET {sets}, updated_at = now() WHERE id = $1",
            conversation_id,
            *fields.values(),
        )

    async def reset_conversation(self, conversation_id: int) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM bot_message WHERE conversation_id = $1",
                    conversation_id,
                )
                await conn.execute(
                    "DELETE FROM offered_slots WHERE conversation_id = $1",
                    conversation_id,
                )
                await conn.execute(
                    """
                    UPDATE bot_conversation
                    SET phase = 'descubrimiento', greeted = FALSE,
                        media_notice_sent = FALSE, followup_due_at = NULL,
                        followup_sent = FALSE, stalled_at = NULL,
                        updated_at = now()
                    WHERE id = $1
                    """,
                    conversation_id,
                )

    # ----------------------------------------------------------- mensajes ---

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        wa_message_id: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO bot_message (conversation_id, role, content, wa_message_id)
            VALUES ($1, $2, $3, $4)
            """,
            conversation_id,
            role,
            content,
            wa_message_id,
        )

    async def recent_messages(
        self, conversation_id: int, limit: int
    ) -> list[BotMessage]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM bot_message WHERE conversation_id = $1
            ORDER BY id DESC LIMIT $2
            """,
            conversation_id,
            limit,
        )
        return [
            BotMessage(
                id=r["id"],
                conversation_id=r["conversation_id"],
                role=r["role"],
                content=r["content"],
                wa_message_id=r["wa_message_id"],
                created_at=r["created_at"],
            )
            for r in reversed(rows)
        ]

    # -------------------------------------------------------------- slots ---

    async def replace_offered_slots(
        self, conversation_id: int, slots: list[OfferedSlot]
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM offered_slots WHERE conversation_id = $1",
                    conversation_id,
                )
                for slot in slots:
                    await conn.execute(
                        """
                        INSERT INTO offered_slots (conversation_id, start_utc, end_utc, label)
                        VALUES ($1, $2, $3, $4)
                        """,
                        conversation_id,
                        slot.start_utc,
                        slot.end_utc,
                        slot.label,
                    )

    async def get_offered_slots(self, conversation_id: int) -> list[OfferedSlot]:
        rows = await self.pool.fetch(
            "SELECT * FROM offered_slots WHERE conversation_id = $1 ORDER BY start_utc",
            conversation_id,
        )
        return [
            OfferedSlot(
                conversation_id=r["conversation_id"],
                start_utc=r["start_utc"],
                end_utc=r["end_utc"],
                label=r["label"],
                offered_at=r["offered_at"],
            )
            for r in rows
        ]

    async def clear_offered_slots(self, conversation_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM offered_slots WHERE conversation_id = $1", conversation_id
        )

    # ------------------------------------------------- envíos pendientes ---

    async def enqueue_pending_send(
        self, conversation_id: int, crm_conversation_id: str, content: str
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO pending_send (conversation_id, crm_conversation_id, content)
            VALUES ($1, $2, $3) RETURNING id
            """,
            conversation_id,
            crm_conversation_id,
            content,
        )
        assert row is not None
        return row["id"]

    async def due_pending_sends(self, now: datetime) -> list[PendingSend]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM pending_send
            WHERE delivered_at IS NULL AND abandoned_at IS NULL AND next_retry_at <= $1
            ORDER BY id
            LIMIT 50
            """,
            now,
        )
        return [
            PendingSend(
                id=r["id"],
                conversation_id=r["conversation_id"],
                crm_conversation_id=r["crm_conversation_id"],
                content=r["content"],
                attempts=r["attempts"],
                created_at=r["created_at"],
                next_retry_at=r["next_retry_at"],
                delivered_at=r["delivered_at"],
                abandoned_at=r["abandoned_at"],
            )
            for r in rows
        ]

    async def mark_pending_send_delivered(self, pending_id: int) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET delivered_at = now() WHERE id = $1", pending_id
        )

    async def mark_pending_send_abandoned(self, pending_id: int) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET abandoned_at = now() WHERE id = $1", pending_id
        )

    async def reschedule_pending_send(
        self, pending_id: int, attempts: int, next_retry_at: datetime
    ) -> None:
        await self.pool.execute(
            "UPDATE pending_send SET attempts = $2, next_retry_at = $3 WHERE id = $1",
            pending_id,
            attempts,
            next_retry_at,
        )

    # ------------------------------------------------- carrito + analytics ---

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
        row = await self.pool.fetchrow(
            """
            INSERT INTO bot_cart
              (conversation_id, product_id, producto, presentacion, laboratorio,
               cantidad, precio_usd, precio_bs)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (conversation_id, product_id)
            DO UPDATE SET cantidad = bot_cart.cantidad + EXCLUDED.cantidad,
                          precio_usd = EXCLUDED.precio_usd,
                          precio_bs = EXCLUDED.precio_bs,
                          updated_at = now()
            RETURNING *
            """,
            conversation_id, product_id, producto, presentacion, laboratorio,
            cantidad, precio_usd, precio_bs,
        )
        assert row is not None
        return CartItem(
            id=row["id"], conversation_id=row["conversation_id"],
            product_id=row["product_id"], producto=row["producto"],
            presentacion=row["presentacion"] or "", laboratorio=row["laboratorio"] or "",
            cantidad=row["cantidad"], precio_usd=row["precio_usd"], precio_bs=row["precio_bs"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    async def cart_items(
        self, conversation_id: int, session_hours: float | None = None
    ) -> list[CartItem]:
        if session_hours is not None:
            rows = await self.pool.fetch(
                """
                SELECT * FROM bot_cart
                WHERE conversation_id = $1 AND updated_at >= now() - make_interval(secs => $2)
                ORDER BY created_at
                """,
                conversation_id, session_hours * 3600,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM bot_cart WHERE conversation_id = $1 ORDER BY created_at",
                conversation_id,
            )
        return [
            CartItem(
                id=r["id"], conversation_id=r["conversation_id"],
                product_id=r["product_id"], producto=r["producto"],
                presentacion=r["presentacion"] or "", laboratorio=r["laboratorio"] or "",
                cantidad=r["cantidad"], precio_usd=r["precio_usd"], precio_bs=r["precio_bs"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    async def cart_clear(self, conversation_id: int) -> None:
        await self.pool.execute("DELETE FROM bot_cart WHERE conversation_id = $1", conversation_id)

    async def cart_set(
        self, conversation_id: int, product_id: str, cantidad: int
    ) -> CartItem | None:
        row = await self.pool.fetchrow(
            """
            UPDATE bot_cart SET cantidad = $3, updated_at = now()
            WHERE conversation_id = $1 AND product_id = $2
            RETURNING *
            """,
            conversation_id, product_id, cantidad,
        )
        if row is None:
            return None
        return CartItem(
            id=row["id"], conversation_id=row["conversation_id"],
            product_id=row["product_id"], producto=row["producto"],
            presentacion=row["presentacion"] or "", laboratorio=row["laboratorio"] or "",
            cantidad=row["cantidad"], precio_usd=row["precio_usd"], precio_bs=row["precio_bs"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

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
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO med_queries
              (conversation_id, provider_id, term, product_id, product_name,
               result_count, added_to_cart)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            conversation_id, provider_id, term, product_id, product_name,
            result_count, added_to_cart,
        )

    async def top_med_queries(
        self,
        provider_id: str,
        desde: str | None = None,
        hasta: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT term, COUNT(*) AS consultas
            FROM med_queries
            WHERE provider_id = $1
        """
        params: list[Any] = [provider_id]
        if desde:
            params.append(desde[:10])
            sql += f" AND created_at >= ${len(params)}::date"
        if hasta:
            params.append(hasta[:10])
            sql += f" AND created_at <= (${len(params)}::date + interval '1 day')"
        sql += " GROUP BY term ORDER BY consultas DESC LIMIT $%d" % (len(params) + 1)
        params.append(limit)
        rows = await self.pool.fetch(sql, *params)
        return [
            {"term": r["term"], "consultas": r["consultas"]}
            for r in rows
        ]

    # pacientes crónicos (Fase 2: clasificación automática)
    async def condiciones_para_termino(self, term: str) -> list[str]:
        t = (term or "").strip().lower()
        rows = await self.pool.fetch(
            "SELECT condicion FROM condiciones_cronicas WHERE activo = TRUE"
        )
        vistos: set[str] = set()
        out: list[str] = []
        for r in rows:
            if r["condicion"] in t and r["condicion"] not in vistos:
                vistos.add(r["condicion"])
                out.append(r["condicion"])
        # Coincidencia por substring del patrón (los seeds son 'losartan', etc.)
        if not out and t:
            pat_rows = await self.pool.fetch(
                "SELECT pattern, condicion FROM condiciones_cronicas WHERE activo = TRUE"
            )
            for r in pat_rows:
                if r["pattern"] in t and r["condicion"] not in vistos:
                    vistos.add(r["condicion"])
                    out.append(r["condicion"])
        return out

    async def registrar_consulta_cronica(
        self,
        provider_id: str,
        wa_identity: str,
        condiciones: list[str],
    ) -> None:
        for cond in condiciones:
            await self.pool.execute(
                """
                INSERT INTO patient_profiles (provider_id, wa_identity, condicion, confianza, nivel)
                VALUES ($1, $2, $3, 1, 'bajo')
                ON CONFLICT (provider_id, wa_identity, condicion)
                DO UPDATE SET confianza = patient_profiles.confianza + 1,
                              nivel = CASE
                                WHEN patient_profiles.confianza + 1 >= 2 THEN 'alto'
                                ELSE 'medio'
                              END,
                              updated_at = now()
                """,
                provider_id, wa_identity, cond,
            )

    async def chronic_patients(
        self,
        provider_id: str,
        solo_consentidos: bool = False,
    ) -> list[dict[str, Any]]:
        if solo_consentidos:
            rows = await self.pool.fetch(
                """
                SELECT wa_identity, condicion, confianza, nivel, consent,
                       first_seen_at, updated_at
                FROM patient_profiles WHERE provider_id = $1 AND consent = TRUE
                ORDER BY updated_at DESC
                """,
                provider_id,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT wa_identity, condicion, confianza, nivel, consent,
                       first_seen_at, updated_at
                FROM patient_profiles WHERE provider_id = $1
                ORDER BY updated_at DESC
                """,
                provider_id,
            )
        return [
            {
                "wa_identity": r["wa_identity"],
                "condicion": r["condicion"],
                "confianza": r["confianza"],
                "nivel": r["nivel"],
                "consent": r["consent"],
                "first_seen_at": r["first_seen_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    async def marcar_consent(
        self, provider_id: str, wa_identity: str, consent: bool = True
    ) -> None:
        await self.pool.execute(
            """
            UPDATE patient_profiles SET consent = $3, consent_at = now()
            WHERE provider_id = $1 AND wa_identity = $2
            """,
            provider_id, wa_identity, consent,
        )

    # -------------------------------------------------------- seguimiento ---

    async def due_followups(self, now: datetime) -> list[Conversation]:
        rows = await self.pool.fetch(
            """
            SELECT * FROM bot_conversation
            WHERE followup_due_at IS NOT NULL
              AND followup_due_at <= $1
              AND followup_sent = FALSE
              AND phase <> 'cerrada'
            ORDER BY followup_due_at
            LIMIT 20
            """,
            now,
        )
        return [_conv_from_row(r) for r in rows]

    async def claim_followup(self, conversation_id: int) -> bool:
        # Se marca ANTES de enviar: a lo sumo un empujón, incluso con crash.
        row = await self.pool.fetchrow(
            """
            UPDATE bot_conversation SET followup_sent = TRUE, updated_at = now()
            WHERE id = $1 AND followup_sent = FALSE
            RETURNING id
            """,
            conversation_id,
        )
        return row is not None

    # --------------------------------------------------------------- misc ---

    async def ping(self) -> None:
        await self.pool.fetchval("SELECT 1")

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
