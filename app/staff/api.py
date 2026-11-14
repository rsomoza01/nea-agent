"""Router `/staff/*`: superficie del Staff Agent (piloto cerrado, Paso 11).

Separada de la superficie Customer (`/webhook`, `/chat`): el cliente entra por WhatsApp
y jamás alcanza estas rutas. Toda operación:
  1. exige una **sesión de empleado vigente** (Bearer); sin ella → 401 (no se materializa
     actor Staff) — negativas N1 (cliente→Staff) y N4 (expirada/revocada);
  2. pasa por `policy.autorizar` (deny-by-default): actor + rol + farmacia + operación +
     precondición en servidor (negativas N2 tenant, N3 rol);
  3. fija el tenant desde la SESIÓN (`session.pharmacy_id`), nunca desde el query/body:
     un `providerId` entrante se COMPARA (mismatch → denegado), nunca se adopta;
  4. **audita** el intento (autorizado o denegado) en `staff_audit`;
  5. reutiliza los casos de uso/consultas de dominio existentes (analytics, pedidos,
     perfiles); no duplica lógica Customer ni deja que el LLM construya SQL u operaciones.

Piloto CERRADO: sin empleados provisionados en `staff_user`, `/staff/login` no autentica
a nadie y todo queda denegado por defecto (fail-closed).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.staff.identity import (
    ActorType,
    Role,
    StaffStore,
    autenticar,
    emitir_token,
    verificar_sesion,
)
from app.staff.policy import AuthorizationContext, Veredicto, autorizar

logger = logging.getLogger("nea.staff")

router = APIRouter(prefix="/staff")


# --------------------------------------------------------------- utilidades ---


def _staff_store(request: Request) -> StaffStore | None:
    ctx = request.app.state.ctx
    if ctx is None:
        return None
    return getattr(ctx, "staff", None)


def _correlation_id(request: Request) -> str:
    """Correlación por request (replay/auditoría). Respeta un id entrante o genera uno."""
    return request.headers.get("X-Correlation-Id") or str(uuid.uuid4())


def _bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return ""


async def _resolver_actor(
    store: StaffStore, request: Request, correlation_id: str
) -> AuthorizationContext | None:
    """Resuelve el Bearer a un `AuthorizationContext` Staff vigente, o None (→ 401)."""
    sesion = await verificar_sesion(store, _bearer(request))
    if sesion is None:
        return None
    return AuthorizationContext(
        actor=ActorType.STAFF,
        actor_id=sesion.staff_user_id,
        pharmacy_id=sesion.pharmacy_id,
        role=sesion.role,
        authenticated=True,
        correlation_id=correlation_id,
        issued_at=sesion.issued_at,
    )


def _respuesta_denegacion(decision: Any) -> JSONResponse:
    """Traduce una decisión no-autorizada a HTTP: 409 si falta confirmación, 403 si no."""
    if decision.veredicto is Veredicto.REQUIERE_ACLARACION:
        return JSONResponse(
            {"error": "confirmation_required", "motivo": decision.motivo},
            status_code=409,
        )
    return JSONResponse({"error": "forbidden", "motivo": decision.motivo}, status_code=403)


async def _ejecutar(
    request: Request,
    operation: str,
    *,
    resource_ref: str | None,
    resource_pharmacy_id: str | None,
    confirmado: bool,
):
    """Patrón común: resolver store+actor, autorizar, AUDITAR, y devolver (actor, store)
    si procede. Devuelve una JSONResponse de error (401/403/409/503) en caso contrario.

    El llamador recibe una tupla `(actor, store)` SOLO cuando la operación quedó
    autorizada; si recibe una `JSONResponse`, debe devolverla tal cual.
    """
    store = _staff_store(request)
    if store is None:
        return JSONResponse({"error": "staff_surface_unavailable"}, status_code=503)
    corr = _correlation_id(request)
    actor = await _resolver_actor(store, request, corr)
    if actor is None:
        await store.registrar_auditoria(
            correlation_id=corr, actor_id=None, pharmacy_id=None,
            action=operation, resource_ref=resource_ref,
            result="denegado:no_autenticado",
        )
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    decision = autorizar(
        actor, operation,
        resource_pharmacy_id=resource_pharmacy_id, confirmado=confirmado,
    )
    await store.registrar_auditoria(
        correlation_id=corr, actor_id=actor.actor_id, pharmacy_id=actor.pharmacy_id,
        action=operation, resource_ref=resource_ref, result=decision.audit_result(),
    )
    if not decision.autorizado:
        return _respuesta_denegacion(decision)
    return actor, store


# ------------------------------------------------------------------ login ---


@router.post("/login")
async def login(request: Request) -> JSONResponse:
    """Autentica un empleado (pharmacyId + username + secret) y emite un Bearer con TTL."""
    store = _staff_store(request)
    if store is None:
        return JSONResponse({"error": "staff_surface_unavailable"}, status_code=503)
    corr = _correlation_id(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "body inválido"}, status_code=400)
    pharmacy_id = str((payload or {}).get("pharmacyId") or "")
    username = str((payload or {}).get("username") or "")
    secret = str((payload or {}).get("secret") or "")
    if not pharmacy_id or not username or not secret:
        return JSONResponse(
            {"error": "faltan pharmacyId, username, secret"}, status_code=400
        )

    user = await autenticar(
        store, pharmacy_id=pharmacy_id, username=username, secreto=secret
    )
    if user is None:
        # No se distingue hacia afuera "no existe" de "secreto malo" (no enumeración).
        await store.registrar_auditoria(
            correlation_id=corr, actor_id=None, pharmacy_id=pharmacy_id,
            action="staff.login", resource_ref=username,
            result="denegado:credenciales_invalidas",
        )
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    token, sesion = await emitir_token(store, user)
    await store.registrar_auditoria(
        correlation_id=corr, actor_id=user.id, pharmacy_id=user.pharmacy_id,
        action="staff.login", resource_ref=None, result="autorizado",
    )
    return JSONResponse(
        {
            "token": token,
            "role": sesion.role.value,
            "pharmacyId": sesion.pharmacy_id,
            "expiresAt": sesion.expires_at.isoformat(),
        }
    )


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoca la sesión actual (fail-safe: siempre 200, aun si el token ya no vale)."""
    store = _staff_store(request)
    if store is None:
        return JSONResponse({"error": "staff_surface_unavailable"}, status_code=503)
    corr = _correlation_id(request)
    sesion = await verificar_sesion(store, _bearer(request))
    if sesion is not None:
        await store.revocar_sesion(sesion.token_hash)
        await store.registrar_auditoria(
            correlation_id=corr, actor_id=sesion.staff_user_id,
            pharmacy_id=sesion.pharmacy_id, action="staff.logout",
            resource_ref=None, result="autorizado",
        )
    return JSONResponse({"ok": True})


