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
from decimal import Decimal
from typing import Any

from app.crm import CrmConflict, CrmError, SlotTaken
from app.relevancia import (
    _norm,
    _token_cabeza_farmaco,
    filtrar_relevantes,
    hay_senal_de_farmaco,
    parece_presentacion_personal,
)

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
    # Referencias a la IMAGEN que mandó el cliente. NO son fármacos, y buscarlas
    # en el catálogo devuelve basura por SUBSTRING: 'foto' matchea 'FOTORRETIN'
    # (un oftálmico), y el agente respondía "sí, tengo el producto de la foto"
    # mostrando ese oftálmico ante la foto de unos óvulos vaginales. Caso real
    # provider 19 (2026-10). El producto de la imagen se busca con el OCR, nunca
    # con estas palabras.
    "foto", "fotos", "imagen", "imagenes", "adjunto", "adjuntos", "captura",
    "pantallazo", "envie", "envio", "enviaste", "mande", "mandaste", "muestro",
    "muestra", "aparece", "figura", "ve", "ven", "ahi", "arriba", "anexo",
    "anexa", "mandado", "mandada", "enviado", "enviada",
    # Cortesía/negación/despedida: NUNCA son fármacos, y buscarlas devuelve basura
    # (el splitter de listas partía "no las voy a comprar y disculpe" y buscaba
    # 'voy comprar' y 'disculpe' como medicamentos). El guard
    # `_es_negativa_o_despedida` corta ese camino; esta lista protege además el
    # camino en que el LLM decide buscar por su cuenta.
    "voy", "vas", "vamos", "disculpe", "disculpa", "disculpen", "perdone",
    "perdon", "molestia", "siento", "lamento", "interesa", "interesada",
    "interesado", "comprar", "compro", "comprare", "olvidalo", "dejalo",
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


UNIDADES_HABLADAS: dict[str, str] = {
    "miligramo": "mg", "miligramos": "mg",
    "mililitro": "ml", "mililitros": "ml",
    "microgramo": "mcg", "microgramos": "mcg",
    "gramo": "g", "gramos": "g",
}
# Unidades de dosis DICHAS EN PALABRAS, con su abreviatura.
#
# Un cliente por nota de voz (o al escribir con naturalidad) dice "nifedipina de 30
# MILIGRAMOS", no "nifedipina 30 mg". Si la unidad hablada no se reconoce:
#   (1) el NÚMERO que la precede se descarta por corto -> 'nifedipina miligramos', y
#       el 30 se pierde;
#   (2) el catálogo no puede filtrar la dosis -> devuelve 10, 20 y 30 mg mezcladas.
# Medido contra el catálogo real: 'nifedipina 30 miligramos' -> 9 productos con
# 10/20/30 mezclados; 'nifedipina 30 mg' -> 2, ambos de 30.
# Es el MISMO patrón que ya mordió cuatro veces en este proyecto: filtrar por
# longitud, o no conocer una forma del dato, rompe la DOSIS. Los números y sus
# unidades —escritas como sea— son la excepción a cualquier regla de limpieza.


def _normalizar_unidad(palabra: str) -> str:
    """Traduce la unidad HABLADA ('miligramos') a su abreviatura ('mg').

    Devuelve la palabra intacta si no es una unidad hablada.
    """
    return UNIDADES_HABLADAS.get(palabra, palabra) or palabra


# Tokens de PRESENTACIÓN / concentración de ENVASE: no identifican el fármaco, solo lo
# describen. El matcher del catálogo hace AND, así que escribirlos EXIGE que el nombre
# los contenga — y como casi ningún nombre los trae, la búsqueda cae a la fase difusa y
# el producto exacto se sale de la ventana de resultados.
#
# Medido sobre 6.148 nombres reales: 'tab' aparece en 776, 'cap' en 113, 'comp' en 91 —
# no discriminan nada. 'meq' aparece en UNO solo (un rehidrosol), así que no puede
# encontrar ningún citrato de potasio: solo estorba.
#
# NO están aquí mg/ml/mcg/g/ui/cc (unidades de DOSIS) ni ningún número: esos SÍ filtran
# y perderlos devuelve todas las concentraciones mezcladas. Es la regla que más veces ha
# mordido en este proyecto.
_PRESENTACION = {
    "tab", "tabs", "tableta", "tabletas", "comp", "comprimido", "comprimidos",
    "cap", "caps", "capsula", "capsulas", "gragea", "grageas",
    "jab", "sob", "sobre", "sobres", "meq", "lp", "retard",
    # Formas que el cliente añade a la MARCA y que NO identifican el fármaco.
    # Caso real: 'Depofem ampolla' → el catálogo devolvía su grupo difuso de ampollas
    # (Dexametasona, Furosemida, Ranitidina...) y bastaba que el filtro aceptara UNA de
    # esas para llenar la lista con 20 medicamentos ajenos. Con la marca sola
    # ('depofem') el catálogo devuelve 0: el producto no está, y el agente debe decirlo.
    "ampolla", "ampollas", "amp", "vial", "viales",
    "inyectable", "inyectables", "inyeccion", "inyecciones",
    "frasco", "frascos", "tubo", "tubos", "pote", "potes",
    "pastilla", "pastillas", "pildora", "pildoras",
}


