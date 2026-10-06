"""Seguimiento: UN empujón suave si el lead se quedó callado.

Loop asyncio cada 60 s. Un empujón a las `FOLLOWUP_HOURS` (default 4) si la
fase lo amerita (hubo conversación, sin resultado, sin handoff). `followup_sent`
se marca ANTES de enviar: a lo sumo uno, incluso con crash a media operación.
Ventana cerrada o IA pausada → se omite con log (v1 no maneja plantillas).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from app.crm import CrmConflict, CrmError
from app.llm import LlmExhausted
from app.profile import resolve_profile
from app.prompt import FOLLOWUP_INSTRUCTION, build_system_prompt
from app.state import AppContext, Conversation, utcnow
from app.turn import conversation_lock
from app.turn import _agent_tz

logger = logging.getLogger("nea.followup")


def _dia_permitido(dia: int | None, config: str) -> bool:
    """¿Se permite el seguimiento en este día de la semana? 0=lunes … 6=domingo.

    `config` es un CSV ("0,1,2,3,4,5") o vacío/None = todos los días.
    """
    if dia is None:
        return False
    texto = (config or "").strip()
    if not texto:
        return True
    try:
        permitidos = {int(x) for x in texto.split(",") if x.strip() != ""}
    except ValueError:
        # Config mal escrita: no bloquees el seguimiento por eso.
        return True
    return dia in permitidos


def _medicamentos_inventados(texto: str, carrito: list[Any]) -> list[str]:
    """Medicamentos que el texto menciona y que NO están en el pedido real.

    El LLM inventa el contenido del pedido cuando no se le da: medido en producción, con
    el carrito en 2 cajas de acetaminofén generó "1 caja de Losartán 50mg, 1 caja de
    Daflon 500mg y 1 caja de ESOZ 40mg". Enviar eso es peor que no enviar nada.

    Se compara contra los NOMBRES COMPLETOS del carrito: un nombre propio del pedido
    ("ACETAMINOFEN 650 MG X 10 TAB ELTER") cubre sus palabras, así que nombrarlo en
    cualquier forma no dispara el detector. Solo salta cuando aparece un fármaco ajeno.
    """
    if not texto:
        return []
    # Palabras que el texto puede usar libremente: nada de lo que hay en el carrito, ni
    # vocabulario de la conversación/negocio.
    permitidas: set[str] = set()
    for item in carrito:
        for campo in (getattr(item, "producto", ""), getattr(item, "presentacion", ""),
                      getattr(item, "laboratorio", "")):
            permitidas.update(_palabras(str(campo or "")))
    permitidas |= {
        "hola", "soy", "asistente", "agente", "ia", "pedido", "carrito", "listo",
        "caja", "cajas", "unidad", "unidades", "frasco", "frascos", "tubo", "tubos",
        "sobre", "sobres", "ampolla", "ampollas", "tableta", "tabletas", "capsula",
        "capsulas", "jarabe", "crema", "gel", "gotas", "solucion", "suspension",
        "mas", "más", "para", "con", "sin", "por", "tu", "tus", "su", "sus", "que",
        "qué", "como", "cómo", "cuando", "cuándo", "donde", "dónde", "todo", "toda",
        "todos", "todas", "este", "esta", "estos", "estas", "ese", "esa", "esos",
        "esas", "aquel", "aquella", "puedo", "puedes", "quieres", "quiere", "deseas",
        "necesitas", "ayudar", "ayudarte", "agregar", "cerrar", "cerrarlo", "confirmar",
        "recordar", "recordatorio", "dejar", "dejo", "deje", "dejé", "quedar", "queda",
        "quedo", "quedó", "armado", "pendiente", "disponible", "precio", "total",
        "farmacia", "medicamento", "medicamentos", "producto", "productos", "si",
        "sí", "no", "gracias", "buenas", "buenos", "dias", "días", "tardes", "noches",
        "saludo", "saludos", "cordial", "atento", "gusto", "ayudo", "ayude", "ayudé",
        "escribir", "escribeme", "escríbeme", "avisame", "avísame", "cualquier",
        "cualquiera", "cosa", "cuenta", "gusto", "mucho", "muchas", "mil", "buena",
        "buen", "hoy", "manana", "mañana", "luego", "ahora", "aun", "aún", "tambien",
        "también", "espero", "esperando", "estoy", "aqui", "aquí", "esta", "está",
        "estan", "están", "fue", "ser", "esta", "tiene", "tengo", "tienes", "tenia",
        "hacer", "hago", "hice", "hace", "hacemos", "vamos", "vas", "voy", "ver",
        "vemos", "veo", "sabes", "sabe", "se", "sé",
    }
    # Los números y las dosis NO son "medicamentos inventados": 650 mg del carrito puede
    # aparecer como "650" o "650mg".
    intrusos: list[str] = []
    for w in _palabras(texto):
        if w.isdigit() or len(w) < 5:
            continue
        if w in permitidas:
            continue
        # ¿parece un fármaco? Solo palabras con cuerpo de nombre de medicamento
        # (evita disparar con una palabra suelta de cortesía que no listé).
        if _parece_farmaco(w):
            intrusos.append(w)
    return intrusos


def _palabras(texto: str) -> list[str]:
    """Palabras normalizadas (minúsculas, sin tildes) de un texto."""
    t = texto.lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                 ("ü", "u"), ("ñ", "n")):
        t = t.replace(a, b)
    return re.findall(r"[a-z0-9]+", t)


# Sufijos/raíces de presentación, cortesía y vocabulario comercial: una palabra que los
# contenga NO es el nombre de un fármaco. Sin estos filtros, cualquier palabra larga
# ("farmacia", "cordialmente", "recordatorio") dispararía el detector y bloquearía
# seguimientos legítimos.
_NO_FARMACO_PALABRA = (
    # presentación / envase
    "mg", "ml", "mcg", "tab", "cap", "jarab", "crem", "got", "susp", "soluc",
    "ampoll", "sobr", "blister", "caja", "unidad", "frasco", "presentac",
    # marcas del catálogo
    "laborat", "drotafar", "genven", "calox", "aless", "sant", "elter", "cofasa",
    # cortesía / conversación
    "farmac", "cordial", "atent", "recordat", "agradec", "asistent", "mensaje",
    "pedido", "carrito", "precio", "disponib", "pendient", "confirm", "cerrar",
    "esperand", "cualquier", "saludo", "bienvenid", "excelent", "perfect",
    "informac", "consult", "necesit", "quier", "podem", "pued", "gustar",
    "teng", "tien", "hac", "decir", "avisar", "avisam", "escrib",
)
# Raíces de fármacos que SÍ son nombres reales: si aparece una, es un intruso seguro.
_RAICES_FARMACO = (
    "losartan", "diclofenac", "ibuprof", "omeprazol", "esomeprazol", "acetaminof",
    "paracetamol", "metformin", "atorvastat", "amoxicil", "azitromic", "cefadrox",
    "nifedipin", "amlodipin", "enalapril", "losartan", "daflon", "esoz", "leprit",
    "evigax", "atamel", "depofem", "salbutamol", "loratadin", "cetirizin",
    "ranitidin", "omeprazol", "pantoprazol", "clopidogrel", "aspirina", "warfarina",
    "levotirox", "prednison", "dexametason", "ketorolac", "tramadol", "tramal",
    "naproxeno", "ketoprofeno", "ceftriaxon", "ciprofloxac", "cloranfenicol",
    "vitamin", "complejo", "insulina", "heparina", "sulfato", "carbonato",
)


def _parece_farmaco(palabra: str) -> bool:
    """¿Esta palabra parece el nombre de un medicamento (no una de cortesía)?

    Dos criterios, ambos deliberadamente conservadores — un falso positivo aquí haría
    que NO se envíe un seguimiento legítimo, así que solo se dispara ante evidencia clara:
      (a) contiene una RAÍZ de fármaco conocida (losartan, diclofenac, ibuprof…);
      (b) es una palabra larga (≥7) que NO es presentación ni marca del catálogo.
    """
    if any(r in palabra for r in _RAICES_FARMACO):
        return True
    if len(palabra) >= 7 and not any(s in palabra for s in _NO_FARMACO_PALABRA):
        return True
    return False


class FollowupWorker:
    INTERVAL = 60.0

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            try:
                await self.tick()
            except Exception:
                logger.exception("followup: fallo en el barrido")

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        s = self._ctx.settings
        if not s.followup_enabled:
            return
        # NUNCA fuera del horario del negocio. Medido en producción, 12 de 40 empujones
        # salieron fuera de las 8-20 h (uno a las 4 de la mañana). Un mensaje que
        # despierta al cliente es peor que el silencio.
        local = now.astimezone(_agent_tz(s))
        if not (s.followup_hour_start <= local.hour < s.followup_hour_end):
            logger.debug(
                "followup: %02d h locales — fuera de la franja %d-%d, se omite",
                local.hour, s.followup_hour_start, s.followup_hour_end,
            )
            return
        # Días permitidos (0=lunes … 6=domingo). El domingo la farmacia está cerrada:
        # un seguimiento que no se puede atender solo genera frustración.
        if not _dia_permitido(local.weekday(), s.followup_dias):
            logger.debug("followup: %s — día no permitido, se omite",
                         local.strftime("%A"))
            return
        for conv in await self._ctx.store.due_followups(now):
            # Claim atómico ANTES de enviar: jamás un segundo empujón.
            if not await self._ctx.store.claim_followup(conv.id):
                continue
            try:
                # Mismo candado que los turnos: sin él, el empujón puede salir
                # encimado con la respuesta de un turno vivo (dos mensajes del
                # agente a la vez). El valor se copia ANTES del candado porque
                # el Store puede devolver el mismo objeto vivo (MemoryStore) y
                # compararlo contra sí mismo no detectaría nada.
                ultimo_inbound = conv.last_inbound_at
                async with conversation_lock(self._ctx, conv.wa_identity):
                    await self._push(conv, ultimo_inbound)
            except Exception:
                logger.exception(
                    "followup de %s falló — queda consumido (a lo sumo uno)",
                    conv.wa_identity,
                )

    async def _push(
        self, conv: Conversation, ultimo_inbound: datetime | None
    ) -> None:
        ctx = self._ctx
        # Si mientras esperábamos el candado el lead escribió, el empujón sobra:
        # ya hay conversación viva y "¿seguimos?" quedaría fuera de lugar.
        fresca = await ctx.store.get_or_create_conversation(conv.wa_identity)
        if fresca.last_inbound_at != ultimo_inbound:
            logger.info(
                "followup %s: el lead escribió mientras tanto — omitido",
                conv.wa_identity,
            )
            return
        try:
            context = await ctx.crm.get_context(conv.wa_identity)
        except CrmError as exc:
            logger.warning("followup %s: CRM inaccesible (%s) — omitido", conv.wa_identity, exc)
            return
        if context is None:
            logger.warning("followup %s: sin contexto — omitido", conv.wa_identity)
            return
        info = context.get("conversation") or {}
        crm_conv_id = info.get("id") or conv.crm_conversation_id
        if not crm_conv_id:
            logger.warning("followup %s: sin conversationId — omitido", conv.wa_identity)
            return
        if not info.get("aiEnabled", False):
            logger.info("followup %s: IA pausada — omitido", conv.wa_identity)
            return
        if not info.get("windowOpen", False):
            logger.info(
                "followup %s: ventana de 24 h cerrada — omitido con registro",
                conv.wa_identity,
            )
            return

        # LA CONDICIÓN DE NEGOCIO: el empujón es para el PEDIDO que no se cerró, no un
        # "¿sigues ahí?" genérico. Se omite si:
        #   · el cliente YA cerró el pedido (`cart_closed`) → ya compró, insistirle molesta
        #   · el carrito está VACÍO → no hay pedido a retomar
        # Antes esto no existía: el seguimiento se agendaba con CUALQUIER turno enviado
        # (una consulta de precio, un "gracias", una reserva de demo), así que llegaba
        # a quien nunca mostró intención de comprar. Medido: 40 empujones, y el que
        # reportó el negocio era sobre una RESERVA DE DEMO, no un pedido.
        #
        # `cart_closed` es un flag LOCAL de bot_conversation (no viene del CRM): se
        # activa solo en finalizar_pedido. Es la marca fiable de "esto fue una venta".
        if getattr(conv, "cart_closed", False):
            logger.info(
                "followup %s: el pedido YA se cerró (venta hecha) — omitido",
                conv.wa_identity,
            )
            return
        # Antigüedad del pedido: se usa `followup_max_age_hours` (48 h), NO la ventana
        # del carrito operativo (2 h en producción). Con la ventana operativa el pedido
        # ya había expirado justo cuando toca el empujón de las 4 h → 0 seguimientos.
        carrito = await ctx.store.cart_items(
            conv.id, session_hours=ctx.settings.followup_max_age_hours
        )
        if not carrito:
            logger.info(
                "followup %s: sin pedido armado — no es seguimiento de pedido, omitido",
                conv.wa_identity,
            )
            return

        system = build_system_prompt(
            profile=await resolve_profile(ctx, str(crm_conv_id)),
            context=context,
            conv=conv,
            offered=[],
            tz=_agent_tz(ctx.settings),
        )
        # EL PEDIDO REAL, en texto, y la instrucción de retomarlo. Sin esto el LLM NO
        # sabe qué quedó en el carrito e INVENTA el contenido: probado en producción, con
        # 2 cajas de acetaminofén en el carrito el seguimiento hablaba de "1 caja de
        # Losartán 50mg, 1 caja de Daflon 500mg y 1 caja de ESOZ 40mg" — tres
        # medicamentos que el cliente NUNCA pidió. Un seguimiento que describe mal el
        # pedido es peor que no enviarlo: el cliente cree que compró otra cosa.
        pedido_txt = "\n".join(
            f"- {i.cantidad} x {i.producto}" + (f" ({i.presentacion})" if i.presentacion else "")
            for i in carrito
        )
        history = await ctx.store.recent_messages(
            conv.id, ctx.settings.history_window
        )
        messages: list[dict[str, Any]] = (
            [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in history]
            + [{
                "role": "system",
                "content": (
                    "PEDIDO QUE EL LEAD DEJÓ ARMADO (datos EXACTOS del carrito, no los "
                    "inventes ni los cambies):\n" + pedido_txt + "\n\n"
                    + FOLLOWUP_INSTRUCTION
                ),
            }]
        )
        try:
            reply = await ctx.llm.complete(messages, tools=None)
        except LlmExhausted as exc:
            logger.warning("followup %s: LLM agotado (%s) — omitido", conv.wa_identity, exc)
            return
        text = (reply.content or "").strip()
        if not text:
            logger.warning("followup %s: LLM sin texto — omitido", conv.wa_identity)
            return
        # BACKSTOP DETERMINISTA: el prompt ya prohíbe inventar el pedido, pero en este
        # proyecto el prompt no es garantía (probado: inventó Losartán/Daflon/ESOZ con un
        # carrito de acetaminofén). Si el texto nombra un medicamento que NO está en el
        # carrito, se descarta el mensaje: un seguimiento que describe mal el pedido es
        # peor que no enviarlo — el cliente creería que compró otra cosa.
        intrusos = _medicamentos_inventados(text, carrito)
        if intrusos:
            logger.warning(
                "followup %s: el texto nombra medicamentos que NO están en el pedido "
                "(%s) — NO se envía",
                conv.wa_identity, ", ".join(intrusos[:3]),
            )
            return
        # Y no puede terminar en pregunta (regla del negocio).
        if text.rstrip().endswith("?"):
            logger.warning(
                "followup %s: el texto termina en pregunta — NO se envía", conv.wa_identity
            )
            return
        try:
            await ctx.crm.send_message(str(crm_conv_id), text)
        except CrmConflict as exc:
            logger.info("followup %s: bloqueado por el CRM (%s)", conv.wa_identity, exc.code)
            return
        except CrmError as exc:
            logger.warning("followup %s: envío falló (%s)", conv.wa_identity, exc)
            return
        await ctx.store.add_message(conv.id, "assistant", text)
        logger.info("followup %s: empujón único enviado", conv.wa_identity)