# ------------------------------------------------ operaciones (piloto cerrado) ---


@router.get("/analytics/top-meds")
async def staff_top_meds(
    request: Request,
    provider_id: str = "",
    desde: str = "",
    hasta: str = "",
    limit: int = 20,
) -> JSONResponse:
    """Medicamentos más buscados de LA farmacia del empleado (lectura, rol clerk+)."""
    res = await _ejecutar(
        request, "analytics.top_meds",
        resource_ref=None,
        resource_pharmacy_id=provider_id or None,  # se compara con la sesión
        confirmado=False,
    )
    if isinstance(res, JSONResponse):
        return res
    actor, _store = res
    ctx = request.app.state.ctx
    try:
        data = await ctx.store.top_med_queries(
            provider_id=actor.pharmacy_id,  # SIEMPRE de la sesión, nunca del query
            desde=desde or None,
            hasta=hasta or None,
            limit=max(1, min(limit, 100)),
        )
        return JSONResponse({"provider_id": actor.pharmacy_id, "items": data})
    except Exception as exc:
        logger.exception("staff_top_meds: error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/analytics/chronic-patients")
async def staff_chronic_patients(
    request: Request,
    provider_id: str = "",
    consentidos: str = "",
) -> JSONResponse:
    """Pacientes crónicos de LA farmacia del empleado (lectura sensible, rol farmacéutico+)."""
    res = await _ejecutar(
        request, "analytics.chronic_patients",
        resource_ref=None,
        resource_pharmacy_id=provider_id or None,
        confirmado=False,
    )
    if isinstance(res, JSONResponse):
        return res
    actor, _store = res
    ctx = request.app.state.ctx
    try:
        data = await ctx.store.chronic_patients(
            provider_id=actor.pharmacy_id,
            solo_consentidos=consentidos.lower() in ("1", "true", "si", "sí"),
        )
        return JSONResponse({"provider_id": actor.pharmacy_id, "items": data})
    except Exception as exc:
        logger.exception("staff_chronic_patients: error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/orders/{order_attempt_id}")
async def staff_order_lookup(request: Request, order_attempt_id: str) -> JSONResponse:
    """Consulta el estado de un intento de pedido por id (lectura, rol clerk+)."""
    res = await _ejecutar(
        request, "order.lookup",
        resource_ref=order_attempt_id,
        resource_pharmacy_id=None,  # el intento se identifica por id; instancia mono-tenant
        confirmado=False,
    )
    if isinstance(res, JSONResponse):
        return res
    _actor, _store = res
    ctx = request.app.state.ctx
    try:
        intento = await ctx.store.obtener_intento(order_attempt_id)
        if intento is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(
            {
                "order_attempt_id": intento.order_attempt_id,
                "phase": intento.phase.value,
                "cart_fingerprint": intento.cart_fingerprint,
            }
        )
    except Exception as exc:
        logger.exception("staff_order_lookup: error")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/chronic/consent")
async def staff_mark_consent(request: Request) -> JSONResponse:
    """Marca/retira consentimiento crónico (MUTACIÓN, rol farmacéutico+, confirmación).

    Body: {waIdentity, consent?: bool (default true), confirm: bool}. Sin `confirm:true`
    la política devuelve 409 (confirmation_required) y NO muta. Idempotente: repetir con
    el mismo valor no cambia el estado (robusto ante replay).
    """
    store = _staff_store(request)
    if store is None:
        return JSONResponse({"error": "staff_surface_unavailable"}, status_code=503)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "body inválido"}, status_code=400)
    wa_identity = str((payload or {}).get("waIdentity") or "")
    consent = bool((payload or {}).get("consent", True))
    confirmado = bool((payload or {}).get("confirm"))
    if not wa_identity:
        return JSONResponse({"error": "falta waIdentity"}, status_code=400)

    res = await _ejecutar(
        request, "chronic.mark_consent",
        resource_ref=wa_identity,
        resource_pharmacy_id=None,  # el perfil se escribe SIEMPRE bajo la farmacia de la sesión
        confirmado=confirmado,
    )
    if isinstance(res, JSONResponse):
        return res
    actor, _store = res
    ctx = request.app.state.ctx
    try:
        await ctx.store.marcar_consent(
            provider_id=actor.pharmacy_id,  # tenant de la sesión, no del body
            wa_identity=wa_identity,
            consent=consent,
        )
        return JSONResponse(
            {"ok": True, "waIdentity": wa_identity, "consent": consent}
        )
    except Exception as exc:
        logger.exception("staff_mark_consent: error")
        return JSONResponse({"error": str(exc)}, status_code=500)
