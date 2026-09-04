"""Herramientas del LLM: update_ficha, propose_slots, book_session, route_out, handoff.

La validación es server-side: `book_session` SOLO acepta slots previamente
ofrecidos (tabla offered_slots, comparación por epoch exacto). Un fallo del
CRM dentro de una tool regresa `{"ok": false, ...}` al LLM — nunca tumba el
turno.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.crm import CrmConflict, CrmError, SlotTaken

# Palabras que revelan que el LLM alucinó una frase como término de búsqueda
# (backstops del prompt, mensajes de "unsupported", instrucciones, etc.).
_RUIDO_BUSQUEDA = re.compile(
    r"\b(unsupported|contenido|honesta|pidele|pídele|nota\s+de\s+voz|"
    r"lead\s+mando|mand[oó]\s+una\s+imagen|imagen\s+adjunta|puedes\s+ver|"
    r"tipo\s+de\s+contenido|consultan|solicitud|transcripci[oó]n|"
    r"disponibilidad|invent|cat[aá]logo\s+de\s+medicamento)\b",
    re.I,
)


def _termino_busqueda_plausible(term: str) -> bool:
    """Filtra términos basura que NO deben registrarse en med_queries.

    Un término plausible de medicamento es corto y sin palabras de ruido:
    'losartan 50', 'nifedipina 10 mg'. Un término basura suele ser una frase
    alucinada del LLM (backstop/OCR-instrucciones): 'lead mando contenido
    puedes ver tipo unsupported honesta pidele texto nota voz'.
    """
    t = (term or "").strip().lower()
    if not t:
        return False
    # Ruido explícito de alucinación / instrucciones del prompt.
    if _RUIDO_BUSQUEDA.search(t):
        return False
    # Un medicamento plausible tiene pocas palabras (marca + dosis + forma).
    tokens = re.findall(r"[a-záéíóúñü0-9.,]+(?:/[a-záéíóúñü0-9.,]+)?", t)
    if len(tokens) > 6:
        return False
    return True


# Saludos y cortesía que NUNCA son un medicamento. Si el LLM llama
# buscar_medicamento con un término que es SOLO esto (p. ej. "saludos",
# "buen día"), es un error del modelo: no hay que buscar en el catálogo ni
# devolver una lista de productos irrelevantes. Se responde con un saludo.
_SALUDOS = {
    "hola", "buenas", "buen", "buenos", "buena", "buen dia", "buenos dias",
    "buenas tardes", "buenas noches", "saludos", "saludo", "que tal", "que tal",
    "como estas", "como esta", "como estas", "como va", "epa", "hey", "ey",
    "holi", "holis", "buen dia", "buenas", "saludos cordiales", "cordial",
    "gracias", "por favor", "favor", "ok", "okey", "okay", "vale", "listo",
    "perfecto", "genial", "excelente", "bien", "bueno", "buena", "si", "no",
    "hola buenas", "hola buenos dias", "hola buenas tardes", "buen dia saludos",
}


def _es_solo_saludo(term: str) -> bool:
    """True si el término es SOLO saludos/cortesía (no un medicamento).

    'saludos' → True. 'losartan 50' → False. 'buen dia, saludos' → True.
    Normaliza a minúsculas, quita tildes y puntuación; compara contra _SALUDOS.
    """
    t = (term or "").strip().lower()
    if not t:
        return False
    # Quitar tildes (el set _SALUDOS está sin tildes: 'dia', 'como estas').
    t = t.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    # Quitar puntuación y normalizar espacios.
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    # El término completo es un saludo conocido.
    if t in _SALUDOS:
        return True
    # Todas las palabras son saludos/cortesía (p. ej. "buen dia saludos").
    palabras = set(t.split())
    return bool(palabras) and palabras <= _SALUDOS


# Palabras funcionales del español que NO son parte de un medicamento. Usadas
# por _termino_es_medicamento_plausible para rechazar frases enteras del
# cliente que el backstop intenta buscar como si fueran medicamentos (p. ej.
# 'caja cada uno', 'medicamento llega vencido cambian', 'van responder').
_PALABRAS_FUNCIONALES = {
    "van", "vamos", "respondo", "responder", "respuesta", "necesito",
    "quiero", "quieres", "quiere", "busco", "busca", "buscan", "buscar",
    "tienes", "tiene", "tienen", "tener", "hay", "es", "son", "estan", "esta",
    "estoy", "del", "dela", "al", "que", "cual", "como", "cuando", "donde",
    "me", "mi", "tu", "te", "se", "lo", "la", "los", "las", "le", "les", "nos",
    "uno", "una", "unos", "unas", "para", "por", "con", "sin", "sobre", "hasta",
    "cada", "todo", "toda", "todos", "todas", "algo", "alguien", "nada", "nadie",
    "ello", "este", "esta", "esto", "estos", "estas", "ese", "esa", "eso",
    "caja", "cajas", "unidad", "unidades", "blister", "paquete", "compra",
    "comprar", "venden", "precio", "precios", "cuanto", "cuesta", "cuestan",
    "disponible", "disponibles", "tengo", "tienen", "traen", "mande", "dime",
    "diga", "dian", "digas", "puedes", "puede", "podrias", "podria", "favor",
    "gracias", "solo", "sola", "solamente", "mas", "menos", "mucho", "mucha",
    "bueno", "buena", "bien", "porfavor", "okey", "ok", "vale", "listo",
    "medicamento", "medicamentos", "consulta", "consultar", "opcion", "opciones",
    "economico", "economica", "barato", "barata", "costo", "oferta",
    # Verbos/nombres de FRASE del cliente (reclamos, garantías, entregas).
    "van", "responda", "responden", "respondan", "solucion", "solucionar",
    "arreglar", "llega", "llegar", "llegue", "vencio", "vencido", "vencida",
    "cambio", "cambian", "cambien", "cambiar", "controlado", "controlada",
    "manejan", "maneja", "manejar", "receta", "recetas", "abuela", "domicilio",
    "entrega", "entregar", "domingo", "domingos", "hacen", "hacer", "hace",
    "quieren", "pienso", "espero", "problema", "problemas", "pasar",
    "generico", "generica", "marca", "presentacion", "mas", "mejor",
    # Muletillas venezolanas/mexicanas de arranque que NO son fármaco.
    "oiga", "oigan", "epa", "hey", "ey", "mira", "miren", "che", "wey", "vale",
    "epa", "eh", "ah", "uy",
    # Conceptos que NO son medicamentos (preguntas generales de precios,
    # comparadores, catálogos, servicios). Si el término se reduce a esto, no
    # es una consulta de medicamento.
    "comparador", "comparadores", "catalogo", "catalogos", "listado", "lista",
    "precios", "precio", "tarifa", "tarifas", "servicio", "servicios", "info",
    "informacion", "ayuda", "ayudar", "atencion", "atender", "contacto",
    "contactar", "horario", "horarios", "ubicacion", "direccion", "telefono",
    "whatsapp", "web", "pagina", "tienda", "farmacia", "negocio", "producto",
    "productos", "stock", "inventario", "disponibilidad", "existencias",
    # Conceptos de negocio/contrato/chat que NO son medicamentos. Un mensaje
    # como "mañana conversamos para dar inicio formal del contrato de la
    # página y el chat y el comparador" NO es una receta.
    "chat", "contrato", "contratos", "conversamos", "conversar", "inicio",
    "formal", "pagina", "paginas", "mañana", "manana", "dar", "damos",
    # Verbos/estados de consulta general que no son fármaco.
    "interesada", "interesado", "interes", "indicar", "indica", "indicame",
    "saber", "sabes", "conocer", "conozco", "averiguar", "consultar", "preguntar",
    "pregunta", "quiero", "quisiera", "necesito", "buscar", "buscando",
}

# Palabras que delatan un ACCESORIO/insumo médico, NO un medicamento. El
# fallback por principio activo (p. ej. 'lantus' → 'insulina') puede devolver
# accesorios (jeringas, agujas, tiras) que NO son el fármaco que el cliente
# pidió. Si tras filtrar solo quedan accesorios, el agente debe decir
# honestamente que el medicamento no está disponible y escalar, en vez de
# ofrecer una jeringa como si fuera la respuesta a 'Lantus'.
_ACCESORIOS_MEDICOS = {
    "jeringa", "jeringas", "aguja", "agujas", "tira", "tiras", "glucotest",
    "glucómetro", "glucometro", "lanceta", "lancetas", "tirilla",
    "tirillas", "test", "prueba", "pruebas",
}


def _termino_es_medicamento_plausible(term: str) -> bool:
    """True si el término parece un medicamento, no una frase del cliente.

    Es un filtro conservador para NO disparar el fallback por principio activo
    (que adivina con el LLM) ni consultar el catálogo con basura. Un término
    plausible de medicamento: corto (≤4 palabras), sin verbos funcionales que
    lo llenen de contexto ('caja cada uno'), con al menos una palabra de ≥3
    letras que NO sea funcional. 'losartan 50' → True. 'caja cada uno' → False.
    'medicamento llega vencido cambian' → False. 'van responder que solucion' →
    False. 'panadol' → True.
    """
    t = (term or "").strip().lower()
    if not t:
        return False
    # Saludos solos no son medicamentos.
    if _es_solo_saludo(t):
        return False
    # Quitar tildes para comparar contra las funcionales (sin tildes).
    t_sin = t.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    palabras = re.findall(r"[a-z0-9]+", t_sin)
    if not palabras:
        return False
    # Más de 4 palabras → probablemente una frase, no un medicamento.
    if len(palabras) > 4:
        return False
    # Contar palabras "sustantivas" (no funcionales).
    sustantivas = [w for w in palabras if w not in _PALABRAS_FUNCIONALES and len(w) >= 3]
    if not sustantivas:
        return False
    return True


def _limpiar_termino_medicamento(term: str) -> str:
    """Deja SOLO las palabras "sustantivas" (posible fármaco) del término.

    Quita TODAS las palabras funcionales/relleno en cualquier posición (no solo
    al inicio como _quitar_saludos): 'genérico del daflon económico' →
    'daflon'; 'cajas opción económica 50 mg' → '50' (sin sustantivo). Devuelve
    '' si no queda ninguna palabra sustantiva de ≥3 letras.
    """
    t = (term or "").strip().lower()
    if not t:
        return ""
    t_sin = t.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    palabras = re.findall(r"[a-z0-9]+", t_sin)
    sustantivas = [
        w for w in palabras if w not in _PALABRAS_FUNCIONALES and len(w) >= 3
    ]
    if not sustantivas:
        return ""
    return " ".join(sustantivas)


def _quitar_saludos(term: str) -> str:
    """Quita los saludos/cortesía del INICIO de un término de búsqueda.

    El LLM a veces deja el saludo pegado al medicamento ('epa panadol',
    'hola losartan'), y ese saludo falsea la búsqueda (epa → EPAX, hola →
    ...). Quita las palabras iniciales que sean saludos/cortesía O verbos de
    consulta, devolviendo el resto. 'epa panadol' → 'panadol'.
    'buenos dias, quiero daflon' → 'daflon'. Devuelve '' si todo era ruido.
    """
    t = (term or "").strip().lower()
    if not t:
        return ""
    # Quitar tildes para comparar contra _SALUDOS (sin tildes).
    t_sin = t.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    tokens = re.findall(r"[a-z0-9]+", t_sin)
    # Ruido inicial que quitar: saludos/cortesía + verbos de consulta comunes.
    ruido = _SALUDOS | {
        # Palabras sueltas de saludos compuestos ('buenos dias', 'buenas tardes').
        "dia", "dias", "tardes", "noches", "mañana", "tarde", "buenos", "buenas",
        "tienes", "tiene", "tengan", "tienen", "hay", "hay", "venden", "vendes",
        "quiero", "quiere", "quieres", "quería", "quisiera", "necesito", "busco",
        "buscando", "buscar", "busca", "buscan", "consiguen", "consigues",
        "conseguir", "me", "dan", "dame", "da", "saber", "cuanto", "cuesta",
        "cuestan", "precio", "disponible", "disponibles", "traen", "mande",
    }
    for w in tokens:
        if w in ruido:
            continue
        # Primera palabra que NO es ruido: cortar el término a partir de ella.
        idx = t.find(w)
        return t[idx:].strip()
    return ""
from app.profile import BusinessProfile
from app.state import AppContext, Conversation, OfferedSlot

logger = logging.getLogger("nea.tools")

# Tools de AGENDA (rol original de Nea). En el rol farmacéutico (spec 001) se
# retiran: el agente consulta disponibilidad/precio, no agenda citas. Se
# mantienen en el schema por compatibilidad, pero `active_tool_schemas()`
# filtra cuáles se exponen al LLM según el rol.
AGENDA_TOOLS = frozenset({"propose_slots", "book_session", "reschedule_session", "route_out"})

# Tools que se exponen SIEMPRE (transversales).
CORE_TOOLS = frozenset({"update_ficha", "handoff"})

# Tools del rol farmacéutico (spec 001).
FARMACIA_TOOLS = frozenset(
    {
        "buscar_medicamento",
        "sugerir_generico",
        "info_provider",
        "agregar_al_carrito",
        "ver_carrito",
        "finalizar_pedido",
    }
)


def active_tool_schemas(*, farmacia: bool = False) -> list[dict[str, Any]]:
    """Schemas de tools que se exponen al LLM en este turno.

    - Rol farmacia: se quitan las de agenda (propose/book/reschedule/route_out),
      se mantienen las transversales (update_ficha, handoff) y se añaden las de
      catálogo (buscar_medicamento, sugerir_generico, info_provider).
    - Rol agenda (default): schema completo (compatibilidad con el rol original).
    """
    if not farmacia:
        return list(TOOL_SCHEMAS)
    return [
        t
        for t in TOOL_SCHEMAS
        if t["function"]["name"] in CORE_TOOLS or t["function"]["name"] in FARMACIA_TOOLS
    ]

# Cuántos huecos quedan RESERVABLES tras un propose_slots. El agente muestra 3
# a la vez (regla del prompt), pero guardar solo 3 lo dejaba sin nada que
# ofrecer cuando el lead pedía otro día: el catálogo reservable es más ancho
# que el menú que se enseña.
MAX_OFFERED = 12
# Reparto pedido al CRM: hasta 3 huecos por día, en 5 días distintos.
OFFER_PER_DAY = 3
OFFER_DAYS = 5

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "update_ficha",
            "description": (
                "Guarda o actualiza la ficha del lead en el CRM (merge: solo los "
                "campos que mandes). Llámala en cuanto descubras un dato nuevo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rubro": {"type": "string"},
                    "rol": {
                        "type": "string",
                        "description": "dueno | hijo_del_dueno | empleado | otro",
                    },
                    "tamano_aprox": {"type": "string"},
                    "sistemas": {"type": "string"},
                    "dolor_principal": {"type": "string"},
                    "geo": {"type": "string"},
                    "calificado": {"type": "boolean"},
                    "resultado": {
                        "type": "string",
                        "description": "agendo | dio_diy | handoff | sin_respuesta",
                    },
                    "notas": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_slots",
            "description": (
                "Consulta la disponibilidad real de la agenda del negocio. Te "
                "regresa los huecos libres REPARTIDOS entre los próximos días, "
                "cada uno con su día en palabras (hoy/mañana/nombre del día). "
                "Ofrece al lead máximo 3, los que embonen con lo que pidió. Si "
                "el día que pidió no aparece, es que no hay agenda ese día: "
                "dilo. SOLO estos horarios serán reservables después."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_session",
            "description": (
                "Reserva la cita en uno de los horarios previamente ofrecidos. "
                "start_utc debe ser EXACTAMENTE el start_utc de un slot ofrecido "
                "en esta conversación. Llámala SOLO después de haber nombrado el "
                "día completo y de que el lead lo aceptara sin ambigüedad."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC del slot elegido, tal cual se ofreció",
                    },
                    "dia_confirmado": {
                        "type": "string",
                        "description": (
                            "Lo que el lead escribió para aceptar ESE día concreto. "
                            "Si no puedes citarlo, todavía no confirmó: pregunta "
                            "en vez de reservar."
                        ),
                    },
                },
                "required": ["start_utc", "dia_confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_session",
            "description": (
                "Mueve la cita YA agendada del lead a otro horario ofrecido. "
                "Mismo protocolo que book_session: primero propose_slots, luego "
                "confirmas el día completo, y hasta entonces mueves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC del nuevo slot, tal cual se ofreció",
                    },
                    "dia_confirmado": {
                        "type": "string",
                        "description": "Lo que el lead escribió para aceptar ESE día",
                    },
                },
                "required": ["start_utc", "dia_confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "route_out",
            "description": (
                "Marca al lead como no calificado (hoy). Después despídete con "
                "honestidad, compartiendo los recursos alternativos del negocio "
                "si existen, puerta abierta."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff",
            "description": (
                "Pasa la conversación a un humano del negocio y pausa la IA. Tu "
                "mensaje de despedida se envía ANTES de la pausa — salvo en el "
                "handoff por hostilidad, donde cierras sobrio sin anunciarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo breve (p.ej. 'pidió humano', 'duda fuera del conocimiento')",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_medicamento",
            "description": (
                "Consulta la disponibilidad y el precio de un medicamento en el "
                "catálogo de la farmacia. Devuelve el producto con su precio y si "
                "está disponible. Llámala SOLO cuando el cliente pida un "
                "medicamento CONCRETO por su nombre (p. ej. 'losartán', 'daflon "
                "500', 'paracetamol'). NO la llames para preguntas generales de "
                "precios, comparadores, catálogos completos, saludos, reclamos u "
                "off-topic: si el cliente no nombra un medicamento específico, "
                "responde directamente sin usar esta herramienta. NUNCA inventes "
                "precios: si no aparece aquí, no está en el catálogo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {
                        "type": "string",
                        "description": "Nombre del medicamento a buscar (p. ej. 'losartán 50 mg')",
                    },
                },
                "required": ["nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sugerir_generico",
            "description": (
                "Busca alternativas genéricas de un medicamento en el catálogo de "
                "la farmacia. Úsala para ofrecer la opción más económica cuando el "
                "cliente lo pida o cuando tenga sentido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {
                        "type": "string",
                        "description": "Nombre del medicamento del que se buscan genéricos",
                    },
                },
                "required": ["nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "info_provider",
            "description": (
                "Devuelve la información de la farmacia: dirección, horario y "
                "ciudad. Úsala cuando el cliente pregunte dónde está la farmacia, "
                "su horario o su ubicación."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agregar_al_carrito",
            "description": (
                "Añade (o incrementa) un medicamento al pedido del cliente, con "
                "su cantidad. Producto y precios deben venir EXACTAMENTE de un "
                "resultado previo de buscar_medicamento (productId/precio/..."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "productId": {
                        "type": "string",
                        "description": "productId del producto del catálogo (de buscar_medicamento)",
                    },
                    "producto": {
                        "type": "string",
                        "description": "Nombre del medicamento (producto del catálogo)",
                    },
                    "presentacion": {"type": "string", "description": "Presentación (opcional)"},
                    "laboratorio": {"type": "string", "description": "Laboratorio/marca (opcional)"},
                    "cantidad": {
                        "type": "integer",
                        "description": "Cuántas cajas/unidades quiere (mínimo 1)",
                    },
                    "precioUsd": {"type": "number", "description": "Precio unitario en USD"},
                    "precioBs": {"type": "number", "description": "Precio unitario en Bs"},
                },
                "required": ["productId", "producto", "cantidad", "precioUsd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ver_carrito",
            "description": (
                "Devuelve el resumen del pedido actual del cliente: productos, "
                "cantidades, precios y total (USD y Bs). Úsala cuando el cliente "
                "quiera ver su pedido o al cerrar la compra."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "actualizar_cantidad",
            "description": (
                "Corrige la cantidad EXACTA de un medicamento ya agregado al "
                "pedido cuando el cliente la cambia (p. ej. 'solo quiero 3 "
                "cajas'). NO suma: reemplaza la cantidad del producto por la "
                "nueva. Producto y precios deben venir del resultado previo de "
                "buscar_medicamento (productId)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "productId": {
                        "type": "string",
                        "description": "productId del producto del catálogo (de buscar_medicamento)",
                    },
                    "cantidad": {
                        "type": "integer",
                        "description": "Nueva cantidad exacta de cajas/unidades (mínimo 1)",
                    },
                },
                "required": ["productId", "cantidad"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalizar_pedido",
            "description": (
                "Registra el pedido del carrito en el CRM como nota de la "
                "conversación y lo marca como listo para que un humano lo "
                "procese. Después limpia el carrito. Úsala SOLO cuando el "
                "cliente haya confirmado que NO quiere agregar más productos."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _label_of(raw: dict[str, Any], start: datetime) -> str:
    """Etiqueta con el día en palabras: "hoy viernes 7 de agosto, 10:30".

    La corta del CRM ("vie 7 ago, 10:30") se presta a que el lead entienda
    otro día: basta que conteste "10:30, de mañana" a una oferta de HOY para
    agendar mal. Si el CRM no manda `dayLabel` (respuestas sin reparto, p. ej.
    las alternativas de un slot_taken), se cae a la corta.
    """
    day_label = str(raw.get("dayLabel") or "").strip()
    time = str(raw.get("time") or "").strip()
    if day_label and time:
        return f"{day_label}, {time}"
    return str(raw.get("label") or _iso_z(start))


def _slots_from_payload(
    conversation_id: int, raw_slots: list[dict[str, Any]]
) -> list[OfferedSlot]:
    """Convierte slots del CRM ({startUtc,endUtc,label}) a OfferedSlot, tolerante."""
    out: list[OfferedSlot] = []
    for raw in raw_slots[:MAX_OFFERED]:
        start = _parse_utc(str(raw.get("startUtc") or ""))
        if start is None:
            continue
        end = _parse_utc(str(raw.get("endUtc") or "")) if raw.get("endUtc") else None
        out.append(
            OfferedSlot(
                conversation_id=conversation_id,
                start_utc=start,
                end_utc=end,
                label=_label_of(raw, start),
            )
        )
    return out


def _slots_for_llm(slots: list[OfferedSlot]) -> list[dict[str, str]]:
    return [{"start_utc": _iso_z(s.start_utc), "label": s.label} for s in slots]


# -------------------------------------------------------- farmacia: helpers ---
# Extraen el miligramo (mg) y la marca de los productos devueltos por el CRM,
# para que la IA detecte cuándo un principio activo tiene varias presentaciones
# y decida preguntar (más amigable/preciso) en vez de listar un grupo grande.

_MG_RE = re.compile(r"(\d+)\s*(?:mg|miligramo)", re.IGNORECASE)


def _extraer_miligramos(products: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in products:
        nombre = f"{p.get('producto') or ''} {p.get('presentacion') or ''}"
        m = _MG_RE.search(nombre)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(f"{m.group(1)} mg")
    return out


def _extraer_marcas(products: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in products:
        marca = str(p.get("laboratorio") or "").strip()
        if marca and marca.lower() not in seen:
            seen.add(marca.lower())
            out.append(marca)
    return out


def _fmt_ve(num: float | int | None) -> str:
    """Formato venezolano: coma decimal y punto de miles (1.234,56)."""
    if not isinstance(num, (int, float)):
        return "—"
    s = f"{num:,.2f}"  # 1,234.56 (estilo US)
    # intercambiar coma y punto: 1.234,56
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _normalizar_tildes(texto: str) -> str:
    """Quita tildes/diacríticos: 'potásico' → 'potasico', 'á' → 'a'.

    El catálogo guarda los nombres sin tildes; el motor de búsqueda matchea
    tokens exactos (AND), así que 'potásico' no encuentra 'potasico'.
    """
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )


def _dedupe_por_nombre(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """El catálogo de Firebase repite el MISMO ítem (mismo nombre) con distintos
    productId/precio (una fila por farmacia/precio). Dedupe por nombre quedándose
    con el de MENOR precio."""
    mejores: dict[str, dict[str, Any]] = {}
    for p in products:
        n = (p.get("producto") or "").strip()
        if not n:
            continue
        precio = p.get("precio")
        prev = mejores.get(n)
        if prev is None or (isinstance(precio, (int, float)) and precio < (prev.get("precio") or 0)):
            mejores[n] = p
    return list(mejores.values())


def _es_accesorio_medico(p: dict[str, Any]) -> bool:
    """True si el producto es un ACCESORIO/insumo médico, no un medicamento.

    El fallback por principio activo (p. ej. 'lantus' → 'insulina') puede
    devolver accesorios (jeringas, agujas, tiras reactivas) que NO son el
    fármaco que el cliente pidió. Si tras filtrar solo quedan accesorios, el
    agente debe decir honestamente que el medicamento no está disponible y
    escalar, en vez de ofrecer una jeringa como si fuera la respuesta a
    'Lantus'.
    """
    nombre = (p.get("producto") or p.get("nombre") or "").lower()
    if not nombre:
        return False
    nombre_sin = _normalizar_tildes(nombre)
    for acc in _ACCESORIOS_MEDICOS:
        if acc in nombre_sin:
            return True
    return False


def _filtrar_accesorios(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Descarta accesorios/insumos médicos de una lista de productos."""
    return [p for p in products if not _es_accesorio_medico(p)]


