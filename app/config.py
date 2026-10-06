"""Configuración tipada del bot (pydantic-settings).

Todas las variables se documentan en `.env.example`. Los defaults permiten
importar el módulo sin entorno (los tests inyectan valores explícitos);
la validación de lo obligatorio ocurre al arranque real.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


def canonical_identity(wa_id: str) -> str:
    """Canonicaliza una identidad de WhatsApp para comparaciones.

    México: Meta a veces reporta `521XXXXXXXXXX` (13 dígitos con el "1" de
    móvil) y a veces `52XXXXXXXXXX` — son la misma persona. Los BSUID y otros
    identificadores pasan tal cual.
    """
    s = wa_id.strip()
    if s.startswith("521") and len(s) == 13 and s.isdigit():
        return "52" + s[3:]
    return s


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Webhook de Meta
    verify_token: str = ""
    meta_app_secret: str = ""  # vacío = no se verifica la firma (dev)

    # CRM (vocero-crm, bot gateway /api/bot/*)
    crm_base_url: str = "http://localhost:3000"
    crm_webhook_url: str = ""  # incluye el segmento del verify token del CRM
    crm_bot_api_key: str = ""

    # Perfil del negocio (capa de persona; ver app/profile.py)
    agent_name: str = "Nea"  # se usa si el CRM no define nombre
    agent_timezone: str = "America/Caracas"  # IANA; fechas del prompt (Venezuela)
    brief_path: str = ""  # markdown local, fallback si el CRM no tiene perfil

    # Farmacia (rol farmacéutico, spec 001): providerId del tenant en Firebase.
    # Cada instancia atiende a UNA farmacia con su propio catálogo (aislamiento).
    provider_id: str = ""

    # LLM
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_transcribe_model: str = "whisper-1"  # notas de voz → texto
    # Proveedor OpenAI-compatible (OpenRouter, Ollama, etc.). Vacío = api.openai.com
    openai_base_url: str = ""
    # Proveedor SEPARADO para transcripción de audio (Groq, etc.): OpenRouter
    # no expone /audio/transcriptions (403). Vacío = usa el mismo cliente del chat.
    transcribe_api_key: str = ""
    transcribe_base_url: str = ""  # p.ej. https://api.groq.com/openai/v1
    history_window: int = 10

    # Guardarraíles y tiempos
    allowed_wa_ids: str = ""
    # Identidades que pueden usar el comando /reset. Va SEPARADA de
    # allowed_wa_ids a propósito: en producción esa lista va vacía (el agente
    # atiende a todos los leads), y cuando el /reset colgaba de ella el
    # comando quedaba muerto justo donde hace falta — para correr una ronda
    # de pruebas en vivo había que cerrarle la puerta a los leads reales.
    tester_wa_ids: str = ""  # CSV; vacía = responde a todos (Constitución V)
    # Ventana de agrupación de ráfagas (debounce). Cada mensaje nuevo REINICIA el
    # reloj; al vencer, la ráfaga entera se procesa en UN solo turno.
    #
    # POR QUÉ 8 s Y NO 4: medido contra las ráfagas reales de la BD del CRM, con
    # 4 s el caso reportado salía en 2 turnos y otro caso real (mensajes a +6,4 s,
    # +7,7 s y +5,5 s) en 4 turnos. Los 8 s son la ventana MÍNIMA que agrupa
    # ambas ráfagas en un único turno.
    #
    # El coste (un mensaje SOLITARIO espera 8 s antes de que arranque su turno) se
    # mitiga con la señal de vida inmediata ("escribiendo…"): la latencia medida
    # hoy ya es mediana 5,1 s / p75 13,3 s, y el 87 % de los mensajes seguidos
    # llegan a más de 4 s de distancia (mediana 29 s), así que la mayoría NO son
    # ráfagas y no cambian de comportamiento.
    coalesce_seconds: float = 8.0
    # Seguimiento automático ("empujón" a las N horas si el lead se calla).
    #
    # ENCENDIDO, pero con dos condiciones que antes no tenía (ver `followup.py`):
    #   1. SOLO si el pedido NO se cerró (`cart_closed = TRUE`) — es decir, si la
    #      conversación no se convirtió en venta. Si el cliente ya finalizó el pedido
    #      (o hubo handoff / se le agendó / no calificó), el seguimiento no se agenda:
    #      insistirle a quien ya compró molesta.
    #   2. Solo dentro del horario del negocio (ver `followup_hour_start/end`). Medido
    #      antes de esto: 12 de 40 empujones salieron fuera de las 8-20 h, uno a las 4
    #      de la mañana. Un mensaje que despierta al cliente es peor que el silencio.
    followup_enabled: bool = True
    followup_hours: float = 4.0
    # Franja horaria (hora local del negocio) en la que SÍ se permite el empujón.
    followup_hour_start: int = 9
    followup_hour_end: int = 19
    # Días en los que SÍ se permite. 0=lunes … 6=domingo. Vacío = todos los días.
    # Por defecto de lunes a sábado: el domingo la farmacia está cerrada y un
    # seguimiento que no se puede atender solo genera frustración.
    followup_dias: str = "0,1,2,3,4,5"
    # Antigüedad máxima del pedido para seguirlo (horas).
    #
    # NO se usa `cart_session_hours` aquí: esa ventana (2 h en producción) gobierna el
    # carrito OPERATIVO y es MÁS CORTA que el propio seguimiento (4 h), así que el pedido
    # ya habría "expirado" justo cuando toca empujarlo → la condición nunca se cumplía y
    # no se enviaba ningún seguimiento. Medido: 0 candidatas de 127 conversaciones.
    #
    # 48 h: cubre de sobra el empujón de las 4 h y descarta el pedido abandonado hace
    # semanas (a esas alturas el cliente ya resolvió de otra forma y molestarlo quema).
    followup_max_age_hours: float = 48.0
    # Ventana de sesión del carrito (horas): los ítems que no se tocan en este
    # tiempo se descartan — el carrito no acumula medicamentos de sesiones
    # anteriores del mismo chat.
    cart_session_hours: float = 12.0
    # "Escribiendo…" casi inmediato al recibir un mensaje (antes del coalesce).
    typing_delay_seconds: float = 0.5

    # Infra
    database_url: str = ""
    port: int = 8000

    # Langfuse (observabilidad + evals). Vacío = instrumentación desactivada
    # (no-op): el agente funciona igual sin Langfuse. Self-hosted en Railway.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""  # p.ej. https://langfuse-production-xxxx.up.railway.app

    # Desarrollo: loguear el JSON crudo de mensajes no-texto entrantes para
    # capturar los formatos reales de Meta (spec 002). Apagar al terminar.
    capture_payloads: bool = False

    @staticmethod
    def _identities(csv: str) -> frozenset[str]:
        return frozenset(
            canonical_identity(part) for part in csv.split(",") if part.strip()
        )

    @property
    def allowed_identities(self) -> frozenset[str]:
        """Allowlist canonicalizada; vacía = sin restricción."""
        return self._identities(self.allowed_wa_ids)

    @property
    def tester_identities(self) -> frozenset[str]:
        """Quién puede correr /reset. Vacía = comando apagado."""
        return self._identities(self.tester_wa_ids)
