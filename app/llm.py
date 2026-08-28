"""Capa LLM: OpenAI chat.completions con tool-calling y extracción tolerante.

Gotchas del brief que se honran aquí:
- `content` vacío con tool_calls es NORMAL (turno solo-herramientas).
- Respuesta vacía de verdad (sin content ni tool_calls) o excepción → reintento
  con backoff (2 reintentos). Agotado → `LlmExhausted` y el turno degrada en
  silencio + handoff error (Constitución IV).
- Los `arguments` de las tools pueden venir malformados: JSON inválido → {}.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI

logger = logging.getLogger("nea.llm")


class LlmExhausted(Exception):
    """El LLM falló todos los reintentos — el turno debe degradar en silencio."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LlmReply:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)

class Llm(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply: ...

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str: ...

    async def ocr_image(self, data: bytes, mime: str = "image/jpeg") -> str: ...


class OpenAiLlm:
    RETRIES = 2  # además del intento inicial

    def __init__(
        self,
        api_key: str,
        model: str,
        transcribe_model: str = "whisper-1",
        base_url: str | None = None,
        transcribe_api_key: str | None = None,
        transcribe_base_url: str | None = None,
    ) -> None:
        # base_url ≠ None → proveedor OpenAI-compatible (p. ej. OpenRouter,
        # para el bench de modelos del 002). La transcripción de audio es una
        # API de OpenAI — con OpenRouter degrada honesta (403, no existe
        # /audio/transcriptions). Si se pasa transcribe_api_key/base_url, se
        # usa un cliente SEPARADO (p. ej. Groq, gratis y compatible).
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._transcribe_model = transcribe_model
        if transcribe_api_key:
            self._transcribe_client = AsyncOpenAI(
                api_key=transcribe_api_key,
                base_url=transcribe_base_url or "https://api.openai.com/v1",
            )
        else:
            self._transcribe_client = self._client
        # Contadores de uso (para el bench de costos del 002): tokens reales
        # reportados por el proveedor, acumulados por instancia.
        self.usage = {"prompt": 0, "cached": 0, "completion": 0, "llamadas": 0}

    async def transcribe(
        self, data: bytes, mime: str, filename: str = "audio.ogg"
    ) -> str:
        """Audio → texto (API de transcripción). Vacío/fallo → LlmExhausted."""
        last_error: Exception | None = None
        content_type = (mime or "audio/ogg").split(";")[0].strip()
        for attempt in range(2):
            try:
                resp = await self._transcribe_client.audio.transcriptions.create(
                    model=self._transcribe_model,
                    file=(filename, data, content_type),
                    language="es",
                )
                text = (getattr(resp, "text", None) or "").strip()
                if text:
                    return text
                last_error = ValueError("transcripción vacía")
                logger.warning("transcribe: texto vacío, intento %d", attempt + 1)
            except Exception as exc:
                last_error = exc
                logger.warning("transcribe: fallo en intento %d: %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise LlmExhausted(str(last_error))

    async def ocr_image(self, data: bytes, mime: str = "image/jpeg") -> str:
        """Extrae el texto de una imagen (medicamento/receta) con visión.

        Usa el mismo modelo del chat (gpt-4o-mini tiene visión). Devuelve el
        texto extraído; vacío/fallo → LlmExhausted (media lo degrada honesto).
        """
        from app.media import MAX_IMAGE_BYTES  # import tardío, evita ciclo

        if len(data) > MAX_IMAGE_BYTES:
            raise LlmExhausted("imagen demasiado pesada para OCR")
        import base64

        uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                resp = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Extrae SOLO el texto legible de esta imagen de "
                                        "medicamento o receta. Devuelve: nombre del "
                                        "medicamento, principio activo y concentración "
                                        "(mg/ml), y presentación si se ve. Si es una "
                                        "receta, extrae cada medicamento en una línea. "
                                        "No inventes nada que no esté en la imagen. "
                                        "Responde en texto plano."
                                    ),
                                },
                                {"type": "image_url", "image_url": {"url": uri}},
                            ],
                        }
                    ],
                )
                choices = getattr(resp, "choices", None) or []
                msg = getattr(choices[0], "message", None) if choices else None
                content = getattr(msg, "content", None) or ""
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
                text = str(content).strip()
                # Blindaje anti-prompt: a veces el modelo responde repitiendo la
                # instrucción del OCR en vez del texto de la imagen (imagen
                # ilegible/fallo). Descartamos y reintentamos con instrucción
                # más corta; si vuelve a fallar, OCR vacío → media degrada.
                if text and (
                    "Extrae SOLO" in text
                    or "texto legible" in text
                    or "Devuelve: nombre" in text
                    or "Responde en texto plano" in text
                ):
                    last_error = ValueError("OCR devolvió el prompt (imagen no procesada)")
                    logger.warning("ocr_image: respuesta es el prompt (intento %d)", attempt + 1)
                    text = ""
                if text:
                    return text
                last_error = last_error or ValueError("OCR vacío")
                logger.warning("ocr_image: texto vacío, intento %d", attempt + 1)
            except Exception as exc:
                last_error = exc
                logger.warning("ocr_image: fallo en intento %d: %s", attempt + 1, exc)
            if attempt == 0:
                await asyncio.sleep(1.0)
        raise LlmExhausted(str(last_error))

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LlmReply:
        last_error: Exception | None = None
        for attempt in range(self.RETRIES + 1):
            try:
                kwargs: dict[str, Any] = {}
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                resp = await self._client.chat.completions.create(
                    model=self._model, messages=messages, **kwargs
                )
                u = getattr(resp, "usage", None)
                if u is not None:
                    det = getattr(u, "prompt_tokens_details", None)
                    self.usage["llamadas"] += 1
                    self.usage["prompt"] += getattr(u, "prompt_tokens", 0) or 0
                    self.usage["completion"] += getattr(u, "completion_tokens", 0) or 0
                    self.usage["cached"] += getattr(det, "cached_tokens", 0) or 0
                reply = self._parse(resp)
                # Observabilidad (Langfuse): registrar la generación en la traza
                # activa del turno. No-op si Langfuse está desactivado.
                self._record_generation(messages, reply, u)
                if reply.content or reply.tool_calls:
                    return reply
                last_error = ValueError("respuesta vacía del LLM (sin content ni tools)")
                logger.warning("llm: respuesta vacía, intento %d", attempt + 1)
            except Exception as exc:  # red, API, parseo — todo reintenta
                last_error = exc
                logger.warning("llm: fallo en intento %d: %s", attempt + 1, exc)
            if attempt < self.RETRIES:
                await asyncio.sleep(2**attempt)  # 1 s, 2 s
        raise LlmExhausted(str(last_error))

    def _record_generation(
        self, messages: list[dict[str, Any]], reply: LlmReply, usage: Any
    ) -> None:
        """Registra la generación en la traza Langfuse activa (no-op si no hay)."""
        try:
            from app.observability import current_trace

            trace = current_trace()
            if trace is None:
                return
            gen = trace.generation(
                name="llm.complete",
                model=self._model,
                input=messages,
                output={
                    "content": reply.content,
                    "tool_calls": [
                        {"name": tc.name, "arguments": tc.arguments}
                        for tc in reply.tool_calls
                    ],
                },
            )
            if usage is not None:
                gen.end(
                    usage={
                        "input": getattr(usage, "prompt_tokens", 0) or 0,
                        "output": getattr(usage, "completion_tokens", 0) or 0,
                        "total": getattr(usage, "total_tokens", 0) or 0,
                    }
                )
            else:
                gen.end()
        except Exception as exc:  # observabilidad jamás rompe el turno
            logger.debug("record_generation falló (%s)", exc)

    @staticmethod
    def _parse(resp: Any) -> LlmReply:
        """Extracción tolerante: nunca truena por formato inesperado."""
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return LlmReply(content=None)
        message = getattr(choices[0], "message", None)
        if message is None:
            return LlmReply(content=None)
        content = getattr(message, "content", None)
        if isinstance(content, str):
            content = content.strip() or None
        else:
            content = None
        tool_calls: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            if not name:
                continue
            raw_args = getattr(fn, "arguments", None) or "{}"
            args = _parse_tool_arguments(name, raw_args)
            tool_calls.append(
                ToolCall(id=getattr(tc, "id", "") or "", name=name, arguments=args)
            )
        return LlmReply(content=content, tool_calls=tool_calls)


def _parse_tool_arguments(name: str, raw: str) -> dict[str, Any]:
    """Parsea los `arguments` de una tool-call de forma tolerante.

    El LLM a veces devuelve JSON inválido (comillas rotas, texto alrededor,
    llaves duplicadas). Intentamos extraer el primer objeto JSON válido;
    si no se puede, volvemos a `{}` (con aviso, no silencioso).
    """
    if not raw or not isinstance(raw, str):
        return {}
    s = raw.strip()
    # Caso común: el JSON puro parsea directo.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (TypeError, ValueError):
        pass
    # Tolera: texto antes/después del JSON, o JSON con comillas simples.
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except (TypeError, ValueError):
            pass
        # comillas simples → convertir a dobles y reintentar
        try:
            candidate = s[start : end + 1].replace("'", '"')
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (TypeError, ValueError):
            pass
    logger.warning("llm: arguments malformados en %s — uso {}", name)
    return {}