def _formatear_lista_productos(
    products: list[dict[str, Any]], titulo: str
) -> str:
    """Genera la lista de resultados en formato amigable y determinista:
    título del medicamento + opciones enumeradas, ordenadas por precio (menor a
    mayor), cada una con emoji 💊 y precio en USD y Bs. El LLM la cita literal."""
    if not products:
        return ""
    # Ordenar por precio USD de menor a mayor (estable).
    ordenados = sorted(
        products,
        key=lambda p: (p.get("precio") if isinstance(p.get("precio"), (int, float)) else 0),
    )
    lineas = [titulo.strip().upper()]
    for i, p in enumerate(ordenados, 1):
        nombre = str(p.get("producto") or p.get("title") or "").strip()
        usd = p.get("precio")
        bs = p.get("precioBs")
        usd_s = f"${_fmt_ve(usd)}" if isinstance(usd, (int, float)) else "$—"
        bs_s = f"Bs {_fmt_ve(bs)}" if isinstance(bs, (int, float)) else "Bs —"
        lineas.append(f"💊 {i}. {nombre}")
        lineas.append(f"   {usd_s}  |  {bs_s}")
    return "\n".join(lineas)


class ToolRuntime:
    """Ejecuta las tool-calls de UN turno y acumula sus efectos."""

    def __init__(
        self,
        ctx: AppContext,
        conv: Conversation,
        crm_conversation_id: str,
        profile: BusinessProfile | None = None,
        provider_id: str = "",
    ) -> None:
        self._ctx = ctx
        self._conv = conv
        self._crm_conv_id = crm_conversation_id
        self._profile = profile or BusinessProfile()
        # providerId del catálogo del tenant (viene del contexto del CRM, no env).
        self._provider_id_val = provider_id or ""
        # Efectos observables por turn.py:
        self.handoff_reason: str | None = None  # se ejecuta DESPUÉS de la despedida
        self.booked = False
        self.routed_out = False
        self.proposed = False
        # true si el turno consultó el catálogo (buscar_medicamento o
        # sugerir_generico). Sirve como backstop anti-alucinación: si el usuario
        # preguntó por un medicamento y NO se consultó, forzamos la consulta.
        self.consulted_catalog = False
        # Último término consultado con buscar_medicamento (para re-consultar
        # cuando el cliente refina con miligramo/marca sin repetir el nombre).
        self.last_term = ""
        # Último producto consultado con buscar_medicamento. Lo usan los backstops
        # de carrito: si el cliente responde con una cantidad y el LLM no llama
        # agregar_al_carrito, forzamos el add con este producto.
        self.last_product: dict[str, Any] | None = None
        # Lista completa de productos consultados (backstop de contradicción).
        self.last_products: list[dict[str, Any]] = []
        # Lista de opciones del último buscar_medicamento, ORDENADA por precio
        # (menor a mayor), tal como la muestra el formateador. Permite resolver
        # "quiero X cajas de la opción Z" en un turno nuevo.
        self.last_options: list[dict[str, Any]] = []
        # true cuando el backstop de carrito ya forzó el add este turno (evita loops).
        self.cart_forced = False
        # true cuando el backstop anti-alucinación ya re-consultó el catálogo
        # con el término deterministicamente correcto (evita loops infinitos).
        self.catalog_retried = False
        # true cuando el backstop de receta ya consultó todos los medicamentos
        # de la imagen y envió la respuesta formateada (evita re-procesar).
        self.receta_atendida = False
        # flags de backstops de resumen/finalizar (evitan loops).
        self.summary_forced = False
        self.finalize_forced = False
        # true si este turno se consultó un medicamento que NO está en el catálogo.
        # Lo usa el turno para escalar a humano de forma determinista (no depender
        # de que el LLM llame handoff).
        self.med_not_found = False
        # Si se llamó ver_carrito este turno, aquí queda el texto determinista del
        # resumen (cada producto con cantidad y subtotal en USD y Bs, y el total).
        # turn.py lo usa para reemplazar lo que haya generado el LLM, garantizando
        # que el monto SIEMPRE aparezca en Bs y por medicamento aunque el modelo
        # omita ese formato.
        self.cart_summary_text: str | None = None

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "update_ficha":
                return await self._update_ficha(args)
            if name == "propose_slots":
                return await self._propose_slots()
            if name == "book_session":
                return await self._book_session(args)
            if name == "reschedule_session":
                return await self._reschedule_session(args)
            if name == "route_out":
                return await self._route_out()
            if name == "handoff":
                return self._handoff(args)
            if name == "buscar_medicamento":
                return await self._buscar_medicamento(args)
            if name == "sugerir_generico":
                return await self._sugerir_generico(args)
            if name == "info_provider":
                return await self._info_provider()
            if name == "agregar_al_carrito":
                return await self._agregar_al_carrito(args)
            if name == "ver_carrito":
                return await self._ver_carrito()
            if name == "actualizar_cantidad":
                return await self._actualizar_cantidad(args)
            if name == "finalizar_pedido":
                return await self._finalizar_pedido()
            logger.warning("tools: herramienta desconocida %r", name)
            return {"ok": False, "error": f"herramienta desconocida: {name}"}
        except CrmError as exc:
            logger.warning("tools: %s falló contra el CRM: %s", name, exc)
            return {
                "ok": False,
                "error": "crm_error",
                "detalle": "no pude completar la acción; continúa la conversación o haz handoff",
            }

    async def _update_ficha(self, args: dict[str, Any]) -> dict[str, Any]:
        # Tolera el drift del LLM: manda lo que haya, el CRM normaliza flojo.
        ficha = {k: v for k, v in args.items() if v is not None}
        if not ficha:
            return {"ok": True, "nota": "sin campos nuevos"}
        await self._ctx.crm.put_ficha(self._crm_conv_id, ficha)
        return {"ok": True}

    async def _propose_slots(self) -> dict[str, Any]:
        raw = await self._ctx.crm.get_availability(
            limit=MAX_OFFERED, per_day=OFFER_PER_DAY, days=OFFER_DAYS
        )
        slots = _slots_from_payload(self._conv.id, raw)
        if not slots:
            return {
                "ok": False,
                "error": "sin_disponibilidad",
                "detalle": "no hay horarios abiertos; ofrece handoff para coordinar directo",
            }
        await self._ctx.store.replace_offered_slots(self._conv.id, slots)
        self.proposed = True
        return {
            "ok": True,
            "slots": _slots_for_llm(slots),
            "dias_con_agenda": sorted(
                {s.label.rsplit(",", 1)[0].strip() for s in slots}
            ),
            "instrucciones": (
                "esta es TODA la agenda abierta: los días que no aparecen aquí "
                "NO tienen agenda, dilo en vez de mover al lead a otro día. "
                "Ofrécele máximo 3, con su etiqueta tal cual (día incluido), "
                "los que embonen con lo que pidió."
            ),
        }

    async def _resolve_offered(
        self, args: dict[str, Any], accion: str
    ) -> tuple[OfferedSlot | None, dict[str, Any] | None]:
        """Slot elegido, o el error listo para devolverle al LLM.

        Validación server-side por epoch exacto: solo lo ofrecido es reservable.
        """
        wanted = _parse_utc(str(args.get("start_utc") or ""))
        offered = await self._ctx.store.get_offered_slots(self._conv.id)
        if wanted is None:
            return None, {
                "ok": False,
                "error": "start_utc_invalido",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        chosen = next(
            (
                s
                for s in offered
                if int(s.start_utc.timestamp()) == int(wanted.timestamp())
            ),
            None,
        )
        if chosen is None:
            logger.info(
                "tools: %s rechazado — %s no está entre los ofrecidos",
                accion,
                args.get("start_utc"),
            )
            return None, {
                "ok": False,
                "error": "slot_no_ofrecido",
                "detalle": "solo puedes agendar un horario que ya ofreciste",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        # Deja rastro de sobre qué frase del lead se tomó la decisión: cuando
        # una cita sale mal, esto dice si hubo confirmación o se asumió.
        logger.info(
            "tools: %s a %s (el lead confirmó con: %r)",
            accion,
            chosen.label,
            str(args.get("dia_confirmado") or "")[:120],
        )
        return chosen, None

    async def _book_session(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offered(args, "book_session")
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "slot_no_ofrecido"}
        try:
            result = await self._ctx.crm.create_booking(
                self._crm_conv_id, _iso_z(chosen.start_utc)
            )
        except SlotTaken as exc:
            # El slot se ocupó entre oferta y elección: alternativas frescas.
            fresh = _slots_from_payload(self._conv.id, exc.slots)
            await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
            return {
                "ok": False,
                "error": "slot_taken",
                "detalle": "ese horario se acaba de ocupar; discúlpate breve y ofrece estas alternativas",
                "slots": _slots_for_llm(fresh),
            }
        await self._ctx.store.clear_offered_slots(self._conv.id)
        self.booked = True
        try:
            await self._ctx.crm.put_ficha(
                self._crm_conv_id, {"calificado": True, "resultado": "agendo"}
            )
        except CrmError as exc:  # best-effort: la cita ya existe
            logger.warning("tools: no pude actualizar ficha tras booking: %s", exc)
        return {
            "ok": True,
            # La etiqueta del slot ofrecido trae el día en palabras; la del
            # CRM es la corta. Se repite ESTA para que el lead lea el día.
            "label": chosen.label or result.get("label"),
            "zoom_url": result.get("zoomJoinUrl"),
            "instrucciones": (
                "confirma el día COMPLETO y la hora tal cual dice label, "
                "comparte el link de la videollamada si existe y menciona lo "
                "que el negocio pida para llegar preparado"
            ),
        }

    async def _reschedule_session(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offered(args, "reschedule_session")
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "slot_no_ofrecido"}
        try:
            result = await self._ctx.crm.reschedule_booking(
                self._crm_conv_id, _iso_z(chosen.start_utc)
            )
        except SlotTaken as exc:
            fresh = _slots_from_payload(self._conv.id, exc.slots)
            await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
            return {
                "ok": False,
                "error": "slot_taken",
                "detalle": "ese horario se acaba de ocupar; discúlpate breve y ofrece estas alternativas",
                "slots": _slots_for_llm(fresh),
            }
        except CrmConflict as exc:
            if exc.code == "no_booking":
                return {
                    "ok": False,
                    "error": "sin_cita",
                    "detalle": "el lead no tiene cita por delante; usa book_session",
                }
            raise
        await self._ctx.store.clear_offered_slots(self._conv.id)
        self.booked = True
        return {
            "ok": True,
            "label": chosen.label or result.get("label"),
            "zoom_url": result.get("zoomJoinUrl"),
            "instrucciones": (
                "confirma que quedó movida, con el día COMPLETO y la hora tal "
                "cual dice label; el link de la videollamada sigue siendo el "
                "mismo salvo que aquí venga otro"
            ),
        }

    async def _route_out(self) -> dict[str, Any]:
        # "dio_diy" es el valor del enum `resultado` en el gateway del CRM
        # (006); el nombre de la herramienta es genérico, el cable no cambia.
        await self._ctx.crm.put_ficha(
            self._crm_conv_id, {"calificado": False, "resultado": "dio_diy"}
        )
        self.routed_out = True
        out: dict[str, Any] = {"ok": True}
        if self._profile.resources:
            out["recursos"] = self._profile.resources
            out["instrucciones"] = "comparte estos recursos al despedirte, puerta abierta"
        return out

    def _handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        self.handoff_reason = str(args.get("reason") or "lead_request")
        return {
            "ok": True,
            "nota": (
                "el pase a humano se ejecutará después de tu mensaje de despedida"
            ),
        }

    def _cancelar_handoff_por_no_disponible(self) -> None:
        """Cancela un handoff pendiente si fue por no-disponibilidad y la
        búsqueda posterior SÍ encontró productos (p. ej. fallback de principio
        activo). El LLM pudo llamar handoff tras un sin_resultados basura
        ('marca 125 mg') y luego la búsqueda real encontró alternativas."""
        if self.handoff_reason and (
            "no disponible" in self.handoff_reason
            or "no encontrado" in self.handoff_reason
            or "medicamento" in self.handoff_reason
            or self.handoff_reason == "medicamento_no_disponible"
        ):
            logger.info(
                "handoff cancelado: la búsqueda encontró productos (%s)",
                self.handoff_reason,
            )
            self.handoff_reason = None

    # ------------------------------------------------------------- farmacia ---
    # Tools del rol farmacéutico (spec 001). Consultan el catálogo del tenant vía
    # el CRM; NUNCA inventan precios (la fuente es el catálogo por providerId).

    @property
    def _provider_id(self) -> str:
        return self._provider_id_val

    async def _log_med_query(
        self,
        term: str,
        products: list[dict[str, Any]],
        added_to_cart: bool = False,
    ) -> None:
        """Registra la consulta de medicamento (analytics Fase 1)."""
        # Filtro anti-basura: no registrar frases alucinadas del LLM como si
        # fueran medicamentos (manchan el dashboard de Analítica).
        if not _termino_busqueda_plausible(term):
            logger.info("med_query descartada (término no plausible): %r", term[:60])
            return
        try:
            await self._ctx.store.log_med_query(
                conversation_id=self._conv.id,
                provider_id=self._provider_id,
                term=term,
                product_id=products[0].get("productId") if products else None,
                product_name=products[0].get("producto") if products else None,
                result_count=len(products),
                added_to_cart=added_to_cart,
            )
            # Fase 2: clasificación automática de pacientes crónicos. Si el
            # término matchea un medicamento crónico, registra la consulta y
            # actualiza el perfil (best-effort, jamás tumba el turno).
            condiciones = await self._ctx.store.condiciones_para_termino(term)
            if condiciones:
                await self._ctx.store.registrar_consulta_cronica(
                    provider_id=self._provider_id,
                    wa_identity=self._conv.wa_identity,
                    condiciones=condiciones,
                )
        except Exception:
            logger.exception("_log_med_query: fallo al registrar consulta")

    async def _buscar_medicamento(self, args: dict[str, Any]) -> dict[str, Any]:
        nombre = str(args.get("nombre") or "").strip()
        if not nombre:
            return {"ok": False, "error": "nombre_vacio", "detalle": "indica qué medicamento buscas"}
        # Guarda anti-saludo: si el LLM llamó buscar_medicamento con un término
        # que es SOLO saludos/cortesía ("saludos", "buen día"), NO es una
        # consulta de medicamento. Devolver una lista de productos aquí sería
        # un error grave (el cliente saludó y le respondemos con 19 productos).
        # Se devuelve un resultado que le dice al LLM que responda con un
        # saludo, sin tocar el catálogo.
        if _es_solo_saludo(nombre):
            logger.info(
                "buscar_medicamento: término '%s' es solo saludo — no busco en catálogo",
                nombre,
            )
            return {
                "ok": False,
                "error": "solo_saludo",
                "detalle": (
                    "El cliente solo saludó (no pidió ningún medicamento). "
                    "Responde con un saludo cálido y pregunta qué medicamento "
                    "necesita. NO muestres lista de productos."
                ),
            }
        # Quitar saludos/cortesía que el LLM dejó pegados al medicamento
        # ('epa panadol' → 'panadol'). Sin esto, 'epa' matchea con 'EPAX'
        # en el catálogo y devuelve el producto equivocado.
        nombre_limpio = _quitar_saludos(nombre)
        if nombre_limpio and nombre_limpio != nombre.lower():
            logger.info(
                "buscar_medicamento: término '%s' → limpio '%s' (quité saludos)",
                nombre, nombre_limpio,
            )
            nombre = nombre_limpio
        # Limpiar TODAS las palabras funcionales/relleno en cualquier posición
        # ('genérico del daflon económico' → 'daflon'). Si el LLM llamó la
        # búsqueda con una frase que tras limpiar NO deja ningún sustantivo
        # plausible, es ruido (CAJAS OPCION ECONOMICA, ...) — NO consultar el
        # catálogo, que devolvería basura irrelevante.
        nombre_sustantivo = _limpiar_termino_medicamento(nombre)
        if not _termino_es_medicamento_plausible(nombre) and not nombre_sustantivo:
            logger.info(
                "buscar_medicamento: término '%s' sin sustantivo de fármaco — no busco en catálogo",
                nombre,
            )
            return {
                "ok": False,
                "error": "no_medicamento",
                "detalle": (
                    "El cliente NO está pidiendo un medicamento: pide otra cosa "
                    "(un comparador de precios, un reclamo, un saludo, una "
                    "pregunta general, etc.). NO digas que buscaste un "
                    "medicamento ni que 'no encontraste nada' — eso confunde. "
                    "Responde a lo que el cliente realmente pide: si pide un "
                    "comparador de precios, aclara que solo consultas el precio "
                    "de medicamentos específicos y pregúntale cuál quiere; si "
                    "es un reclamo, escúchalo y escala a un humano; si es un "
                    "saludo, saluda y pregunta qué medicamento necesita. "
                    "NUNCA muestres lista de productos."
                ),
            }
        # Limpio quedó un solo fármaco: usarlo como término (evita basura).
        if nombre_sustantivo and nombre_sustantivo != nombre.lower():
            logger.info(
                "buscar_medicamento: término '%s' → sustantivo '%s'",
                nombre, nombre_sustantivo,
            )
            nombre = nombre_sustantivo
        if not self._provider_id:
            return {
                "ok": False,
                "error": "sin_provider",
                "detalle": "no hay catálogo configurado; di que consultarás o haz handoff",
            }
        self.consulted_catalog = True
        # Normalizar tildes: el catálogo guarda 'potasico' sin tilde; si el
        # cliente escribe 'potásico', el motor no matchea (AND sobre tokens).
        nombre = _normalizar_tildes(nombre)
        data = await self._ctx.crm.get_products(self._provider_id, q=nombre, limit=20)
        self.last_term = nombre
        products = data.get("products") or []
        # Dedupe por nombre de producto: el catálogo de Firebase repite el MISMO
        # ítem (mismo nombre) con distintos productId/precio (una entrada por
        # farmacia/precio). Quedarse con el de MENOR precio evita listas de 20
        # con 14 duplicados idénticos.
        products = _dedupe_por_nombre(products)
        # Fallback de acortamiento: si el término completo (p. ej. un OCR muy
        # verboso "sitagliptina metformina clorhidrato 50 mg 500 mg comprimidos")
        # no da resultados porque el motor matchea TODOS los tokens (AND), suelta
        # tokens finales de la cola hasta encontrar algo. 'clorhidrato',
        # 'comprimidos', 'recubiertos' sobran; el nombre del fármaco es lo que
        # matchea en el catálogo.
        if not products:
            tokens = [
                t
                for t in re.split(r"[\s,/-]+", nombre)
                if t and t.lower()
                not in {
                    "mg", "ml", "g", "mcg", "tab", "tabletas", "tabletas",
                    "comprimidos", "recubiertos", "clorhidrato", "clorhidrat",
                    "gotas", "jarabe", "x", "de", "con", "y", "solucion",
                    "suspension", "amp", "ampolla", "frasco", "x30", "x20",
                    "x10", "caja", "blister",
                }
            ]
            fallback = " ".join(tokens[:4]) if tokens else nombre
            if fallback != nombre:
                logger.info(
                    "buscar_medicamento: '%s' sin resultados — reintento con '%s'",
                    nombre, fallback,
                )
                data = await self._ctx.crm.get_products(
                    self._provider_id, q=fallback, limit=20
                )
                products = _dedupe_por_nombre(data.get("products") or [])
                if products:
                    self.last_term = fallback
        if not products:
            # Fallback por principio activo: 'depomedrol' → 'metilprednisolona'.
            # El cliente pregunta por una MARCA que no está, pero su principio
            # activo puede estar en el catálogo (p. ej. ampollas genéricas).
            # SOLO se intenta si el término parece una marca de medicamento
            # real (corto, sin frases del cliente). 'caja cada uno' o
            # 'van responder qué solución' NO son medicamentos → no adivinar.
            alternativas: list[dict[str, Any]] = []
            if _termino_es_medicamento_plausible(nombre):
                alternativas = _dedupe_por_nombre(
                    await self._buscar_por_principio_activo(nombre)
                )
            if alternativas:
                self.med_not_found = False
                self.last_term = nombre
                self.last_product = alternativas[0]
                self.last_products = alternativas
                self.last_options = sorted(
                    alternativas,
                    key=lambda p: (p.get("precio") if isinstance(p.get("precio"), (int, float)) else 0),
                )
                logger.info(
                    "buscar_medicamento: '%s' sin resultados — alternativa por principio activo (%d productos)",
                    nombre, len(alternativas),
                )
                # Si el LLM llamó handoff por un sin_resultados previo (basura),
                # se cancela: ahora SÍ hay productos que ofrecer.
                self._cancelar_handoff_por_no_disponible()
                await self._log_med_query(nombre, alternativas, added_to_cart=False)
                return {
                    "ok": True,
                    "products": alternativas,
                    "provider": data.get("provider"),
                    "principio_activo": True,
                    "instrucciones": (
                        f"'{nombre}' NO está como tal en el catálogo, pero su PRINCIPIO ACTIVO "
                        "SÍ está disponible. Presenta estas alternativas por principio activo "
                        "con su nombre exacto y precio (USD y Bs). NUNCA digas 'no disponible' "
                        "sin ofrecerlas primero."
                    ),
                }
            self.med_not_found = True
            await self._log_med_query(nombre, [], added_to_cart=False)
            return {
                "ok": False,
                "error": "sin_resultados",
                "detalle": (
                    f"no encontrado '{nombre}' en el catálogo. Antes de decir 'no disponible': "
                    "1) si el nombre puede tener errores de tipeo, reintenta con la grafía "
                    "más probable (p. ej. 'lozartan'→'losartan', 'paracetmol'→'paracetamol'); "
                    "2) prueba con el principio activo; SOLO si nada matchea, informa "
                    "honestamente que no lo tienes y escala a humano (handoff) — NO ofrezcas "
                    "'consultar' algo que no puedes consultar"
                ),
                "busqueda": nombre,
            }
        # Recordar el primer producto (para el backstop de carrito).
        self.last_product = products[0]
        # Lista completa de productos consultados (para el backstop de
        # contradicción: si el LLM niega disponibilidad pese a haber resultados,
        # reemplazamos su texto con la lista real).
        self.last_products = products
        # Lista de opciones ORDENADA por precio (menor a mayor), tal como la
        # muestra _formatear_lista_productos: así "opción Z" se resuelve contra
        # el MISMO orden que el cliente vio.
        self.last_options = sorted(
            products,
            key=lambda p: (p.get("precio") if isinstance(p.get("precio"), (int, float)) else 0),
        )
        # Si el LLM llamó handoff por un sin_resultados previo (basura), se
        # cancela: ahora SÍ hay productos que ofrecer.
        self._cancelar_handoff_por_no_disponible()
        await self._log_med_query(nombre, products, added_to_cart=False)
        return {
            "ok": True,
            "products": products,
            "provider": data.get("provider"),
            "instrucciones": self._formatear_instrucciones_busqueda(products, nombre),
        }

    async def _buscar_por_principio_activo(self, nombre: str) -> list[dict[str, Any]]:
        """Si la marca no está en el catálogo, pide al LLM el PRINCIPIO ACTIVO
        (p. ej. 'depomedrol' → 'metilprednisolona') y lo busca en el catálogo.

        Devuelve los productos del principio activo, o [] si no se puede
        mapear/consultar. Nunca alucina: si el LLM no da un principio activo
        plausible, se devuelve vacío (el agente niega honestamente).
        """
        if not self._provider_id:
            return []
        try:
            reply = await self._ctx.llm.complete(
                [
                    {
                        "role": "user",
                        "content": (
                            f"El medicamento '{nombre}' es una MARCA comercial. "
                            "Responde SOLO con el principio activo genérico en "
                            "español (nombre científico, sin marca), en minúsculas "
                            "y sin puntuación. Ejemplo: 'depomedrol' → "
                            "'metilprednisolona'; 'atamel' → 'paracetamol'; "
                            "'buscapina' → 'hioscina'. Si no conoces el principio "
                            "activo, responde exactamente 'desconocido'."
                        ),
                    }
                ],
                tools=None,
            )
            principio = (reply.content or "").strip().lower()
            if not principio or principio == "desconocido":
                return []
            # Limpiar: quitar ruido del modelo.
            principio = re.sub(r"[^a-záéíóúñü ]+", "", principio).strip()
            if len(principio) < 3 or principio == nombre.lower():
                return []
            data = await self._ctx.crm.get_products(
                self._provider_id, q=principio, limit=20
            )
            products = data.get("products") or []
            # Filtrar accesorios/insumos (jeringas, agujas, tiras): el principio
            # activo 'insulina' matchea la jeringa, que NO es el fármaco que el
            # cliente pidió. Si solo quedan accesorios, devolver [] para que el
            # agente diga honestamente que el medicamento no está disponible.
            return _filtrar_accesorios(products)
        except Exception as exc:
            logger.warning("principio activo: fallo al mapear '%s': %s", nombre, exc)
            return []

    def _formatear_instrucciones_busqueda(
        self, products: list[dict[str, Any]], termino: str = ""
    ) -> str:
        """Instrucciones que guían a la IA: si el principio activo tiene varias
        presentaciones con distinto miligramo/marca, primero pregunta cuál quiere
        (ser más amigable y preciso), en vez de soltar un grupo grande de
        resultados. (Mejora de consultas de medicamentos.)"""
        base = (
            "Cada producto trae 'precio' (USD) y 'precioBs' (bolívares, ya "
            "convertido con la tasa BCV). Presenta SIEMPRE ambos: '$' para "
            "USD y 'Bs' para bolívares. Nunca uses MXN/pesos. 2 decimales. "
            "INVENTA LO MÍNIMO: cita SOLO los productos que están en el catálogo "
            "('products'), con su nombre exacto y su precio EXACTO. NUNCA inventes "
            "el principio activo, laboratorios, marcas, precios ni presentaciones "
            "que no estén en 'products'. Si el catálogo trae UN solo producto, "
            "muestra SOLO ese producto y su precio; no inventes presentaciones ni "
            "composiciones adicionales."
        )
        # Lista ya formateada (ordenada por precio, con 💊) para que el LLM la
        # cite literalmente en vez de inventar formato o datos.
        lista = _formatear_lista_productos(products, termino or "Resultados")
        base += f" Lista formateada (cítala tal cual, sin cambiar nombres ni precios):\n{lista}"
        # Extraer miligramos (mg) de cada producto para detectar presentaciones distintas.
        miligramos = _extraer_miligramos(products)
        marcas = _extraer_marcas(products)
        # Caso 1: varias presentaciones con distinto mg → preguntar antes de listar.
        if len(miligramos) >= 2:
            opciones = ", ".join(sorted(miligramos))
            return (
                base
                + " El usuario pidió un PRINCIPIO ACTIVO que existe en varias "
                "presentaciones con distinto miligramo: "
                + opciones
                + ". NO sueltes el grupo grande todavía. Primero pregúntale en "
                "una línea amigable qué miligramos necesita. Cuando te responda, "
                "consulta de nuevo filtrando ese miligremo. Si ya dio el miligrema, "
                "no preguntes: muestra directamente lo que encaje."
            )
        # Distinto caso: varias marcas del mismo mg → sugerir elegir marca.
        if len(marcas) >= 2:
            return (
                base
                + " El principio activo está disponible en varias marcas. "
                "PRESENTA SOLO las marcas del catálogo real con su precio EXACTO "
                "(las de arriba). NUNCA inventes una marca o precio que no esté en "
                "el catálogo real."
            )
        return base

    async def _sugerir_generico(self, args: dict[str, Any]) -> dict[str, Any]:
        nombre = str(args.get("nombre") or "").strip()
        if not nombre:
            return {"ok": False, "error": "faltante", "detalle": "indica el medicamento"}
        if not self._provider_id:
            return {"ok": False, "error": "sin_provider", "detalle": "sin catálogo configurado"}
        data = await self._ctx.crm.get_products(self._provider_id, q=nombre, limit=8)
        products = data.get("products") or []
        # Genéricos = los que tienen nombre generico distinto del nombre buscado
        genericos = [p for p in products if p.get("generico")]
        if not genericos:
            return {
                "ok": False,
                "error": "sin_generico",
                "detalle": "no encontré alternativas genéricas; ofrece el producto original",
            }
        return {
            "ok": True,
            "genericos": genericos,
            "instrucciones": "ofrece la opción genérica con su precio como alternativa más económica",
        }

    async def _info_provider(self) -> dict[str, Any]:
        if not self._provider_id:
            return {"ok": False, "error": "sin_provider", "detalle": "no hay farmacia configurada"}
        data = await self._ctx.crm.get_providers(self._provider_id)
        provider = data.get("provider")
        if not provider:
            return {"ok": False, "error": "sin_provider_info", "detalle": "no hay info de la farmacia"}
        return {
            "ok": True,
            "provider": provider,
            "instrucciones": "responde con dirección, horario y ciudad de la farmacia",
        }

    # ------------------------------------------------------- carrito (FR-8) ---

    async def _agregar_al_carrito(self, args: dict[str, Any]) -> dict[str, Any]:
        product_id = str(args.get("productId") or "").strip()
        producto = str(args.get("producto") or "").strip()
        cantidad_raw = args.get("cantidad")
        try:
            cantidad = max(1, int(cantidad_raw))
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "cantidad_invalida",
                "detalle": "indica cuántas cajas/unidades quiere (número entero >= 1)",
            }
        if not product_id or not producto:
            return {
                "ok": False,
                "error": "faltante",
                "detalle": "producto y productId son obligatorios (debe venir de buscar_medicamento)",
            }
        presentacion = str(args.get("presentacion") or "")
        laboratorio = str(args.get("laboratorio") or "")
        precio_usd = args.get("precioUsd")
        precio_bs = args.get("precioBs")
        item = await self._ctx.store.cart_add(
            self._conv.id,
            product_id,
            producto,
            presentacion,
            laboratorio,
            cantidad,
            float(precio_usd) if precio_usd is not None else None,
            float(precio_bs) if precio_bs is not None else None,
        )
        return {
            "ok": True,
            "item": {
                "producto": item.producto,
                "cantidad": item.cantidad,
                "precioUsd": item.precio_usd,
                "precioBs": item.precio_bs,
            },
            "instrucciones": (
                "confirma en una línea que quedó agregado (cantidad + producto). "
                "Luego pregunta de forma breve si desea buscar otro medicamento "
                "(SI/NO). No sumes todo el carrito en cada mensaje."
            ),
        }

    async def _actualizar_cantidad(self, args: dict[str, Any]) -> dict[str, Any]:
        product_id = str(args.get("productId") or "").strip()
        cantidad_raw = args.get("cantidad")
        try:
            cantidad = max(1, int(cantidad_raw))
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "cantidad_invalida",
                "detalle": "indica cuántas cajas/unidades quiere (número entero >= 1)",
            }
        if not product_id:
            return {
                "ok": False,
                "error": "faltante",
                "detalle": "productId es obligatorio (debe venir de buscar_medicamento)",
            }
        item = await self._ctx.store.cart_set(
            self._conv.id, product_id, cantidad
        )
        if item is None:
            return {
                "ok": False,
                "error": "no_en_carrito",
                "detalle": "ese producto no está en el pedido; usa agregar_al_carrito",
            }
        return {
            "ok": True,
            "item": {
                "producto": item.producto,
                "cantidad": item.cantidad,
                "precioUsd": item.precio_usd,
                "precioBs": item.precio_bs,
            },
            "instrucciones": (
                "confirma en una línea la nueva cantidad del producto. "
                "Luego llama ver_carrito y presenta el resumen actualizado con "
                "los nuevos subtotales y total."
            ),
        }

    async def _ver_carrito(self) -> dict[str, Any]:
        items = await self._ctx.store.cart_items(
            self._conv.id, session_hours=self._ctx.settings.cart_session_hours
        )
        if not items:
            self.cart_summary_text = None
            return {
                "ok": True,
                "empty": True,
                "detalle": "el carrito está vacío; ofrécele buscar un medicamento",
            }
        total_usd = sum((i.precio_usd or 0) * i.cantidad for i in items)
        total_bs = sum((i.precio_bs or 0) * i.cantidad for i in items)
        # Resumen determinista: cada producto con cantidad y subtotal en USD y
        # Bs, y el total en ambos. El LLM lo cita literal; turn.py lo usa como
        # backstop para que el monto en Bs y el subtotal por medicamento SIEMPRE
        # aparezcan, aunque el modelo omita el formato.
        bloque = []
        bloque.append("🛒 *Productos:*")
        for i in items:
            sub_usd = (i.precio_usd or 0) * i.cantidad
            sub_bs = (i.precio_bs or 0) * i.cantidad
            bloque.append(f"•⁠  ⁠{i.producto}")
            bloque.append(f"  Cantidad: {i.cantidad}")
            bloque.append(f"  Subtotal: ${_fmt_ve(sub_usd)} | Bs {_fmt_ve(sub_bs)}")
        bloque.append("")
        bloque.append("*Total:*")
        bloque.append(f"${_fmt_ve(total_usd)} | Bs {_fmt_ve(total_bs)}")
        bloque.append("")
        bloque.append("¿Está todo correcto o deseas agregar algo más?")
        self.cart_summary_text = "\n".join(bloque)
        return {
            "ok": True,
            "resumen_para_el_cliente": self.cart_summary_text,
            "items": [
                {
                    "producto": i.producto,
                    "cantidad": i.cantidad,
                    "precioUsd": i.precio_usd,
                    "precioBs": i.precio_bs,
                    "subtotalUsd": (i.precio_usd or 0) * i.cantidad,
                    "subtotalBs": (i.precio_bs or 0) * i.cantidad,
                }
                for i in items
            ],
            "totalUsd": total_usd,
            "totalBs": total_bs,
            "instrucciones": (
                "presenta el RESUMEN del pedido EXACTAMENTE como viene en "
                "`resumen_para_el_cliente` (cada producto con cantidad y "
                "subtotal en USD y Bs, y el total en ambos). NO cambies el "
                "formato ni omitas el monto en Bs."
            ),
        }

    async def _finalizar_pedido(self) -> dict[str, Any]:
        items = await self._ctx.store.cart_items(
            self._conv.id, session_hours=self._ctx.settings.cart_session_hours
        )
        if not items:
            return {
                "ok": False,
                "error": "carrito_vacio",
                "detalle": "no hay productos en el pedido; no puedes finalizar sin nada",
            }
        # Registra el pedido como nota de la conversación en el CRM (FR-9) y
        # notifica a un humano para que lo procese.
        lineas = []
        for i in items:
            total_item = (i.precio_usd or 0) * i.cantidad
            lineas.append(f"- {i.cantidad}x {i.producto} (${total_item:.2f})")
        total = sum((i.precio_usd or 0) * i.cantidad for i in items)
        nota = "PEDIDO LISTO (cargado por el agente farmacéutico):\n" + "\n".join(lineas) + f"\nTotal: ${total:.2f}"
        try:
            await self._ctx.crm.put_ficha(
                self._crm_conv_id, {"resultado": "pedido", "notas": nota}
            )
        except Exception as exc:  # no derribe el turno: best-effort
            logger.warning("tools: no pude registrar pedido en el CRM: %s", exc)
        await self._ctx.store.cart_clear(self._conv.id)
        return {
            "ok": True,
            "total": total,
            "instrucciones": (
                "agradece, confirma que el pedido quedó registrado y que un "
                "humano lo procesará, y despídete con puerta abierta. NO inventes "
                "folios ni tiempos de entrega."
            ),
        }
