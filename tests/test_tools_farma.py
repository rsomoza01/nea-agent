"""Tools farmacéuticas (spec 001): buscar_medicamento, sugerir_generico, info_provider.

Verifica que el agente consulta el catálogo vía el CRM (con el providerId del
tenant) y NUNCA inventa precios (devuelve lo que el catálogo diga).
"""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.tools import ToolRuntime, active_tool_schemas
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx

PROVIDER_ID = "prov-gentefarma"


def make_ctx_farmacia():
    settings = Settings(
        _env_file=None,
        verify_token="vtoken",
        crm_base_url=CRM_URL,
        crm_webhook_url=f"{CRM_URL}/api/webhooks/wa/tok",
        crm_bot_api_key="test-key",
        openai_api_key="sk-test",
        provider_id=PROVIDER_ID,
    )
    return make_ctx(settings=settings)


def mock_products(respx_mock, *, q=None, products=None):
    route = respx_mock.get(f"{CRM_URL}/api/bot/products")
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "products": products or [],
                "missing": [] if products else ([] if q is None else [q]),
                "provider": {"providerId": PROVIDER_ID, "nombre": "Farmacia Gentefarma"},
            },
        )
    )
    return route


@pytest.fixture
async def runtime_farmacia():
    ctx = make_ctx_farmacia()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID)
    yield runtime, ctx
    await ctx.crm.aclose()


async def test_buscar_medicamento_consulta_por_provider_id(runtime_farmacia, respx_mock):
    runtime, ctx = runtime_farmacia
    route = mock_products(
        respx_mock,
        products=[{
            "productId": "p1",
            "producto": "Losartán",
            "generico": "Losartán Potásico",
            "presentacion": "Tabletas 50 mg",
            "laboratorio": "Genfar",
            "precio": 12.5,
            "disponible": True,
            "requiereReceta": False,
        }],
    )
    result = await runtime.execute("buscar_medicamento", {"nombre": "losartán"})
    assert result["ok"] is True
    assert result["products"][0]["precio"] == 12.5
    # El providerId del tenant va en la query
    assert PROVIDER_ID in route.calls.last.request.url.query.decode()

async def test_buscar_medicacion_sin_resultados_no_inventa(runtime_farmacia, respx_mock):
    runtime, ctx = runtime_farmacia
    mock_products(respx_mock, q="medicamento-inexistente", products=[])
    result = await runtime.execute("buscar_medicamento", {"nombre": "medicamento-inexistente"})
    assert result["ok"] is False
    assert result["error"] == "sin_resultados"


async def test_buscar_medicacion_sin_provider_id_devuelve_error(runtime_farmacia, respx_mock):
    # Sin providerId no hay catálogo: no debe llamar al CRM ni inventar nada.
    runtime, ctx = runtime_farmacia
    runtime._ctx.settings.provider_id = ""
    result = await runtime.execute("buscar_medicamento", {"nombre": "losartán"})
    assert result["ok"] is False
    assert result["error"] == "sin_provider"


async def test_info_provider_devuelve_farmacia(runtime_farmacia, respx_mock):
    runtime, ctx = runtime_farmacia
    route = respx_mock.get(f"{CRM_URL}/api/bot/providers").mock(
        return_value=httpx.Response(
            200,
            json={
                "provider": {
                    "providerId": PROVIDER_ID,
                    "nombre": "Farmacia Gentefarma",
                    "direccion": "Av. Principal",
                    "horario": "8:00 AM a 8:00 PM",
                    "ciudad": "Ciudad Bolívar",
                }
            },
        )
    )
    result = await runtime.execute("info_provider", {})
    assert result["ok"] is True
    assert result["provider"]["horario"] == "8:00 AM a 8:00 PM"
    assert PROVIDER_ID in route.calls.last.request.url.query.decode()


async def test_active_tool_schemas_farmacia_retira_agenda():
    schemas = active_tool_schemas(farmacia=True)
    names = {t["function"]["name"] for t in schemas}
    assert "buscar_medicamento" in names
    assert "sugerir_generico" in names
    assert "info_provider" in names
    # Las de agenda se retiran
    assert "propose_slots" not in names
    assert "book_session" not in names
    assert "route_out" not in names
    # Las transversales se mantienen
    assert "handoff" in names
    assert "update_ficha" in names
