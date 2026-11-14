"""Identidad verificable de empleado: modelos, credenciales, sesión y persistencia.

Materializa el modelo de identidad que ADR-0008 §8 exigía para DESBLOQUEAR el Paso 11
(ver ADR-0010). Principios no negociables:

  * **No es la X-API-Key ni el nº de WhatsApp.** Un empleado es una fila real en
    `staff_user` con credencial propia (hasheada), una farmacia y un rol. La posesión
    del secreto de canal (`CRM_BOT_API_KEY`) NO concede identidad Staff (ADR-0002/0008).
  * **Credencial hasheada, nunca en claro** (`construir_secret_hash` / `verificar_secret`):
    pbkdf2-hmac-sha256 con sal por usuario. La BD nunca ve el secreto.
  * **Sesión emitida, con TTL y revocable** (`emitir_token` / `verificar_sesion`): el
    token Bearer vive solo en el cliente; la BD guarda su SHA-256. Caducidad obligatoria
    (`expires_at`) y `revoked` por persona hacen ejercitables las negativas N4 (expirada)
    y "sesión revocada".
  * **Tenant fijado al emitir.** `pharmacy_id`/`role` se copian del empleado a la sesión;
    las operaciones los COMPARAN, nunca los adoptan del query/body (anti tenant-crossing).

Este módulo NO decide permisos (eso es `policy`) ni expone HTTP (eso es `api`). Solo
modela identidad y su persistencia (`StaffStore`), con una implementación en memoria de
referencia para tests y la real en `pg.PgStaffStore`.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol, runtime_checkable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ enums ---


class ActorType(str, Enum):
    """Quién pide. Se resuelve en backend, nunca desde el texto/LLM (ADR-0002).

    `STAFF` SOLO se materializa con una sesión de empleado válida (este módulo). Los
    demás actores ya existían de hecho en el sistema (ver SECURITY-ACTOR-POLICIES §2).
    """

    CUSTOMER = "customer"          # nº WhatsApp (no autenticado)
    INTEGRATION = "integration"    # X-API-Key compartida (no es una persona)
    SYSTEM = "system"              # workers relay/followup/sender
    STAFF = "staff"                # empleado autenticado (farmacia + rol)


class Role(str, Enum):
    """Rol individual del empleado (mínimo privilegio, jerárquico).

    admin ⊇ pharmacist ⊇ clerk: un rol superior puede todo lo del inferior. El rango
    lo usa `policy.authorize` para exigir rol MÍNIMO por operación.
    """

    CLERK = "clerk"            # empleado de mostrador (operaciones de bajo riesgo)
    PHARMACIST = "pharmacist"  # farmacéutico (lectura sensible + mutaciones acotadas)
    ADMIN = "admin"            # administrador de la farmacia


_RANGO_ROL: dict[Role, int] = {Role.CLERK: 1, Role.PHARMACIST: 2, Role.ADMIN: 3}


def rol_alcanza(rol: Role | None, minimo: Role) -> bool:
    """¿El rol del actor alcanza el rol mínimo exigido? (None nunca alcanza)."""
    if rol is None:
        return False
    return _RANGO_ROL[rol] >= _RANGO_ROL[minimo]


# --------------------------------------------------------------- credenciales ---
# pbkdf2-hmac-sha256 con sal por usuario. Formato almacenado:
#   pbkdf2_sha256$<iteraciones>$<sal_hex>$<hash_hex>
# Sin dependencias nuevas (stdlib hashlib/secrets/hmac): no se añade passlib/bcrypt.

_PBKDF2_ITERACIONES = 200_000


def construir_secret_hash(secreto: str, *, iteraciones: int = _PBKDF2_ITERACIONES) -> str:
    """Deriva el hash almacenable de un secreto de empleado (para provisionar usuarios).

    Uso (fuera de runtime, el propietario provisiona el piloto cerrado):
        hash = construir_secret_hash("secreto-del-empleado")
        store.crear_staff_user(..., secret_hash=hash)
    """
    if not secreto:
        raise ValueError("el secreto no puede ser vacío")
    sal = secrets.token_bytes(16)
    derivado = hashlib.pbkdf2_hmac("sha256", secreto.encode("utf-8"), sal, iteraciones)
    return f"pbkdf2_sha256${iteraciones}${sal.hex()}${derivado.hex()}"


def verificar_secret(secreto: str, almacenado: str) -> bool:
    """Compara en tiempo constante un secreto candidato contra el hash almacenado."""
    try:
        algo, iter_s, sal_hex, hash_hex = almacenado.split("$")
    except (ValueError, AttributeError):
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iteraciones = int(iter_s)
        sal = bytes.fromhex(sal_hex)
        esperado = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    derivado = hashlib.pbkdf2_hmac("sha256", (secreto or "").encode("utf-8"), sal, iteraciones)
    return hmac.compare_digest(derivado, esperado)


def nuevo_token() -> str:
    """Token Bearer opaco (se entrega UNA vez al cliente; la BD solo guarda su hash)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 del token: lo que se persiste y se busca (nunca el token en claro)."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def nuevo_id() -> str:
    return str(uuid.uuid4())