def _limpiar_termino_medicamento(term: str) -> str:
    """Deja SOLO las palabras "sustantivas" (posible fármaco) del término.

    Quita TODAS las palabras funcionales/relleno en cualquier posición (no solo
    al inicio como _quitar_saludos): 'genérico del daflon económico' →
    'daflon'; 'cajas opción económica 50 mg' → '' (sin sustantivo). Devuelve
    '' si no queda ninguna palabra sustantiva.

    OJO — umbral de longitud: las palabras de relleno se descartan por ser
    cortas (≤2 letras: 'de', 'la', 'x'), pero un NÚMERO de 1-2 cifras es una
    DOSIS y hay que conservarlo. Con un `len(w) >= 3` parejo, 'ATORVASTATINA 80
    MG' se reducía a 'atorvastatina' y el agente perdía la concentración: el
    catálogo devolvía TODAS las presentaciones (20, 40, 80 mg) cuando el cliente
    pidió 80. Se detectó justo así en producción (los '100' y '850' sí
    sobrevivían por tener 3 cifras, y los '40'/'50'/'80' no).
    """
    t = (term or "").strip().lower()
    if not t:
        return ""
    t_sin = t.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    palabras = re.findall(r"[a-z0-9]+", t_sin)
    # La forma HABLADA de la unidad ("miligramos") se traduce a su abreviatura
    # ("mg") ANTES de decidir. Sin esto el número que la precede se descarta por
    # corto (len < 3) y la dosis se pierde: 'nifedipina 30 miligramos' →
    # 'nifedipina miligramos' → el catálogo devuelve 10/20/30 mg mezcladas.
    # Medido: con `mg` → 2 productos, ambos de 30.
    palabras = [_normalizar_unidad(w) for w in palabras]
    # UNIDADES de dosis/presentación: son cortas (2-3 letras) pero NO son relleno.
    # Descartarlas por longitud rompe la búsqueda por concentración: 'esoz 40 mg'
    # se reducía a 'esoz 40' — sin unidad, el catálogo no puede filtrar la dosis y
    # devuelve todas las presentaciones mezcladas (20 y 40 mg), que es justo el
    # bug reportado. Se conservan siempre.
    unidades = {
        "mg", "ml", "mcg", "gr", "g", "cc", "ui", "kg",
        "tab", "tabs", "cap", "caps", "jab", "sob",
    }
    sustantivas = [
        w for w in palabras
        if (w in unidades)
        or (w not in _PALABRAS_FUNCIONALES and (len(w) >= 3 or w.isdigit()))
    ]
    if not sustantivas:
        return ""
    # Un número SOLO no es un medicamento: 'cajas opción económica 50 mg' no debe
    # reducirse a '50' y pasar el guard de "no es medicamento" (consultaría el
    # catálogo con basura). Se exige al menos una palabra con letras.
    if not any(not w.isdigit() for w in sustantivas):
        return ""
    # TOKENS DE PRESENTACIÓN FUERA DE LA CONSULTA. El matcher del catálogo hace AND
    # sobre los tokens: si el cliente escribe la presentación, el catálogo la EXIGE
    # como si fuera parte del nombre.
    #
    # Medido contra el catálogo real (provider 27):
    #   'CITRATO POTASIO TAB MEQ' -> 16 productos, 1 con citrato de potasio
    #   'CITRATO POTASIO'         -> 20 productos, 2 con citrato de potasio
    #   'citrato potasio 10 meq'  -> 20 productos, 0 con citrato de potasio
    # Es decir: 'tab'/'meq' EMPUJAN al matcher a la fase difusa y sacan al producto
    # EXACTO de la ventana. 'meq' aparece en 1 solo producto del catálogo (un
    # rehidrosol), así que no puede encontrar nada: solo estorba.
    #
    # NO se tocan los NÚMEROS ni las UNIDADES DE DOSIS (mg, ml, mcg, g, ui, cc): esas
    # SÍ filtran y perderlas devuelve todas las concentraciones mezcladas (la DOSIS
    # es la excepción permanente a cualquier limpieza de este proyecto).
    sin_presentacion = [w for w in sustantivas if w not in _PRESENTACION]
    if sin_presentacion and any(not w.isdigit() for w in sin_presentacion):
        sustantivas = sin_presentacion
    return " ".join(sustantivas)


def _quitar_saludos(term: str) -> str:
    """Quita los saludos/cortesía del INICIO de un término de búsqueda.

    El LLM a veces deja el saludo pegado al medicamento ('epa panadol',
    'hola losartan'), y ese saludo falsea la búsqueda (epa → EPAX, hola →
    ...). Quita las palabras iniciales que sean saludos/cortesía O verbos de
    consulta, devolviendo el resto. 'epa panadol' → 'panadol'.
    'buenos dias, quiero daflon' → 'daflon'. Devuelve '' si todo era ruido.

    NOTA: opera sobre los TOKENS ORIGINALES (con tildes) y compara cada uno
    contra el ruido en su forma sin tilde. Un `t.find(w)` previo buscaba el
    token sin tilde dentro del `t` original acentuado → devolvía -1 para
    palabras con tilde ('óvulos' → find('ovulos') = -1) y `t[-1:]` escupía la
    última letra ('u'), así que cualquier consulta con tilde terminaba
    buscando una letra suelta y el agente decía "no encontré información".
    """
    t = (term or "").strip().lower()
    if not t:
        return ""
    # Tokens originales (preservan tildes); se comparan sin tilde contra el ruido.
    tokens = re.findall(r"[\wáéíóúñü]+", t, re.UNICODE)
    ruido = _SALUDOS | {
        "dia", "dias", "tardes", "noches", "mañana", "tarde", "buenos", "buenas",
        "tienes", "tiene", "tengan", "tienen", "hay", "venden", "vendes",
        "quiero", "quiere", "quieres", "quisiera", "necesito", "busco",
        "buscando", "buscar", "busca", "buscan", "consiguen", "consigues",
        "conseguir", "me", "dan", "dame", "da", "saber", "cuanto", "cuesta",
        "cuestan", "precio", "disponible", "disponibles", "traen", "mande",
    }
    sin_tilde = lambda w: (
        w.replace("á", "a").replace("é", "e").replace("í", "i")
         .replace("ó", "o").replace("ú", "u")
    )
    # Saltar ruido inicial y reconstruir el resto con los tokens ORIGINALES.
    out: list[str] = []
    for w in tokens:
        if not out and sin_tilde(w) in ruido:
            continue
        out.append(w)
    return " ".join(out).strip()
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


def _fmt_ve(num) -> str:
    """Formato venezolano: coma decimal y punto de miles (1.234,56).

    Acepta int/float y Decimal (asyncpg devuelve decimal.Decimal para columnas
    NUMERIC del carrito; si se rechaza, el subtotal sale $— aunque el precio
    esté guardado).
    """
    if isinstance(num, Decimal):
        num = float(num)
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


def _extraer_dosis(texto: str) -> str:
    """Extrae la dosis ("40 mg", "500 mg", "120 ml") de un término de medicamento.

    Devuelve "" si no hay. Se usa para NO perder la concentración al cambiar de
    marca a principio activo: el cliente pidió "esoz 40 mg", el LLM devuelve
    "omeprazol" sin dosis, y buscar así mezclaba 20 y 40 mg en la misma lista.

    Solo número + unidad de DOSIS (mg/mcg/g/ml/ui); nunca la cantidad de envase
    ("x 10 cap", "20 tabletas"), que no es concentración.
    """
    if not texto:
        return ""
    m = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(mg|mcg|g|gr|ml|cc|ui|u\.i\.)\b",
        texto.lower(),
    )
    if not m:
        return ""
    return f"{m.group(1).replace(',', '.')} {m.group(2).replace('.', '')}"


