"""Pruebas del Staff Agent (Paso 11): identidad, política deny-by-default y superficie HTTP.

Cubre el punto 8 del Paso 11 (escalamiento de privilegios, tenant-crossing, identidad no
autenticada, replay, operaciones no permitidas) y las negativas N1-N4 de
SECURITY-ACTOR-POLICIES §7, ahora EJERCITABLES porque existe identidad verificable
(ADR-0010 desbloquea ADR-0008). Más las positivas del piloto cerrado.

Dos niveles:
  * política pura (`autorizar`) — rápido, sin IO;
  * superficie HTTP (`/staff/*`) con `TestClient` sobre `MemoryStore` + `EnMemoriaStaffStore`.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.state import AppContext, MemoryStore
from app.staff.api import router as staff_router
from app.staff.identity import (
    ActorType,
    EnMemoriaStaffStore,
    Role,
    StaffSession,
    SESSION_TTL,
    autenticar,
    construir_secret_hash,
    emitir_token,
    hash_token,
    nuevo_token,
    rol_alcanza,
    verificar_secret,
    verificar_sesion,
)
from app.staff.policy import (
    AuthorizationContext,
    Veredicto,
    autorizar,
)
from app.state import utcnow

PHARMACY_A = "prov-A"
PHARMACY_B = "prov-B"


def _run(coro):
    return asyncio.run(coro)


def _ctx(actor=ActorType.STAFF, role=Role.PHARMACIST, pharmacy=PHARMACY_A, auth=True):
    return AuthorizationContext(
        actor=actor, actor_id="emp-1", pharmacy_id=pharmacy, role=role,
        authenticated=auth, correlation_id="corr-1",
    )


# =========================================================== identidad (unit) ===


def test_secret_hash_roundtrip():
    h = construir_secret_hash("sup3r-secreto")
    assert h.startswith("pbkdf2_sha256$")
    assert "sup3r-secreto" not in h  # nunca en claro
    assert verificar_secret("sup3r-secreto", h) is True
    assert verificar_secret("otro", h) is False
    assert verificar_secret("", h) is False


def test_secret_hash_formato_invalido_no_truena():
    assert verificar_secret("x", "basura") is False
    assert verificar_secret("x", "") is False


def test_token_no_se_guarda_en_claro():
    token = nuevo_token()
    assert hash_token(token) != token
    assert len(hash_token(token)) == 64  # sha256 hex


def test_sesion_vigente_expirada_revocada():
    ahora = utcnow()
    viva = StaffSession("h", "u", PHARMACY_A, Role.CLERK, ahora, ahora + timedelta(hours=1))
    expirada = StaffSession("h", "u", PHARMACY_A, Role.CLERK, ahora - timedelta(hours=2), ahora - timedelta(hours=1))
    revocada = StaffSession("h", "u", PHARMACY_A, Role.CLERK, ahora, ahora + timedelta(hours=1), revoked=True)
    assert viva.vigente() is True
    assert expirada.vigente() is False
    assert revocada.vigente() is False


def test_rol_alcanza_jerarquia():
    assert rol_alcanza(Role.ADMIN, Role.PHARMACIST) is True
    assert rol_alcanza(Role.PHARMACIST, Role.PHARMACIST) is True
    assert rol_alcanza(Role.CLERK, Role.PHARMACIST) is False
    assert rol_alcanza(None, Role.CLERK) is False


def test_autenticar_inactivo_y_mal_secreto():
    store = EnMemoriaStaffStore()
    h = construir_secret_hash("ok")
    _run(store.crear_staff_user(pharmacy_id=PHARMACY_A, username="ana", role=Role.CLERK, secret_hash=h))
    assert _run(autenticar(store, pharmacy_id=PHARMACY_A, username="ana", secreto="ok")) is not None
    assert _run(autenticar(store, pharmacy_id=PHARMACY_A, username="ana", secreto="mal")) is None
    # usuario inexistente / farmacia equivocada → None (no enumeración)
    assert _run(autenticar(store, pharmacy_id=PHARMACY_B, username="ana", secreto="ok")) is None
    assert _run(autenticar(store, pharmacy_id=PHARMACY_A, username="nadie", secreto="ok")) is None


# =========================================================== política (unit) ===


def test_politica_autoriza_happy_path():
    d = autorizar(_ctx(role=Role.PHARMACIST), "analytics.chronic_patients")
    assert d.autorizado and d.veredicto is Veredicto.AUTORIZADO


def test_politica_deny_no_staff():
    d = autorizar(_ctx(actor=ActorType.CUSTOMER), "analytics.top_meds")
    assert d.veredicto is Veredicto.DENEGADO and d.motivo == "identidad_no_staff"


def test_politica_deny_no_autenticado():
    d = autorizar(_ctx(auth=False), "analytics.top_meds")
    assert d.veredicto is Veredicto.DENEGADO and d.motivo == "identidad_no_staff"


def test_politica_deny_operacion_no_permitida():
    # No se inventan operaciones: cualquier clave fuera del catálogo cerrado → denegado.
    d = autorizar(_ctx(role=Role.ADMIN), "db.ejecutar_sql_arbitrario")
    assert d.veredicto is Veredicto.DENEGADO and d.motivo == "operacion_no_permitida"


def test_politica_deny_rol_insuficiente_N3():
    d = autorizar(_ctx(role=Role.CLERK), "analytics.chronic_patients")
    assert d.veredicto is Veredicto.DENEGADO and d.motivo == "rol_insuficiente"


def test_politica_deny_tenant_mismatch_N2():
    d = autorizar(_ctx(pharmacy=PHARMACY_A), "analytics.top_meds", resource_pharmacy_id=PHARMACY_B)
    assert d.veredicto is Veredicto.DENEGADO and d.motivo == "tenant_mismatch"


def test_politica_mutacion_requiere_confirmacion():
    sin = autorizar(_ctx(role=Role.PHARMACIST), "chronic.mark_consent", confirmado=False)
    assert sin.veredicto is Veredicto.REQUIERE_ACLARACION and sin.motivo == "confirmacion_requerida"
    con = autorizar(_ctx(role=Role.PHARMACIST), "chronic.mark_consent", confirmado=True)
    assert con.autorizado


def test_politica_admin_hereda_permisos():
    # admin ⊇ pharmacist ⊇ clerk: un admin alcanza una operación de rol farmacéutico.
    d = autorizar(_ctx(role=Role.ADMIN), "analytics.chronic_patients")
    assert d.autorizado


def test_audit_result_codificado():
    assert autorizar(_ctx(role=Role.CLERK), "analytics.chronic_patients").audit_result() == "denegado:rol_insuficiente"
    assert autorizar(_ctx(role=Role.PHARMACIST), "analytics.top_meds").audit_result() == "autorizado"


# =========================================================== superficie HTTP ===


@pytest.fixture
def entorno():
    """App mínima con router /staff montado (sin lifespan/workers), stores en memoria."""
    store = MemoryStore()
    staff = EnMemoriaStaffStore()
    ctx = AppContext(settings=Settings(), store=store, crm=None, llm=None, staff=staff)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(staff_router)
    client = TestClient(app)
    return client, store, staff, ctx


def _crear_empleado(staff, *, pharmacy=PHARMACY_A, username="ana", secret="ok", role=Role.PHARMACIST):
    _run(staff.crear_staff_user(
        pharmacy_id=pharmacy, username=username, role=role,
        secret_hash=construir_secret_hash(secret),
    ))


def _login(client, *, pharmacy=PHARMACY_A, username="ana", secret="ok"):
    r = client.post("/staff/login", json={"pharmacyId": pharmacy, "username": username, "secret": secret})
    return r


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_ok_devuelve_token(entorno):
    client, _store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.CLERK)
    r = _login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["token"] and body["role"] == "clerk" and body["pharmacyId"] == PHARMACY_A
    assert "expiresAt" in body
    # auditado como login autorizado
    assert any(a["action"] == "staff.login" and a["result"] == "autorizado" for a in staff.auditoria)


def test_login_credenciales_invalidas_401_auditado(entorno):
    client, _store, staff, _ctx_ = entorno
    _crear_empleado(staff)
    r = _login(client, secret="MAL")
    assert r.status_code == 401
    assert any(a["result"] == "denegado:credenciales_invalidas" for a in staff.auditoria)


def test_N1_no_autenticado_401_auditado(entorno):
    # Negativa N1 / identidad no autenticada: sin Bearer no se alcanza la superficie Staff.
    client, _store, staff, _ctx_ = entorno
    r = client.get("/staff/analytics/top-meds")
    assert r.status_code == 401
    assert any(a["result"] == "denegado:no_autenticado" and a["action"] == "analytics.top_meds" for a in staff.auditoria)


def test_top_meds_autorizado_scoped_a_sesion(entorno):
    client, store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.CLERK)
    # Semilla de consultas en DOS farmacias; el empleado es de A.
    _run(store.log_med_query(1, PHARMACY_A, "paracetamol", None, None, 1))
    _run(store.log_med_query(1, PHARMACY_A, "paracetamol", None, None, 1))
    _run(store.log_med_query(2, PHARMACY_B, "ibuprofeno", None, None, 1))
    token = _login(client).json()["token"]
    r = client.get("/staff/analytics/top-meds", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["provider_id"] == PHARMACY_A
    terms = {i["term"] for i in body["items"]}
    assert "paracetamol" in terms and "ibuprofeno" not in terms  # no cruza a B


def test_N3_clerk_no_puede_cronicos_403(entorno):
    client, _store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.CLERK)
    token = _login(client).json()["token"]
    r = client.get("/staff/analytics/chronic-patients", headers=_auth(token))
    assert r.status_code == 403
    assert any(a["result"] == "denegado:rol_insuficiente" for a in staff.auditoria)


def test_farmaceutico_si_puede_cronicos(entorno):
    client, store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.PHARMACIST)
    _run(store.registrar_consulta_cronica(PHARMACY_A, "5211", ["hipertension"]))
    token = _login(client).json()["token"]
    r = client.get("/staff/analytics/chronic-patients", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["provider_id"] == PHARMACY_A


def test_N2_tenant_crossing_denegado(entorno):
    # Empleado de A intenta leer con provider_id=B en el query → 403, sin fuga de datos.
    client, store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.CLERK)
    _run(store.log_med_query(2, PHARMACY_B, "ibuprofeno", None, None, 1))
    token = _login(client).json()["token"]
    r = client.get("/staff/analytics/top-meds", params={"provider_id": PHARMACY_B}, headers=_auth(token))
    assert r.status_code == 403
    assert any(a["result"] == "denegado:tenant_mismatch" for a in staff.auditoria)


def test_provider_id_del_query_se_ignora_si_coincide(entorno):
    # Pasar el provider_id propio está permitido; los datos siempre salen de la sesión.
    client, store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.CLERK)
    _run(store.log_med_query(1, PHARMACY_A, "paracetamol", None, None, 1))
    token = _login(client).json()["token"]
    r = client.get("/staff/analytics/top-meds", params={"provider_id": PHARMACY_A}, headers=_auth(token))
    assert r.status_code == 200 and r.json()["provider_id"] == PHARMACY_A


def test_mutacion_sin_confirmacion_409_no_muta(entorno):
    client, store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.PHARMACIST)
    _run(store.registrar_consulta_cronica(PHARMACY_A, "5211", ["hipertension"]))
    token = _login(client).json()["token"]
    r = client.post("/staff/chronic/consent", json={"waIdentity": "5211"}, headers=_auth(token))
    assert r.status_code == 409 and r.json()["error"] == "confirmation_required"
    # no mutó: sigue sin consentimiento
    pacientes = _run(store.chronic_patients(PHARMACY_A))
    assert all(p["consent"] is False for p in pacientes)
    assert any(a["result"] == "requiere_aclaracion:confirmacion_requerida" for a in staff.auditoria)


def test_mutacion_con_confirmacion_y_replay_idempotente(entorno):
    client, store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.PHARMACIST)
    _run(store.registrar_consulta_cronica(PHARMACY_A, "5211", ["hipertension"]))
    token = _login(client).json()["token"]
    # Confirmada → muta.
    r1 = client.post("/staff/chronic/consent", json={"waIdentity": "5211", "confirm": True}, headers=_auth(token))
    assert r1.status_code == 200 and r1.json()["consent"] is True
    assert all(p["consent"] is True for p in _run(store.chronic_patients(PHARMACY_A)))
    # Replay (misma petición) → idempotente, sigue consentido, auditado de nuevo.
    r2 = client.post("/staff/chronic/consent", json={"waIdentity": "5211", "confirm": True}, headers=_auth(token))
    assert r2.status_code == 200
    assert all(p["consent"] is True for p in _run(store.chronic_patients(PHARMACY_A)))
    autorizados = [a for a in staff.auditoria if a["action"] == "chronic.mark_consent" and a["result"] == "autorizado"]
    assert len(autorizados) == 2  # ambos intentos quedaron auditados


def test_N4_sesion_expirada_401(entorno):
    client, _store, staff, _ctx_ = entorno
    token = nuevo_token()
    ahora = utcnow()
    _run(staff.crear_sesion(StaffSession(
        token_hash=hash_token(token), staff_user_id="emp-x", pharmacy_id=PHARMACY_A,
        role=Role.PHARMACIST, issued_at=ahora - timedelta(hours=10),
        expires_at=ahora - timedelta(hours=1),  # ya venció
    )))
    r = client.get("/staff/analytics/top-meds", headers=_auth(token))
    assert r.status_code == 401


def test_logout_revoca_sesion(entorno):
    client, _store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.CLERK)
    token = _login(client).json()["token"]
    assert client.get("/staff/analytics/top-meds", headers=_auth(token)).status_code == 200
    assert client.post("/staff/logout", headers=_auth(token)).status_code == 200
    # tras logout, la sesión ya no vale
    assert client.get("/staff/analytics/top-meds", headers=_auth(token)).status_code == 401


def test_order_lookup(entorno):
    client, store, staff, _ctx_ = entorno
    _crear_empleado(staff, role=Role.CLERK)
    _run(store.reservar_intento(order_attempt_id="oa-1", conversation_id=1, cart_fingerprint="fp"))
    token = _login(client).json()["token"]
    r = client.get("/staff/orders/oa-1", headers=_auth(token))
    assert r.status_code == 200 and r.json()["order_attempt_id"] == "oa-1"
    assert client.get("/staff/orders/nope", headers=_auth(token)).status_code == 404


def test_superficie_no_montada_503():
    # ctx.staff None → la superficie responde 503 (no se finge disponibilidad).
    ctx = AppContext(settings=Settings(), store=MemoryStore(), crm=None, llm=None, staff=None)
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(staff_router)
    client = TestClient(app)
    assert client.post("/staff/login", json={"pharmacyId": "A", "username": "u", "secret": "s"}).status_code == 503
    assert client.get("/staff/analytics/top-meds").status_code == 503


def test_piloto_cerrado_sin_empleados_todo_denegado(entorno):
    # Sin empleados sembrados (piloto cerrado) nadie se autentica → fail-closed.
    client, _store, _staff, _ctx_ = entorno
    assert _login(client).status_code == 401