# ------------------------------------------------------------------ modelos ---


@dataclass(frozen=True)
class StaffUser:
    """Empleado: identidad real con farmacia y rol (no un canal compartido)."""

    id: str
    pharmacy_id: str
    username: str
    role: Role
    secret_hash: str
    active: bool = True


@dataclass(frozen=True)
class StaffSession:
    """Sesión emitida tras login: token (hasheado), tenant/rol fijados, TTL y revocación."""

    token_hash: str
    staff_user_id: str
    pharmacy_id: str
    role: Role
    issued_at: datetime
    expires_at: datetime
    revoked: bool = False

    def vigente(self, *, ahora: datetime | None = None) -> bool:
        """¿La sesión sigue viva? (no revocada y no expirada) — fail-closed."""
        ahora = ahora or _utcnow()
        return (not self.revoked) and ahora < self.expires_at


# TTL por defecto de una sesión Staff. Corto a propósito: una superficie interna con
# mutaciones no debe mantener sesiones eternas. Re-login renueva (barato).
SESSION_TTL = timedelta(hours=8)


# ------------------------------------------------------------- persistencia ---


@runtime_checkable
class StaffStore(Protocol):
    """Persistencia de identidad/sesión/auditoría Staff (separada del Store de Customer).

    La lógica de dominio (catálogo, analytics, pedidos) se reutiliza vía el `Store`
    principal; este puerto solo cubre lo NUEVO del Paso 11: quién es el empleado, su
    sesión y el rastro de auditoría. Implementaciones: `EnMemoriaStaffStore` (tests) y
    `pg.PgStaffStore` (Postgres).
    """

    async def crear_staff_user(
        self, *, pharmacy_id: str, username: str, role: Role, secret_hash: str
    ) -> StaffUser: ...

    async def obtener_staff_user(
        self, *, pharmacy_id: str, username: str
    ) -> StaffUser | None: ...

    async def crear_sesion(self, sesion: StaffSession) -> None: ...

    async def obtener_sesion(self, token_hash: str) -> StaffSession | None: ...

    async def revocar_sesion(self, token_hash: str) -> None: ...

    async def registrar_auditoria(
        self,
        *,
        correlation_id: str,
        actor_id: str | None,
        pharmacy_id: str | None,
        action: str,
        resource_ref: str | None,
        result: str,
    ) -> None: ...