def _variantes_typo(term: str, max_variantes: int | None = None) -> list[str]:
    """Genera variantes plausibles de un término mal escrito, para reintentar.

    El catálogo ya matchea Levenshtein ≤1 ("dreene"→"drene",
    "paracetmol"→"paracetamol"), así que borrar/duplicar UNA letra ya está
    cubierto: esas categorías van al final y por eso no hace falta gastar cupo
    en ellas. Lo que el catálogo NO alcanza es la DISTANCIA 2, donde domina la
    transposición de dos letras contiguas ("diclofencao"→"diclofenaco").

    Devuelve variantes en orden de probabilidad, sin repetir el original. El
    llamador las prueba contra el catálogo y se para en la primera que dé
    resultados: NUNCA se inventa un producto, solo se reformula la consulta.
    """
    t = (term or "").strip().lower()
    if not t or len(t) > 40:
        return []
    palabras = t.split()
    out: list[str] = []

    def _agrega(cand: str) -> None:
        cand = cand.strip()
        if cand and cand != t and cand not in out:
            out.append(cand)

    # Solo se corrige la ÚLTIMA palabra (la marca/fármaco); corregir la dosis
    # ("80" → "40") sería inventar la concentración que pidió el cliente.
    nucleo = palabras[-1] if palabras else ""
    if len(nucleo) < 4:
        return []
    prefijo = " ".join(palabras[:-1])

    def _variante_de(nueva: str) -> str:
        return f"{prefijo} {nueva}".strip() if prefijo else nueva

    # Presupuesto de intentos proporcional al largo de la palabra: una palabra
    # corta (≤6) casi nunca trae un typo de distancia 2 y el catálogo la cubre;
    # una larga sí, y tiene más posiciones donde fallar. Bounded para no gastar
    # una tormenta de consultas cuando el producto simplemente no existe.
    if max_variantes is None:
        max_variantes = min(12, max(5, len(nucleo)))

    # Orden: colapsado → TODAS las transposiciones → confusiones de grafía →
    # (relleno) una letra quitada/duplicada, que el catálogo ya resuelve.
    # La transposición necesita su cupo completo: en "diclofencao" la correcta
    # está en la posición 8 de 9, así que no se puede intercalar con otras
    # categorías ni quedarse con un cupo corto.
    colapsado = re.sub(r"(.)\1+", r"\1", nucleo)
    variantes: list[str] = []
    if colapsado != nucleo and len(colapsado) >= 4:
        variantes.append(colapsado)
    variantes += [
        nucleo[:i] + nucleo[i + 1] + nucleo[i] + nucleo[i + 2:]
        for i in range(len(nucleo) - 1)
    ]
    conf = [("z", "s"), ("s", "z"), ("c", "s"), ("s", "c"), ("b", "v"),
            ("v", "b"), ("ll", "y"), ("y", "ll"), ("qu", "c"), ("c", "qu")]
    variantes += [nucleo.replace(a, b, 1) for a, b in conf if a in nucleo]
    if nucleo.startswith("h"):
        variantes.append(nucleo[1:])
    if len(nucleo) >= 6:
        variantes += [nucleo[:i] + nucleo[i + 1:] for i in range(1, len(nucleo) - 1)]
    variantes += [nucleo[:i] + nucleo[i] + nucleo[i:] for i in range(len(nucleo))]

    for cand in variantes:
        _agrega(_variante_de(cand))
        if len(out) >= max_variantes:
            break
    return out[:max_variantes]


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
        # proveedor (dirección, horario, ciudad).
        self._provider_id_val = provider_id or ""
        # Formas de pago del tenant (markdown `paymenType` de providers/{id} en
        # Firestore). Se carga con buscar_medicamento/_info_provider; el resumen
        # del pedido lo muestra OBLIGATORIAMENTE (cada farmacia define el suyo).
        self.paymen_type: str | None = None
        self.provider_hours: str | None = None
        # Efectos observables por turn.py:
        self.handoff_reason: str | None = None  # se ejecuta DESPUÉS de la despedida
        self.booked = False
        self.routed_out = False
        self.proposed = False
        # Backstop de horario: evita forzar info_provider más de una vez por turno.
        self.info_provider_forced = False
        # true si el turno consultó el catálogo (buscar_medicamento o
        # sugerir_generico). Sirve como backstop anti-alucinación: si el usuario
        # preguntó por un medicamento y NO se consultó, forzamos la consulta.
        self.consulted_catalog = False
        # Último término consultado con buscar_medicamento (para re-consultar
        # cuando el cliente refina con miligramo/marca sin repetir el nombre).
        self.last_term = ""
        # Término de medicamento leído por OCR de la última imagen del cliente.
        # Cuando el turno actual solo REFERENCIA una imagen ("el producto de la
        # foto lo tienes?") sin aportar fármaco, se busca con ESTE término y no
        # con las palabras de la pregunta (buscar "foto" trae FOTORRETIN).
        self.last_ocr_term = ""
        # Corrección por typo: si el catálogo no encontró el término original y
        # SÍ lo encontró una variante ("diclofencao" → "diclofenaco"), se anotan
        # ambos para que la respuesta pueda confirmar la grafía al cliente en vez
        # de dejar la duda. None cuando no hubo corrección.
        self.corregido_desde: str | None = None
        self.corregido_a: str | None = None
        # Último producto consultado con buscar_medicamento. Lo usan los backstops
        # de carrito: si el cliente responde con una cantidad y el LLM no llama
        # agregar_al_carrito, forzamos el add con este producto.
        self.last_product: dict[str, Any] | None = None
        # Lista completa de productos consultados (backstop de contradicción).
        self.last_products: list[dict[str, Any]] = []
        # TÉRMINO DEL QUE SALIÓ `last_products`. Los backstops de turn.py componen el
        # título con `last_term` y las líneas con `last_products`: si ambos vienen de
        # búsquedas distintas, la respuesta mezcla dos consultas y el cliente recibe un
        # producto que NO pidió (ver `_lista_coincide_con_termino` en turn.py). Comparar
        # estos dos valores es un criterio EXACTO, a diferencia de comparar textos: el
        # camino de principio activo ('depomedrol' → 'METILPREDNISOLONA') tiene título y
        # productos con nombres legítimamente distintos, y una comparación textual lo
        # bloquearía.
        self.last_products_term: str = ""
        # Lista de opciones del último buscar_medicamento, ORDENADA por precio
        # (menor a mayor), tal como la muestra el formateador. Permite resolver
        # "quiero X cajas de la opción Z" en un turno nuevo.
        self.last_options: list[dict[str, Any]] = []
        # true cuando el backstop de carrito ya forzó el add este turno (evita loops).
        self.cart_forced = False
        # SKU reales que el BACKSTOP ya agregó al carrito en ESTE turno (pre-LLM
        # o en-loop). El LLM luego vuelve a llamar agregar_al_carrito con los
        # mismos productos (pensa que debe confirmar la selección); sin esto, el
        # ON CONFLICT de cart_add suma +1 y la cantidad sale doblada (pidió 1 y
        # quedan 2). Estos SKU se tratan como ya-agregados: idempotente.
        self.backstop_added_skus: set[str] = set()
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
        # PASO DE ENTREGA (delivery / retiro). Coordina la pregunta dentro del turno:
        #   delivery_pregunta_enviada → ya se envió el menú 1/2 en ESTE turno (evita
        #     repetirlo si el loop vuelve a pasar por el backstop).
        #   delivery_pregunta / delivery_pendiente → espejo del estado de la
        #     conversación, para no re-consultar la BD en cada vuelta del loop.
        self.delivery_pregunta_enviada = False
        self.delivery_pregunta = False
        self.delivery_pendiente = ""

    async def guardar_eleccion_entrega(self, metodo: str) -> None:
        """Guarda el MÉTODO DE ENTREGA elegido ('delivery' | 'pickup').

        Con 'pickup' no hay dirección que pedir: el resumen ya puede mostrarse.
        Con 'delivery' queda pendiente la dirección.
        """
        metodo = "pickup" if metodo == "pickup" else "delivery"
        pendiente = "address" if metodo == "delivery" else ""
        await self._ctx.store.update_conversation(
            self._conv.id,
            delivery_method=metodo,
            delivery_pending=pendiente,
        )
        self._conv.delivery_method = metodo
        self._conv.delivery_pending = pendiente
        self.delivery_pendiente = pendiente

    async def guardar_direccion_entrega(self, direccion: str) -> None:
        """Guarda la DIRECCIÓN de entrega y cierra la pregunta pendiente."""
        await self._ctx.store.update_conversation(
            self._conv.id,
            delivery_address=direccion,
            delivery_method="delivery",
            delivery_pending="",
        )
        self._conv.delivery_address = direccion
        self._conv.delivery_method = "delivery"
        self._conv.delivery_pending = ""
        self.delivery_pendiente = ""

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
        # GUARD DE ENTRADA: un término SIN NINGÚN token con cuerpo de fármaco no
        # puede ser una consulta de medicamento, por larga que sea la frase. El
        # catálogo es difuso y devuelve productos con los que comparte una letra
        # ('hasta' → PASTA PRIMOR, 'todo' → DESODORANTE DOVE), así que buscar con
        # relleno no es inocuo: el agente le muestra al cliente esos productos.
        # Casos reales: "No gracias no las voy a comprar y disculpe" (despedida
        # respondida con chocolates), "gracias por todo", "hasta luego".
        # Se comprueba ANTES de consultar: `filtrar_relevantes` protege la
        # salida, pero no vale la pena ni hacer la llamada.
        # GUARD DE PRESENTACIÓN: "soy víctor" / "me llamo Ana" no es una consulta de
        # medicamento aunque el nombre tenga "cuerpo de fármaco" (6 letras, sin ser
        # relleno). Sin esto el catálogo difuso busca el nombre de la PERSONA.
        # Caso real (conv 2720, 2026-10): el cliente escribió "Soy víctor" y el agente
        # respondió "No te tengo información sobre 'Víctor' en el catálogo".
        if parece_presentacion_personal(nombre):
            logger.info(
                "buscar_medicamento: '%s' es una PRESENTACIÓN de la persona — no busco",
                nombre,
            )
            return {
                "ok": False,
                "error": "presentacion_personal",
                "detalle": (
                    "El cliente se está PRESENTANDO (dijo su nombre), no pidiendo un "
                    "medicamento. NO busques su nombre en el catálogo y NUNCA digas que "
                    "no lo tienes: sería tratarlo como un producto. Salúdalo por su "
                    "nombre con calidez y pregúntale en qué puedes ayudarlo."
                ),
            }
        if not hay_senal_de_farmaco(nombre):
            logger.info(
                "buscar_medicamento: término '%s' sin señal de fármaco — no busco en catálogo",
                nombre,
            )
            return {
                "ok": False,
                "error": "no_medicamento",
                "detalle": (
                    "El cliente NO está pidiendo un medicamento: es cortesía, un "
                    "cierre o una despedida. Responde con naturalidad y brevedad "
                    "(agradece y despídete si corresponde), deja la puerta abierta "
                    "a que vuelva cuando necesite algo y NO muestres listas de "
                    "productos ni digas que 'no encontraste' nada."
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
        # El cliente inició una CONSULTA nueva de medicamento. El carrito SOLO se
        # reinicia si el pedido anterior ya se CERRÓ formalmente (LISTO /
        # finalizar_pedido). El flag `cart_summary_shown` NO sirve para esto:
        # se activa cada vez que se muestra el Resumen del Pedido, incluido el
        # flujo normal ("¿Deseas buscar otro medicamento?" → "no" → resumen →
        # "quiero atamel"), y ahí el cliente está AMPLIANDO el pedido, no
        # cerrándolo. Usarlo borraba el pedido recién armado (bug 29/09:
        # resumen de 10 productos → "si atamel" → resumen final con 1 solo).
        # `cart_closed` se activa únicamente en finalizar_pedido.
        if self._conv.cart_closed:
            logger.info(
                "buscar_medicamento: pedido previo cerrado — carrito nuevo para '%s'",
                nombre,
            )
            await self._ctx.store.cart_clear(self._conv.id)
            await self._ctx.store.update_conversation(
                self._conv.id, cart_closed=False
            )
        self.consulted_catalog = True
        # Normalizar tildes: el catálogo guarda 'potasico' sin tilde; si el
        # cliente escribe 'potásico', el motor no matchea (AND sobre tokens).
        nombre = _normalizar_tildes(nombre)
        data = await self._ctx.crm.get_products(self._provider_id, q=nombre, limit=20)
        self.last_term = nombre
        # Formas de pago del tenant (markdown `paymenType` de providers/{id}).
        # El CRM las devuelve en el mismo response; se cachean en el runtime para
        # el resumen del pedido.
        if data.get("paymenType"):
            self.paymen_type = str(data.get("paymenType"))
        if data.get("hours"):
            self.provider_hours = str(data.get("hours"))
        products = data.get("products") or []
        # FILTRO DE RELEVANCIA: el motor del CRM es difuso por diseño (Levenshtein
        # ≤1 y prefijos) porque los typos del cliente DEBEN funcionar
        # ('diclofencao' → DICLOFENAC). El precio de eso es que un término
        # conversacional devuelve productos con los que comparte una letra:
        # medido contra el catálogo real, 'hasta' → PASTA PRIMOR, 'tarda delivery'
        # → VENDA ELASTICA, 'muchas todo muy' → DESODORANTE DOVE. El agente le
        # mostraba esos productos al cliente como respuesta a una despedida.
        # Se descarta lo que no comparte señal real con el término, SIN tocar el
        # resultado cuando el filtro lo vaciaría (ver filtrar_relevantes).
        antes = len(products)
        products = filtrar_relevantes(nombre, products)
        if len(products) != antes:
            logger.info(
                "buscar_medicamento: '%s' — %d/%d productos descartados por relevancia",
                nombre, antes - len(products), antes,
            )
        # Dedupe por nombre de producto: el catálogo de Firebase repite el MISMO
        # ítem (mismo nombre) con distintos productId/precio (una entrada por
        # farmacia/precio). Quedarse con el de MENOR precio evita listas de 20
        # con 14 duplicados idénticos.
        products = _dedupe_por_nombre(products)

        # REINTENTO POR EL FÁRMACO CABEZA: la frase completa no trajo el fármaco al
        # catálogo (el motor del CRM matchea TODOS los tokens, así que "magnesio plus
        # life" devuelve lo que comparte 'life' y ni un magnesio). El cliente nombra el
        # fármaco PRIMERO y añade descriptores después ('plus', 'life', la marca): se
        # reintenta con ese token, que en el catálogo del provider 27 devuelve los 10
        # magnesios reales.
        #
        # Va ANTES del acortamiento porque es más preciso: el acortamiento solo quita
        # la cola, mientras que aquí se aísla el fármaco. Y se RE-FILTRA con el término
        # original, como los demás reintentos, para no aflojar la relevancia.
        if not products:
            cabeza = _token_cabeza_farmaco(nombre)
            if cabeza and cabeza != nombre.lower():
                data_c = await self._ctx.crm.get_products(
                    self._provider_id, q=cabeza, limit=20
                )
                candidatos = _dedupe_por_nombre(data_c.get("products") or [])
                products = filtrar_relevantes(nombre, candidatos)
                if products:
                    logger.info(
                        "buscar_medicamento: '%s' sin resultados — encontrado por el "
                        "fármaco cabeza '%s' (%d productos)",
                        nombre, cabeza, len(products),
                    )
                    self.last_term = cabeza
                    data = data_c
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
                # RE-FILTRAR el reintento. Sin esto, un fallback que afloja el término
                # devuelve CUALQUIER producto que comparta una palabra genérica.
                # Caso real: 'dovilin jarabe adulto' → el filtro lo vacía (DOVILIN no
                # existe) → aquí se prueba 'dovilin adulto' → el catálogo devuelve 16
                # productos con 'adulto' (ELECTRODO DESECHABLE ADULTO, RECOLECTOR DE
                # ORINA ADULTO, CANULA NASAL ADULTO...) y se le mostrarían al cliente
                # como respuesta a su consulta de DOVILIN. El acortamiento sirve para
                # quitar ruido del TÉRMINO (un OCR verboso), nunca para aflojar la
                # RELEVANCIA.
                products = _dedupe_por_nombre(data.get("products") or [])
                products = filtrar_relevantes(nombre, products)
                if products:
                    self.last_term = fallback
        if not products:
            # SEGUNDA PASADA por variantes de escritura ANTES de rendirse.
            # El catálogo matchea Levenshtein ≤1; esto cubre distancia 2 y las
            # grafías alternativas ("diclofencao"→"diclofenaco",
            # "omeprasol"→"omeprazol"). Se prueban en orden y se para en la
            # primera que dé resultados; nunca se inventa un producto, solo se
            # reformula la consulta. El término encontrado se guarda como
            # `last_term` para que el refinamiento posterior ("de 500 mg") siga
            # funcionando sobre la grafía correcta.
            for variante in _variantes_typo(nombre):
                data_v = await self._ctx.crm.get_products(
                    self._provider_id, q=variante, limit=20
                )
                # RE-FILTRAR la variante. Una variante de typo cambia el término, así
                # que su relevancia hay que re-evaluarla contra el término ORIGINAL.
                # Sin esto: 'dovilin jarabe adulto' (DOVILIN no existe) prueba la
                # variante 'dovilin jarabe aulto', el catálogo la resuelve por fuzzy a
                # los mismos 4 jarabes ajenos, y se le mostrarían al cliente como si
                # fueran su DOVILIN.
                candidatos = _dedupe_por_nombre(data_v.get("products") or [])
                products = filtrar_relevantes(nombre, candidatos)
                if products:
                    logger.info(
                        "buscar_medicamento: '%s' sin resultados — encontrado con la "
                        "variante '%s' (%d productos)",
                        nombre, variante, len(products),
                    )
                    self.corregido_desde = nombre
                    self.corregido_a = variante
                    self.last_term = variante
                    data = data_v
                    break
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
                # El principio activo SÍ corresponde a este término (el LLM lo mapeó
                # desde él), así que la lista está en contexto: el guard no la invalida.
                self.last_products_term = nombre
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
            # LA BÚSQUEDA FALLIDA INVALIDA LA LISTA ANTERIOR. `last_products` guarda la
            # lista de la ÚLTIMA búsqueda CON RESULTADOS, y los backstops de turn.py la
            # usan con `last_term` como TÍTULO. Si este turno buscó un medicamento y no
            # hay nada, dejar la lista vieja produce una respuesta que mezcla DOS
            # consultas distintas.
            #
            # Caso real (provider 05, 2026-10): el cliente mandó una receta de
            # ALPRAZOLAM. En el mismo turno el LLM había buscado 'Ibuprofeno 200 mg'
            # (tradujo la caja BRUGESIC de la imagen anterior) → last_products =
            # [BRUDOL (IBUPROFENO - CAFEINA) 200 MG]. Luego buscó 'alprazolam' → 0.
            # El backstop de contradicción tomó el TÍTULO del término nuevo y la LISTA
            # del viejo:
            #
            #     ALPRAZOLAM
            #     💊 1. BRUDOL (IBUPROFENO - CAFEINA) 200 MG X 20 COMP
            #
            # Un antiinflamatorio con cafeína presentado como el ansiolítico que pidió.
            # Peor que decir "no disponible": el cliente puede comprar el medicamento
            # equivocado. El título y la lista deben venir SIEMPRE del mismo turno.
            #
            # `last_term` NO se limpia a propósito: el backstop de refinamiento construye
            # `f"{last_term} {ref}"` y, sin él, un "el de 50 mg" posterior se buscaría
            # como '50 mg' → fuzzy a cualquier cosa. El prompt también lo cita como
            # "Última búsqueda". Lo que se invalida es la LISTA, nunca el término.
            if self.last_products:
                logger.info(
                    "buscar_medicamento: '%s' sin resultados — invalido last_products "
                    "(%d productos de una búsqueda anterior)",
                    nombre, len(self.last_products),
                )
            self.last_products = []
            self.last_options = []
            self.last_product = None
            await self._log_med_query(nombre, [], added_to_cart=False)
            return {
                "ok": False,
                "error": "sin_resultados",
                "detalle": (
                    f"no encontrado '{nombre}' en el catálogo. Recuérdalo: SI YA has "
                    "respondido un 'no encontrado' en este hilo, NO repitas la misma "
                    "frase. Antes de decir 'no disponible': 1) si el nombre puede tener "
                    "errores de tipeo, reintenta con la grafía más probable (p. ej. "
                    "'lupripiu'→'lopirel'/'lupirad', 'lozartan'→'losartan'); 2) si el "
                    "cliente dio una FORMA (óvulos, crema, jarabe, gotas, vaginal) pero "
                    "no el fármaco, pídele el nombre exacto de la caja o busca por esa "
                    "presentación; 3) aprovecha el DATO NUEVO que el cliente agregó en "
                    "este mensaje ('similar', 'genérico', 'de marca', la forma) y "
                    "reintenta con él. Si NADA matchea, informa honestamente que no lo "
                    "tienes disponible, MUESTRA EMPATÍA, deja el chat abierto y haz UNA "
                    "pregunta NUEVA y distinta a cualquier anterior (p. ej. ¿traes el "
                    "nombre que está en la caja?, ¿te sirve otra presentación?). NO lo "
                    "repitas ni lo pases a un humano automáticamente por un medicamento "
                    "agotado."
                ),
                "busqueda": nombre,
            }
        # Recordar el primer producto (para el backstop de carrito).
        self.last_product = products[0]
        # Lista completa de productos consultados (para el backstop de
        # contradicción: si el LLM niega disponibilidad pese a haber resultados,
        # reemplazamos su texto con la lista real).
        self.last_products = products
        # Con qué término se obtuvo esta lista. Los backstops componen el título con
        # `last_term`, así que si `last_term` cambia (búsqueda posterior sin resultados)
        # la lista queda huérfana y hay que invalidarla (ver el `if not products` arriba).
        self.last_products_term = nombre
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
            # CONSERVAR LA DOSIS. El nombre de origen puede traer el mg ("esoz 40
            # mg", "atorvastatina 80 mg") y el LLM devuelve solo el principio
            # activo SIN ella ("omeprazol"). Buscar sin la dosis devolvía TODAS
            # las concentraciones mezcladas (20 y 40 mg en la misma lista — el
            # caso reportado de la receta "ESOZ 40 MG"). Se reinyecta la dosis
            # que el cliente ya pidió; si no traía, se busca igual que antes.
            dosis = _extraer_dosis(nombre)
            consulta = f"{principio} {dosis}".strip() if dosis else principio
            data = await self._ctx.crm.get_products(
                self._provider_id, q=consulta, limit=20
            )
            products = data.get("products") or []
            # Filtrar accesorios/insumos (jeringas, agujas, tiras): el principio
            # activo 'insulina' matchea la jeringa, que NO es el fármaco que el
            # cliente pidió. Si solo quedan accesorios, devolver [] para que el
            # agente diga honestamente que el medicamento no está disponible.
            filtrados = _filtrar_accesorios(products)
            # Si al añadir la dosis no queda NADA pero sin ella sí había
            # resultados, se devuelven los del principio activo: mejor ofrecer
            # las concentraciones disponibles (el cliente elige) que negar el
            # medicamento por una dosis que este catálogo no maneja.
            if not filtrados and dosis:
                data = await self._ctx.crm.get_products(
                    self._provider_id, q=principio, limit=20
                )
                filtrados = _filtrar_accesorios(data.get("products") or [])
            return filtrados
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
        # Corrección de escritura: el cliente escribió mal el nombre y el
        # catálogo lo encontró con otra grafía. Se lo decimos al LLM para que lo
        # mencione ("asumí que buscabas X") — es lo que evita que el cliente vea
        # una lista de algo que no pidió y desconfíe.
        if self.corregido_desde and self.corregido_a:
            base += (
                f" OJO: el cliente escribió '{self.corregido_desde}' y en el catálogo "
                f"aparece como '{self.corregido_a}'. Empieza la respuesta confirmando "
                "la corrección en UNA línea amable (p. ej. "
                f"\"Asumí que buscas {self.corregido_a.upper()} 👍\") y luego muestra los "
                "productos. NUNCA digas que no lo tienes."
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
        provider = data.get("provider") or data or {}
        if not provider:
            return {"ok": False, "error": "sin_provider_info", "detalle": "no hay info de la farmacia"}
        # Cachear las formas de pago del tenant (markdown) para el resumen.
        if provider.get("paymenType"):
            self.paymen_type = str(provider.get("paymenType"))
        if provider.get("hours"):
            self.provider_hours = str(provider.get("hours"))
        return {
            "ok": True,
            "provider": provider,
            "formaDePago": self.paymen_type,
            "horario": self.provider_hours,
            "instrucciones": (
                "responde con dirección, horario y ciudad de la farmacia. Si el "
                "cliente preguntó el HORARIO, cita el valor del campo 'hours' tal "
                "cual (es el horario real de ESTA farmacia) y NO ofrezcas pasar la "
                "consulta a un humano ni digas que no tienes la información. Si el "
                "cliente pregunta las formas de pago, cítalas y compártele la "
                "formaDePago."
            ),
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
        # MULTI-TENANT / DEDUP del carrito: el LLM a veces pasa el NÚMERO DE
        # OPCIÓN como productId ('1','2','3'...) en vez del SKU real del catálogo,
        # y/o omite precioBs. Eso crea filas DUPLICADAS —mismo producto con otro
        # product_id— y subtotales sin Bs en el resumen. Si el productId es un
        # índice contra last_options (la lista real que el cliente vio, ordenada
        # por precio) o un SKU real, lo resolvemos al producto canónico para
        # deduplicar y garantizar precios USD+Bs reales.
        if self.last_options:
            resolved: dict[str, Any] | None = None
            if product_id.isdigit():
                idx = int(product_id) - 1
                if 0 <= idx < len(self.last_options):
                    resolved = self.last_options[idx]
            else:
                resolved = next(
                    (p for p in self.last_options if str(p.get("productId") or "") == product_id),
                    None,
                )
            if resolved is not None:
                sku = str(resolved.get("productId") or "").strip()
                if sku:
                    product_id = sku
                # Precios SIEMPRE del catálogo real (nunca del LLM).
                if resolved.get("precio") is not None:
                    args["precioUsd"] = resolved.get("precio")
                if resolved.get("precioBs") is not None:
                    args["precioBs"] = resolved.get("precioBs")
                if not producto:
                    producto = str(
                        resolved.get("producto")
                        or resolved.get("title")
                        or resolved.get("titulo")
                        or ""
                    )
                if resolved.get("presentacion") is not None:
                    args["presentacion"] = resolved.get("presentacion")
                if resolved.get("laboratorio") is not None:
                    args["laboratorio"] = resolved.get("laboratorio")
        # Idempotencia del backstop: este turno el backstop YA agregó este SKU
        # (con su cantidad correcta). Si el LLM vuelve a llamar agregar_al_carrito
        # con el mismo producto (confirmando la selección), NO lo re-suminamos:
        # cart_add haría ON CONFLICT ... cantidad + 1 y la cantidad saldría
        # doblada (pidió 1 caja y quedan 2). Devolvemos ok sin tocar el carrito.
        sku_final = str(product_id or "").strip()
        if sku_final and sku_final in self.backstop_added_skus:
            return {
                "ok": True,
                "dedup": True,
                "item": {
                    "productId": sku_final,
                    "producto": producto,
                    "cantidad": cantidad,
                    "precioUsd": args.get("precioUsd"),
                    "precioBs": args.get("precioBs"),
                },
                "instrucciones": (
                    "confirma en una línea que quedó agregado (cantidad + producto). "
                    "Luego pregunta de forma breve si desea buscar otro medicamento "
                    "(SI/NO). No vuelvas a llamar agregar_al_carrito para lo mismo."
                ),
            }
        # IDEMPOTENCIA ENTRE TURNOS: el producto YA está en el carrito con la MISMA
        # cantidad que se pide ahora → el LLM está re-confirmando una selección que ya
        # se hizo, no agregando más unidades. Sin esto la cantidad se DOBLA.
        #
        # Caso real (provider 05, 2026-10): el cliente dijo "si" al resumen y el LLM
        # volvió a llamar agregar_al_carrito + finalizar_pedido en la misma ronda. El
        # carrito ya tenía 1 caja; cart_add (ON CONFLICT cantidad + EXCLUDED.cantidad)
        # la habría dejado en 2 — el cliente veía un resumen de $0,52 y el pedido
        # registrado cobraba $1,04. El `backstop_added_skus` de arriba solo cubre el
        # MISMO turno del backstop, no una re-confirmación en un turno posterior.
        #
        # Solo se deduplica si la cantidad coincide EXACTAMENTE: "quiero 2 más" sí es
        # un aumento legítimo y debe sumar.
        if sku_final:
            existentes = await self._ctx.store.cart_items(
                self._conv.id, session_hours=self._ctx.settings.cart_session_hours
            )
            for ex in existentes:
                if str(ex.product_id or "").strip() == sku_final and ex.cantidad == cantidad:
                    logger.info(
                        "agregar_al_carrito: '%s' ya está en el carrito con la misma "
                        "cantidad (%d) — no se duplica",
                        producto or sku_final, cantidad,
                    )
                    return {
                        "ok": True,
                        "dedup": True,
                        "item": {
                            "productId": sku_final,
                            "producto": ex.producto,
                            "cantidad": ex.cantidad,
                            "precioUsd": ex.precio_usd,
                            "precioBs": ex.precio_bs,
                        },
                        "instrucciones": (
                            "confirma en una línea que quedó agregado (cantidad + producto). "
                            "SI el cliente acaba de confirmar el pedido, llama "
                            "finalizar_pedido; NO vuelvas a llamar agregar_al_carrito "
                            "para lo mismo."
                        ),
                    }
        presentacion = str(args.get("presentacion") or "")
        laboratorio = str(args.get("laboratorio") or "")
        precio_usd = args.get("precioUsd")
        precio_bs = args.get("precioBs")
        # EL Bs FALTANTE SE RECUPERA DEL CATÁLOGO. `precioBs` no es `required` en el
        # schema de la tool, así que el LLM lo omite a menudo: medido en la BD, 7 de 32
        # filas de `bot_cart` quedaron sin Bs (22%), y `ver_carrito` las mostraba como
        # "Bs 0,00" con `(i.precio_bs or 0)`.
        #
        # El dato SÍ existe: el CRM devuelve `precioBs` en el 100% de los productos
        # (verificado: 10/10).
        #
        # DOS INTENTOS, por orden de fiabilidad:
        #   1) por productId — exacto, pero el LLM a veces INVENTA el id
        #      ('COLON VITAL LIFE X 6', 'tilodron-jbe-120ml' en la BD real), así que
        #      este camino puede no encontrar nada.
        #   2) por NOMBRE — el `producto` sí viene del catálogo. Se exige coincidencia
        #      exacta (normalizada) para no colgarle el precio de otro medicamento.
        #
        # Nunca se inventa: sin dato, se deja vacío y el formateador OMITE el monto en
        # vez de mostrar "Bs 0,00".
        if precio_bs is None and self._provider_id and (product_id or producto):
            try:
                data_bs = await self._ctx.crm.get_products(
                    self._provider_id, q=producto or product_id, limit=10
                )
                candidatos = data_bs.get("products") or []
                elegido = None
                # 1) por productId (SKU real del catálogo)
                if product_id:
                    elegido = next(
                        (c for c in candidatos
                         if str(c.get("productId") or "").strip() == str(product_id).strip()),
                        None,
                    )
                # 2) por NOMBRE exacto (normalizado): cubre el id inventado
                if elegido is None and producto:
                    objetivo = _norm(producto)
                    elegido = next(
                        (c for c in candidatos
                         if _norm(str(c.get("producto") or c.get("nombre") or "")) == objetivo),
                        None,
                    )
                if elegido is not None:
                    if elegido.get("precioBs") is not None:
                        precio_bs = elegido.get("precioBs")
                    if precio_usd is None and elegido.get("precio") is not None:
                        precio_usd = elegido.get("precio")
                    # Si el id del LLM era inventado, se guarda el SKU REAL del catálogo:
                    # así el carrito deduplica por producto y no por id alucinado.
                    sku_real = str(elegido.get("productId") or "").strip()
                    if sku_real and sku_real != str(product_id).strip():
                        logger.info(
                            "agregar_al_carrito: productId '%s' (no era un SKU) → '%s'",
                            product_id, sku_real,
                        )
                        product_id = sku_real
                    if precio_bs is not None:
                        logger.info(
                            "agregar_al_carrito: Bs recuperado del catálogo para "
                            "'%s' (Bs %s)", producto or product_id, precio_bs,
                        )
            except Exception as exc:
                # Best-effort: sin Bs el resumen lo omite, pero el carrito funciona.
                logger.warning(
                    "agregar_al_carrito: no pude recuperar el Bs de '%s': %s",
                    producto or product_id, exc,
                )
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
        # EL CARRITO CAMBIÓ DESPUÉS DEL RESUMEN: el flag `cart_summary_shown` debe
        # volver a False. Si no, el cliente que ya vio el resumen, agrega otra cosa y
        # dice "LISTO" finalizaría SIN ver el resumen actualizado — cerrando un pedido
        # cuyo detalle nunca vio. El propósito del flag es "el resumen MOSTRADO
        # corresponde al carrito ACTUAL", no "alguna vez se mostró un resumen".
        #
        # Caso real (provider 05, 2026-10): el cliente dijo "no" → vio el resumen →
        # dijo "si" y volvió a ver el MISMO resumen (el guard de finalizar lo exigía
        # porque `cart_summary_text` es por turno). Con el flag persistente, "si"
        # finaliza; pero si entre el resumen y el "si" agrega otra caja, hay que
        # re-resumir para que el detalle refleje lo que realmente va a comprar.
        if self._conv.cart_summary_shown:
            await self._ctx.store.update_conversation(
                self._conv.id, cart_summary_shown=False
            )
            self._conv.cart_summary_shown = False
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
        # El Resumen del Pedido "cierra" este carrito: la siguiente consulta de
        # medicamento arranca uno nuevo (no acumula sobre este). Se persiste el
        # flag para que persista entre turnos (el resumen suele verse en un turno
        # y la consulta nueva llega en otro).
        await self._ctx.store.update_conversation(
            self._conv.id, cart_summary_shown=True
        )
        total_usd = sum((i.precio_usd or 0) * i.cantidad for i in items)
        total_bs = sum((i.precio_bs or 0) * i.cantidad for i in items)
        # ¿Algún ítem SIN Bs? `bot_cart` puede tenerlo en None (el LLM omitió el dato
        # al agregar). Mostrar "Bs 0,00" es un precio FALSO: el cliente creería que el
        # medicamento es gratis en bolívares. Cuando falta, se OMITE el monto en Bs de
        # esa línea (y del total) en vez de inventar un cero.
        hay_bs = all(i.precio_bs is not None for i in items)
        # Resumen determinista: cada producto con cantidad y subtotal en USD y
        # Bs, y el total en ambos. El LLM lo cita literal; turn.py lo usa como
        # backstop para que el monto en Bs y el subtotal por medicamento SIEMPRE
        # aparezcan, aunque el modelo omita el formato.
        bloque = []
        bloque.append("🛒 *Productos:*")
        for i in items:
            sub_usd = (i.precio_usd or 0) * i.cantidad
            bloque.append(f"•⁠  ⁠{i.producto}")
            bloque.append(f"  Cantidad: {i.cantidad}")
            if i.precio_bs is not None:
                sub_bs = i.precio_bs * i.cantidad
                bloque.append(
                    f"  Subtotal: ${_fmt_ve(sub_usd)} | Bs {_fmt_ve(sub_bs)}"
                )
            else:
                bloque.append(f"  Subtotal: ${_fmt_ve(sub_usd)}")
        bloque.append("")
        bloque.append("*Total:*")
        if hay_bs:
            bloque.append(f"${_fmt_ve(total_usd)} | Bs {_fmt_ve(total_bs)}")
        else:
            bloque.append(f"${_fmt_ve(total_usd)}")
        bloque.append("")
        # Formas de pago del tenant (multitenant): OBLIGATORIO mostrarlas en el
        # resumen del pedido. Vienen del campo `paymenType` (markdown) de
        # providers/{id} en Firestore que el dueño edita. Si no quedaron
        # cacheadas en este turno (el resumen suele pedirse en un turno distinto
        # al de la búsqueda), se recargan vía /api/bot/products (que devuelve
        # provider + paymenType + hours).
        pago = self.paymen_type
        if not pago and self._provider_id:
            try:
                data = await self._ctx.crm.get_products(self._provider_id, q="", limit=1)
                pago = str(data.get("paymenType") or "") or None
                if pago:
                    self.paymen_type = pago
                    self.provider_hours = str(data.get("hours")) or self.provider_hours
            except Exception:
                pago = None
        if pago:
            bloque.append("💳 *Formas de pago:*")
            bloque.append(pago)
            bloque.append("")
        # MÉTODO DE ENTREGA: el cliente eligió delivery (con dirección) o retiro en
        # farmacia ANTES de llegar al resumen. Se muestra siempre que haya elección,
        # para que el pedido quede sin ambigüedad sobre cómo se entrega.
        metodo = self._conv.delivery_method
        if metodo == "delivery":
            bloque.append("🚚 *MÉTODO DE ENTREGA:*")
            bloque.append(f"Delivery — {self._conv.delivery_address or ''}".rstrip(" —"))
            bloque.append("")
        elif metodo == "pickup":
            bloque.append("🏥 *MÉTODO DE ENTREGA:*")
            bloque.append("Retirar en Farmacia")
            bloque.append("")
        bloque.append(
            "¿Confirmas el pedido con un *SI*, o quieres agregar otro medicamento?"
        )
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
        # El pedido queda CERRADO: la próxima consulta de medicamento arranca un
        # carrito nuevo en vez de acumular sobre el ya procesado. Esto es lo que
        # distingue "finalicé el pedido" de "solo vi el resumen y quiero seguir
        # agregando" (que mantiene el carrito vivo).
        await self._ctx.store.update_conversation(self._conv.id, cart_closed=True)
        # Si el Resumen del Pedido ya mostró las formas de pago (ver_carrito), NO
        # repetirlas en el mensaje final. SE LEE ANTES DE LIMPIAR EL FLAG: si se lee
        # después, `cart_summary_shown` ya estaría en False y el agente repetiría
        # siempre las formas de pago.
        summary_shown = bool(getattr(self._conv, "cart_summary_shown", False))
        try:
            conv_fresca = await self._ctx.store.get_or_create_conversation(
                self._conv.wa_identity
            )
            summary_shown = bool(conv_fresca.cart_summary_shown)
        except Exception:
            pass
        # EL PEDIDO SE CERRÓ: se limpia TODO el estado del pedido anterior — entrega
        # incluida. El próximo pedido debe volver a preguntar delivery/retiro en vez de
        # heredar la dirección o el "Retirar en Farmacia" del anterior.
        #
        # `cart_summary_shown` TAMBIÉN se limpia (hallazgo de la auditoría de estado):
        # se quedaba en True tras cerrar, y como el paso de entrega exige
        # `not cart_summary_shown`, el pedido NUEVO se SALTABA la pregunta de entrega y
        # su resumen salía sin MÉTODO DE ENTREGA. Medido en la BD: 3 conversaciones
        # cerradas con el flag colgado, listas para reproducir el fallo.
        #
        # Solo se limpia AQUÍ (pedido cerrado): ver el resumen NO cierra nada, el
        # cliente puede seguir agregando.
        await self._ctx.store.update_conversation(
            self._conv.id,
            delivery_method=None,
            delivery_address=None,
            delivery_pending="",
            cart_summary_shown=False,
        )
        self._conv.delivery_method = None
        self._conv.delivery_address = None
        self._conv.delivery_pending = ""
        self._conv.cart_summary_shown = False
        # Formas de pago del tenant (multitenant, field `paymenType` de Firestore)
        # para recordarle al cliente cómo puede pagar — SOLO si el resumen no las
        # mostró ya (evitar duplicación del bloque).
        pago = None
        if not summary_shown:
            pago = self.paymen_type
            if not pago and self._provider_id:
                try:
                    data = await self._ctx.crm.get_products(self._provider_id, q="", limit=1)
                    pago = str(data.get("paymenType") or "") or None
                except Exception:
                    pago = None
        return {
            "ok": True,
            "total": total,
            "formaDePago": pago,
            "instrucciones": (
                "agradece, confirma que el pedido quedó registrado y que un "
                "humano lo procesará y despídete con puerta abierta. NO inventes "
                "folios ni tiempos de entrega."
                + (
                    " El Resumen del Pedido ya mostró las formas de pago; NO "
                    "las repitas aquí."
                    if summary_shown
                    else (
                        " Recuérdale las formas de pago disponibles (usa "
                        "formaDePago)."
                    )
                )
            ),
        }
