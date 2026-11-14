"""Implementación Postgres del `StaffStore` (migración 018).

Comparte el pool asyncpg del `PgStore` principal (`store.pool`); no abre conexiones
propias. Replica la semántica de `EnMemoriaStaffStore`:
  - unicidad (pharmacy_id, username) vía índice único (duplicado → ValueError);
  - sesión indexada por `token_hash`;
  - auditoría **append-only** (solo INSERT; nunca UPDATE/DELETE sobre `staff_audit`).
"""
from __future__ import annotations

import asyncpg

from app.staff.identity import Role, StaffSession, StaffUser, nuevo_id


class PgStaffStore:
    """`StaffStore` respaldado por Postgres (asyncpg), sobre el pool del PgStore."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def crear_staff_user(
        self, *, pharmacy_id: str, username: str, role: Role, secret_hash: str
    ) -> StaffUser:
        uid = nuevo_id()
        try:
            await self._pool.execute(
                """
                INSERT INTO staff_user (id, pharmacy_id, username, role, secret_hash)
                VALUES ($1, $2, $3, $4, $5)
                """,
                uid, pharmacy_id, username, role.value, secret_hash,
            )
        except asyncpg.UniqueViolationError as exc:
            raise ValueError(
                "staff_user duplicado para (pharmacy_id, username)"
            ) from exc
        return StaffUser(
            id=uid, pharmacy_id=pharmacy_id, username=username, role=role,
            secret_hash=secret_hash,
        )

    async def obtener_staff_user(
        self, *, pharmacy_id: str, username: str
    ) -> StaffUser | None:
        row = await self._pool.fetchrow(
            """
            SELECT id, pharmacy_id, username, role, secret_hash, active
            FROM staff_user WHERE pharmacy_id = $1 AND username = $2
            """,
            pharmacy_id, username,
        )
        if row is None:
            return None
        return StaffUser(
            id=row["id"],
            pharmacy_id=row["pharmacy_id"],
            username=row["username"],
            role=Role(row["role"]),
            secret_hash=row["secret_hash"],
            active=row["active"],
        )

    async def crear_sesion(self, sesion: StaffSession) -> None:
        await self._pool.execute(
            """
            INSERT INTO staff_session
                (token_hash, staff_user_id, pharmacy_id, role, issued_at, expires_at, revoked)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            sesion.token_hash, sesion.staff_user_id, sesion.pharmacy_id,
            sesion.role.value, sesion.issued_at, sesion.expires_at, sesion.revoked,
        )

    async def obtener_sesion(self, token_hash: str) -> StaffSession | None:
        row = await self._pool.fetchrow(
            """
            SELECT token_hash, staff_user_id, pharmacy_id, role,
                   issued_at, expires_at, revoked
            FROM staff_session WHERE token_hash = $1
            """,
            token_hash,
        )
        if row is None:
            return None
        return StaffSession(
            token_hash=row["token_hash"],
            staff_user_id=row["staff_user_id"],
            pharmacy_id=row["pharmacy_id"],
            role=Role(row["role"]),
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            revoked=row["revoked"],
        )

    async def revocar_sesion(self, token_hash: str) -> None:
        await self._pool.execute(
            "UPDATE staff_session SET revoked = TRUE WHERE token_hash = $1",
            token_hash,
        )

    async def registrar_auditoria(
        self,
        *,
        correlation_id: str,
        actor_id: str | None,
        pharmacy_id: str | None,
        action: str,
        resource_ref: str | None,
        result: str,
    ) -> None:
        # Append-only: solo INSERT. No existe ruta de UPDATE/DELETE sobre staff_audit.
        await self._pool.execute(
            """
            INSERT INTO staff_audit
                (correlation_id, actor_id, pharmacy_id, action, resource_ref, result)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            correlation_id, actor_id, pharmacy_id, action, resource_ref, result,
        )
