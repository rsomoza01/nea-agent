"""Autorización Staff: contexto tipado, decisión deny-by-default y catálogo de operaciones.

Implementa el diseño de SECURITY-ACTOR-POLICIES §3 (puntos 3-6 del Paso 11) para la
superficie Staff: la política decide **en backend**, el LLM no concede permisos
(ADR-0002). Dos invariantes mandan:

  1. **Deny-by-default (regla madre).** `autorizar` devuelve `DENEGADO` SALVO que exista
     una regla explícita que autorice la tupla (actor Staff autenticado × operación del
     catálogo cerrado × rol suficiente × farmacia coincidente) con sus precondiciones.
  2. **El catálogo de operaciones es cerrado.** El LLM/cliente no puede inventar una
     operación: solo las claves de `OPERACIONES` existen; cualquier otra → DENEGADO. No
     se construye SQL ni se invocan operaciones arbitrarias (punto 5 del Paso 11).

Las operaciones de dominio NO se duplican: cada una REUTILIZA un caso de uso/consulta
existente (analytics, pedido, perfiles); aquí solo vive la política de ENTRADA y su
clasificación por riesgo (SECURITY-ACTOR-POLICIES §3.4).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.staff.identity import ActorType, Role, rol_alcanza


# --------------------------------------------------------- contexto tipado ---


@dataclass(frozen=True)
class AuthorizationContext:
    """Quién pide qué, resuelto SIEMPRE en backend (nunca desde el texto/LLM).

    Para Staff se construye desde una `StaffSession` vigente (ver `api.resolver_actor`):
    `pharmacy_id` y `role` salen de la sesión emitida, no del query/body del cliente.
    """

    actor: ActorType
    actor_id: str
    pharmacy_id: str
    role: Role | None
    authenticated: bool
    correlation_id: str
    issued_at: datetime | None = None


# ----------------------------------------------------------- riesgo + ops ---


class Riesgo(str, Enum):
    """Clasificación de riesgo de una operación interna (punto 1 del Paso 11)."""

    BAJO = "bajo"      # lectura agregada, sin PII directa
    MEDIO = "medio"    # lectura sensible (perfiles) o mutación acotada
    ALTO = "alto"      # mutación de alto impacto (reservado; no hay en el piloto)


@dataclass(frozen=True)
class StaffOperation:
    """Una operación interna autorizable, con su política y clasificación de riesgo."""

    name: str
    riesgo: Riesgo
    rol_minimo: Role
    mutacion: bool
    requiere_confirmacion: bool


# Catálogo CERRADO de operaciones del piloto (punto 10: piloto cerrado, no todo-abierto).
# Clasificadas por riesgo (punto 1). Las lecturas reutilizan analytics/pedidos; la única
# mutación del piloto (consent) exige confirmación explícita (punto 4) y rol farmacéutico.
OPERACIONES: dict[str, StaffOperation] = {
    # Lecturas agregadas de bajo riesgo (sin PII directa).
    "analytics.top_meds": StaffOperation(
        "analytics.top_meds", Riesgo.BAJO, Role.CLERK, mutacion=False,
        requiere_confirmacion=False,
    ),
    "order.lookup": StaffOperation(
        "order.lookup", Riesgo.BAJO, Role.CLERK, mutacion=False,
        requiere_confirmacion=False,
    ),
    # Lectura sensible (pacientes crónicos ≈ perfiles): rol farmacéutico.
    "analytics.chronic_patients": StaffOperation(
        "analytics.chronic_patients", Riesgo.MEDIO, Role.PHARMACIST, mutacion=False,
        requiere_confirmacion=False,
    ),
    # Mutación acotada: marcar/retirar consentimiento crónico. Rol farmacéutico +
    # confirmación explícita (el riesgo lo amerita, punto 4).
    "chronic.mark_consent": StaffOperation(
        "chronic.mark_consent", Riesgo.MEDIO, Role.PHARMACIST, mutacion=True,
        requiere_confirmacion=True,
    ),
}


# --------------------------------------------------------- decisión tipada ---


class Veredicto(str, Enum):
    AUTORIZADO = "autorizado"
    DENEGADO = "denegado"
    REQUIERE_ACLARACION = "requiere_aclaracion"  # falta confirmación → preguntar, no actuar


@dataclass(frozen=True)
class PolicyDecision:
    """Resultado tipado de `autorizar` (deny-by-default). `motivo` alimenta la auditoría."""

    veredicto: Veredicto
    operation: str | None = None
    motivo: str | None = None

    @property
    def autorizado(self) -> bool:
        return self.veredicto is Veredicto.AUTORIZADO

    # Código estable para el campo `result` de auditoría (autorizado / denegado:<motivo>).
    def audit_result(self) -> str:
        if self.veredicto is Veredicto.AUTORIZADO:
            return "autorizado"
        return f"{self.veredicto.value}:{self.motivo or 'sin_motivo'}"

    @classmethod
    def permitir(cls, operation: str) -> "PolicyDecision":
        return cls(Veredicto.AUTORIZADO, operation=operation)

    @classmethod
    def denegar(cls, motivo: str, operation: str | None = None) -> "PolicyDecision":
        return cls(Veredicto.DENEGADO, operation=operation, motivo=motivo)

    @classmethod
    def aclarar(cls, motivo: str, operation: str) -> "PolicyDecision":
        return cls(Veredicto.REQUIERE_ACLARACION, operation=operation, motivo=motivo)


def autorizar(
    ctx: AuthorizationContext,
    operation: str,
    *,
    resource_pharmacy_id: str | None = None,
    confirmado: bool = False,
) -> PolicyDecision:
    """Decide si `ctx` puede ejecutar `operation` sobre un recurso de una farmacia.

    Deny-by-default: se recorre la lista de causas de denegación; solo si NINGUNA
    aplica (y, para mutaciones que lo exigen, hay confirmación) se AUTORIZA. Orden de
    evaluación (de identidad a precondición) espejando SECURITY-ACTOR-POLICIES §3.2:

      1. actor no Staff / no autenticado      → DENEGADO (identidad)
      2. operación fuera del catálogo cerrado  → DENEGADO (no se inventan operaciones)
      3. rol ausente o insuficiente            → DENEGADO (mínimo privilegio, N3)
      4. farmacia del recurso ≠ la de la sesión→ DENEGADO (tenant-crossing, N2)
      5. mutación que exige confirmación sin ella → REQUIERE_ACLARACION (no actuar)
    """
    # 1. Identidad: solo un Staff autenticado puede siquiera intentar.
    if ctx.actor is not ActorType.STAFF or not ctx.authenticated:
        return PolicyDecision.denegar("identidad_no_staff", operation)

    # 2. Catálogo cerrado: la operación debe existir explícitamente.
    op = OPERACIONES.get(operation)
    if op is None:
        return PolicyDecision.denegar("operacion_no_permitida", operation)

    # 3. Rol: mínimo privilegio. role=None (sin sesión de rol) nunca alcanza.
    if not rol_alcanza(ctx.role, op.rol_minimo):
        return PolicyDecision.denegar("rol_insuficiente", operation)

    # 4. Tenant scope: el recurso debe ser de la farmacia de la sesión. El pharmacy_id
    #    entrante se COMPARA, nunca se adopta (anti tenant-crossing / IDOR).
    if resource_pharmacy_id is not None and resource_pharmacy_id != ctx.pharmacy_id:
        return PolicyDecision.denegar("tenant_mismatch", operation)

    # 5. Confirmación explícita para mutaciones de riesgo.
    if op.mutacion and op.requiere_confirmacion and not confirmado:
        return PolicyDecision.aclarar("confirmacion_requerida", operation)

    return PolicyDecision.permitir(operation)
