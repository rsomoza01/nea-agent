"""Regresión de los hints opcionales de rendimiento del proveedor LLM.

Los hints están apagados por defecto. Si el proveedor rechaza reasoning/provider,
el cliente debe desactivarlos y repetir la petición sin perder la respuesta.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm import LlmExhausted, OpenAiLlm


def _reply(text: str = "respuesta") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text, tool_calls=[])
            )
        ],
        usage=None,
    )


class _FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _fake_client(outcomes):
    completions = _FakeCompletions(outcomes)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    ), completions


@pytest.mark.anyio
async def test_hints_apagados_por_defecto(monkeypatch):
    settings = Settings()
    assert settings.llm_reasoning_effort == ""
    assert settings.llm_provider_sort == ""

    llm = OpenAiLlm("key", "model")
    client, calls = _fake_client([_reply()])
    llm._client = client

    result = await llm.complete([{"role": "user", "content": "hola"}])

    assert result.content == "respuesta"
    assert "extra_body" not in calls.calls[0]


@pytest.mark.anyio
async def test_hints_se_envian_solo_si_se_configuran():
    llm = OpenAiLlm(
        "key",
        "model",
        reasoning_effort="minimal",
        provider_sort="throughput",
    )
    client, calls = _fake_client([_reply()])
    llm._client = client

    await llm.complete([{"role": "user", "content": "hola"}])

    assert calls.calls[0]["extra_body"] == {
        "reasoning": {"effort": "minimal"},
        "provider": {"sort": "throughput"},
    }
    assert "max_tokens" not in calls.calls[0]


@pytest.mark.anyio
async def test_rechazo_de_hint_desactiva_y_reintenta_sin_hints(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.llm.asyncio.sleep", no_sleep)
    llm = OpenAiLlm(
        "key",
        "model",
        reasoning_effort="minimal",
        provider_sort="throughput",
    )
    client, calls = _fake_client(
        [RuntimeError("Reasoning is not supported"), _reply("respuesta sin hints")]
    )
    llm._client = client

    result = await llm.complete([{"role": "user", "content": "hola"}])

    assert result.content == "respuesta sin hints"
    assert "extra_body" in calls.calls[0]
    assert "extra_body" not in calls.calls[1]
    assert llm._extras_ok is False


@pytest.mark.anyio
async def test_error_no_relacionado_no_desactiva_hints(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.llm.asyncio.sleep", no_sleep)
    llm = OpenAiLlm(
        "key",
        "model",
        reasoning_effort="minimal",
    )
    client, calls = _fake_client(
        [
            RuntimeError("HTTP 500 internal server error"),
            RuntimeError("HTTP 500 internal server error"),
            RuntimeError("HTTP 500 internal server error"),
        ]
    )
    llm._client = client

    with pytest.raises(LlmExhausted):
        await llm.complete([{"role": "user", "content": "hola"}])

    assert llm._extras_ok is True
    assert all("extra_body" in call for call in calls.calls)