class EnMemoriaStaffStore:
    """Implementación en memoria del `StaffStore` (tests / lab sin Postgres).

    Especifica la semántica que replica `PgStaffStore`: unicidad (pharmacy_id, username),
    sesión por token_hash y auditoría append-only (la lista `auditoria` solo crece).
    """

    def __init__(self) -> None:
        self._users: dict[str, StaffUser] = {}            # id -> user
        self._por_cred: dict[tuple[str, str], str] = {}   # (pharmacy_id, username) -> id
        self._sesiones: dict[str, StaffSession] = {}      # token_hash -> sesión
        self.auditoria: list[dict[str, object]] = []      # append-only (solo crece)

    async def crear_staff_user(
        self, *, pharmacy_id: str, username: str, role: Role, secret_hash: str
    ) -> StaffUser:
        clave = (pharmacy_id, username)
        if clave in self._por_cred:
            raise ValueError("staff_user duplicado para (pharmacy_id, username)")
        user = StaffUser(
            id=nuevo_id(),
            pharmacy_id=pharmacy_id,
            username=username,
            role=role,
            secret_hash=secret_hash,
        )
        self._users[user.id] = user
        self._por_cred[clave] = user.id
        return user

    async def obtener_staff_user(
        self, *, pharmacy_id: str, username: str
    ) -> StaffUser | None:
        uid = self._por_cred.get((pharmacy_id, username))
        return self._users.get(uid) if uid is not None else None

    async def crear_sesion(self, sesion: StaffSession) -> None:
        self._sesiones[sesion.token_hash] = sesion

    async def obtener_sesion(self, token_hash: str) -> StaffSession | None:
        return self._sesiones.get(token_hash)

    async def revocar_sesion(self, token_hash: str) -> None:
        s = self._sesiones.get(token_hash)
        if s is not None:
            self._sesiones[token_hash] = StaffSession(
                token_hash=s.token_hash,
                staff_user_id=s.staff_user_id,
                pharmacy_id=s.pharmacy_id,
                role=s.role,
                issued_at=s.issued_at,
                expires_at=s.expires_at,
                revoked=True,
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
        self.auditoria.append(
            {
                "correlation_id": correlation_id,
                "actor_id": actor_id,
                "pharmacy_id": pharmacy_id,
                "action": action,
                "resource_ref": resource_ref,
                "result": result,
            }
        )


# --------------------------------------------------------- autenticación ---


async def autenticar(
    store: StaffStore, *, pharmacy_id: str, username: str, secreto: str
) -> StaffUser | None:
    """Verifica credenciales de empleado. Devuelve el `StaffUser` o None (fail-closed).

    Deniega (None) si: el empleado no existe, está inactivo, o el secreto no verifica.
    No distingue los casos hacia afuera (no filtra si el usuario existe).
    """
    user = await store.obtener_staff_user(pharmacy_id=pharmacy_id, username=username)
    if user is None or not user.active:
        return None
    if not verificar_secret(secreto, user.secret_hash):
        return None
    return user


async def emitir_token(store: StaffStore, user: StaffUser) -> tuple[str, StaffSession]:
    """Emite una sesión para un empleado autenticado. Devuelve (token_claro, sesión).

    El token en claro se devuelve UNA vez (va al cliente); la BD solo guarda su hash.
    """
    token = nuevo_token()
    ahora = _utcnow()
    sesion = StaffSession(
        token_hash=hash_token(token),
        staff_user_id=user.id,
        pharmacy_id=user.pharmacy_id,
        role=user.role,
        issued_at=ahora,
        expires_at=ahora + SESSION_TTL,
    )
    await store.crear_sesion(sesion)
    return token, sesion


async def verificar_sesion(store: StaffStore, token: str) -> StaffSession | None:
    """Resuelve un token Bearer a una sesión VIGENTE, o None (fail-closed).

    None si: token ausente/desconocido, sesión revocada o expirada. El llamador que
    recibe None responde 401 — nunca materializa un actor Staff sin sesión viva.
    """
    if not token:
        return None
    sesion = await store.obtener_sesion(hash_token(token))
    if sesion is None or not sesion.vigente():
        return None
    return sesion
