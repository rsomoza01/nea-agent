"""Herramientas del LLM: update_ficha, propose_slots, book_session, route_out, handoff.

La validación es server-side: `book_session` SOLO acepta slots previamente
ofrecidos (tabla offered_slots, comparación por epoch exacto). Un fallo del
CRM dentro de una tool regresa `{"ok": false, ...}` al LLM — nunca tumba el
turno.
"""
from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.crm import CrmConflict, CrmError, SlotTaken
from app.relevancia import (
    _SALES_FARMACEUTICAS,
    _es_letra_de_vitamina,
    _norm,
    _token_cabeza_farmaco,
    es_relevante,
    es_relevante_estricto,
    filtrar_relevantes,
    hay_senal_de_farmaco,
    parece_presentacion_personal,
    tokens_farmaco,
)
from app.aliases import reescribir as _reescribir_alias

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


# Muletillas del HABLA que nunca identifican un medicamento por sí solas. Una nota de voz
# trae cortesía y coletillas que el cliente jamás escribe como consulta ("buenos días mi
# amor... verdad?"), y el LLM las extrae como si fueran fármacos.
#
# NO se usa `_MULETILLAS_HABLA` de turn.py: tools.py no puede importarlo (import circular).
#
# LISTA MEDIDA CONTRA EL CATÁLOGO REAL: contiene solo las palabras que NO son token de
# ningún producto del catálogo (33 de 35 verificadas). Se EXCLUYERON deliberadamente:
#   'mi'   → token real de 'PAÑAL MI BEBE'
#   'dias' → token real de 'DYMAZOL 6 DIAS'
#   'sol'  → token real de 11 productos ('ACETAMINOFEN SOL GOTAS')
# Un cliente que busque 'dias' o 'sol' solo no debe perder su producto.
#
# El gate exige que TODOS los tokens del término sean muletillas, así que 'venda sol' y
# 'dymazol 6 dias' siguen pasando (tienen un token con cuerpo de fármaco).
_MULETILLAS_HABLA_GATE = {
    "amor", "verdad", "cierto", "buenas", "buenos", "tardes", "noches",
    "saludos", "hola", "mijo", "mija", "linda", "lindo", "corazon",
    "disculpe", "disculpa", "permiso", "mire", "mira", "vea", "oiga",
    "favor", "porfa", "gracias", "agradezco", "amable", "regalame", "deme",
    "entonces", "pues", "bueno", "okey", "okay",
}

# Muletillas que TAMBIÉN son token de algún producto real ('mi' en 'PAÑAL MI BEBE', 'dias'
# en 'DYMAZOL 6 DIAS'). Cuentan para rechazar un término MULTIPALABRA ('buenos dias', 'mi
# amor' — el saludo completo), pero NUNCA por sí solas: si el cliente escribe 'dias' o 'mi'
# a secas, su producto existe y hay que buscarlo.
_MULETILLAS_DEBILES = {"mi", "dias"}


def _es_solo_muletilla_habla(term: str) -> bool:
    """True si el término es SOLO cortesía/coletilla del habla, no un medicamento.

    Caso real (conv 2834, provider 27): el cliente mandó una nota de voz —
    "Buenos días mi amor, en qué precio tienen la venda sol? La caja trae dos, verdad?" —
    y el LLM extrajo dos "medicamentos" que son el saludo y la coletilla. Se consultaron y
    el cliente recibió:
        ⚠️ No disponibles en el catálogo: DÍAS AMOR, VERDAD
    Ni 'amor' ni 'verdad' son productos: medido contra el catálogo, no son token de NINGÚN
    nombre de producto.

    REGLA: todos los tokens son muletillas, con DOS salvedades medidas:
      - un token FUERTE solo ('amor', 'gracias') → rechaza;
      - un token DÉBIL solo ('dias', 'mi') → NO rechaza, porque 'DYMAZOL 6 DIAS' y
        'PAÑAL MI BEBE' existen y el cliente escribiría eso literal;
      - varios tokens, todos muletillas ('buenos dias', 'mi amor') → rechaza (es el saludo
        completo; ninguno de sus tokens identifica un fármaco por separado).

    Se exige que TODOS sean muletillas: 'venda sol' y 'dymazol 6 dias' siguen pasando
    porque tienen un token con cuerpo de fármaco.
    """
    palabras = re.findall(r"[a-z0-9]+", _normalizar_tildes((term or "").lower()))
    if not palabras:
        return False
    todas = _MULETILLAS_HABLA_GATE | _MULETILLAS_DEBILES
    if not all(w in todas for w in palabras):
        return False
    # Un token DÉBIL solo no basta para rechazar: su producto existe en el catálogo.
    if len(palabras) == 1 and palabras[0] in _MULETILLAS_DEBILES:
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
# VOCATIVO / CARIÑO: el cliente saluda y llama al agente "mi amor", "corazón", "cariño".
# Caso real (provider 32, 2026-10-29): "Buenas tardes!! Mi amor que precio tiene la
# nitrofurantoina de 50mg y la vitamina C" → el agente buscó 'AMOR NITROFURANTOINA 50MG',
# avisó "No tengo NITROFURANTOINA. De AMOR 50MG hay esto" y cotizó TE AMO PRESERV. 3PCS.
VOCATIVOS = {
    "amor", "cariño", "carino", "corazon", "corazón", "bebe", "bebé", "preciosa",
    "precioso", "linda", "lindo", "hermosa", "hermoso", "bonita", "bonito",
    "principe", "príncipe", "reina", "rey", "amiga", "amigo", "senora", "señora",
    "senor", "señor", "doctor", "doctora", "licenciada", "licenciado", "jefe", "jefa",
    "vecina", "vecino", "pana", "panita", "compatriota",
}
# INTENCIÓN DE COMPRA: no es un medicamento.
INTENCION_COMPRA = {
    "quiero", "queria", "quería", "quisiera", "querer", "querría", "querria",
    "deseo", "deseamos", "llevo", "llevare", "llevaré", "llevaria", "llevaría",
    "apartame", "apártame", "separame", "sepárame", "encargame", "encárgame",
}
# REFERENCIA A LA LISTA ANTERIOR: 'de la opción 9', 'la número 3'.
REFERENCIA_LISTA = {
    "opcion", "opción", "opciones", "opcioncita", "numerito",
}

# «¿SOLO LO TIENES POR SEPARADO?» — es una PREGUNTA sobre la respuesta anterior, no un
# producto. Caso real (provider 32, 2026-10-29): tras cotizar la caja de FIN-AL-GRIP FORTE
# como si fueran cuatro medicamentos, el cliente preguntó "No entendí?? Solo lo tienes por
# separado?" y el agente contestó "⚠️ No disponibles en el catálogo: SEPARADO" con un
# ENTERUNI al lado.
POR_SEPARADO = {
    "separado", "separada", "separados", "separadas", "suelto", "suelta", "sueltos",
    "sueltas", "individual", "individuales", "independiente", "independientes", "aparte",
}

# «NO ENTENDÍ» — el cliente pide una ACLARACIÓN, no un medicamento. Sin esto, "No entendí??
# Solo lo tienes por separado?" se buscaba como 'ENTENDI SEPARADO' (provider 32, 2026-10-29).
COMPRENSION = {
    "entendi", "entiendo", "entender", "entendido", "entendiste", "entendieron",
    "comprendi", "comprendo", "comprender", "comprendido", "comprende",
    "repite", "repites", "repetir", "repitas", "reitero", "reiteras",
    "explicame", "explicar", "explicas", "explicacion", "aclarame", "aclarar", "aclaracion",
    "confundi", "confundido", "confundida", "duda", "dudas",
}

# FELICITACIONES / DESPEDIDA DE BUENAS NOCHES: "Feliz noche, ¿el precio de la buscapina?"
# no es un medicamento llamado "FELIZ NOCHE BUSCAPINA" (provider 32, 2026-10-29).
FELICITACION = {
    "feliz", "felicidad", "felices", "abrazo", "abrazos", "beso", "besitos", "besos",
    "bendiciones", "bendicion", "próspero", "prospero", "prospera", "éxito", "exito",
    "exitos", "excelente", "excelentes", "lindas", "hermosos", "bonita", "bonito",
}

# SALUDOS DE TIEMPO EN SINGULAR: "buenas noches" ya estaba, el 'noche' suelto y
# 'feliz noche' no. Son la misma muletilla (provider 32, 2026-10-29).
SALUDO_TIEMPO = {
    "noche", "tarde", "dia", "día", "manana", "mañana", "madrugada",
    "nochecita", "medio dia", "medio dia",
}

# BENDICIONES AL SALUDAR: "Hola buen día, Dios le bendiga" no es una línea de productos
# (provider 32, 2026-10-30: el agente cotizó COLLAR DIOS / COLLAR PERLA FE CORAZON DIOS
# para una receta de 5 medicamentos que venía en la foto).
BENDICIONES = {
    "dios", "diosito", "bendiga", "bendice", "bendecir", "bendecida", "bendecido",
    "bendecidos", "jesus", "jesús", "cristo", "virgen", "amen", "amén", "salmo",
    "oracion", "oración", "bendicion", "bendiciones",
}

# «ME DAN EL PRESUPUESTO DE ESTOS MEDICAMENTOS»: el verbo de la petición y el
# demostrativo no son el nombre de nada. Sin esto quedaba 'dad estos' como término
# (provider 32, 2026-10-30).
DEMOSTRATIVOS = {
    "dad", "dan", "dame", "danos", "da", "den", "denme", "deme", "darme", "darnos",
    "estos", "estas", "este", "esta", "esto", "esos", "esas", "eso", "aquel",
    "aquellos", "aquellas", "aquella", "dichos", "dichas", "mencionados",
    "mencionadas", "indicados", "indicadas", "referidos", "referidas", "antelacion",
    "antelación", "sus", "mis",
}

_PALABRAS_FUNCIONALES = {
    # FRASE DE CONSULTA: el cliente anuncia la lista antes de enumerarla. No es un
    # medicamento y no puede encabezar el título ni la búsqueda (el cliente vería
    # "⚠️ No disponibles: COTIZAR SIGUIENTES MEDICAMENTOS"). MEDIDO: 0 productos reales
    # en los 7 providers activos; los aciertos de 'cotizar' (CORTAUÑA) y 'presupuesto'
    # (PRESTOBARBA) eran falsos positivos del matcher difuso.
    "cotizar", "cotizame", "cotizacion", "cotizaciones", "cotizarlos",
    "siguiente", "siguientes", "presupuesto",
    # Vocativo, intención de compra y referencia a la lista: NO son el nombre de nada.
    *VOCATIVOS, *INTENCION_COMPRA, *REFERENCIA_LISTA,
    *POR_SEPARADO,
    *COMPRENSION,
    *FELICITACION,
    *SALUDO_TIEMPO,
    *BENDICIONES,
    *DEMOSTRATIVOS,
    # 'resumen' no es un medicamento (ver el gemelo en turn.py::_FILLER).
    "resumen", "resumenes", "resúmenes",
    # 'costo'/'porf' tampoco: 'porf' casa con el "POR" de AMPICILINA "POR 1 AMPOLLA" y
    # ANTILAX "POR 10 TABLETAS" (ver el gemelo en turn.py::_FILLER).
    "porf", "porfa", "porfavor", "porfis", "favorcito",
    "costo", "costos", "costar", "costaria", "costarían", "costaban",
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
    # RÓTULOS DEL CAMPO: el OCR devuelve la etiqueta pegada al contenido
    # ('nombre del medicamento fexofenadina clorhidrato calox'). Sin 'nombre' aquí, el
    # término que llega al catálogo lleva la etiqueta y el filtro de laboratorio no
    # reconoce la marca. Caso real (provider 32, 2026-10-27).
    "nombre", "nombres", "rotulo", "etiqueta", "descripcion", "presentacion",
    # ACCESORIOS Y CONSUMIBLES DE UN EQUIPO, NO MEDICAMENTOS. Caso real (provider 32,
    # 2026-10-27): "¿el Glucómetro HLM-78 viene con las plaquitas, el lápiz y la pulla o
    # solo el aparato?" → el troceador sacó ['lapiz', 'pulla aparato'] y el cliente recibió
    # 20 LÁPICES de cejas y labiales, más "no disponibles: PULLA APARATO". Ninguna de estas
    # palabras identifica un fármaco por sí sola.
    # OJO: 'tiras' y 'lancetas' NO van aquí — se venden sueltas y son productos reales.
    "lapiz", "lapices", "lapicero", "lapiceros", "pulla", "pullas",
    # 'tratamiento' no es un producto: el catálogo está lleno de TRATAMIENTO CAPILAR.
    "tratamiento", "tratamientos", "tratamientos capilares",
    # CANTIDADES: 'las tres', 'los dos', 'quiero tres'. NO son medicamentos y el matcher
    # difuso los apoya en cualquier cosa (caso real: 'tres' → LITTLE TREES → 15
    # ambientadores cotizados como medicamentos, provider 32).
    "uno", "una", "unos", "unas", "dos", "tres", "cuatro", "cinco", "seis", "siete",
    "ocho", "nueve", "diez", "once", "doce", "quince", "veinte", "ambos", "ambas",
    "par", "pares", "trio", "docena", "mitad", "todas", "todos",
    # "TIENEN A LA VENTA …" es la forma más común de preguntar si vendes algo: 'venta'
    # no forma parte del nombre y descoloca al matcher (provider 32, 2026-10-29:
    # 'venta aceite oregano' devolvía 5 productos de otra cosa).
    "venta", "ventas", "vende", "vendes", "vendia", "vendían",
    # ADVERBIOS/CUANTIFICADORES del habla: 'gracias, muy amable' dejaba el término 'muy'
    # y el matcher difuso lo apoyaba en cualquier cosa.
    # EL EQUIPO / EL PERSONAL: pedir el pase no nombra un producto. 'derivalo al equipo'
    # dejaba 'equipo' como término de búsqueda.
    "equipo", "staff", "asesor", "asesora", "supervisor", "encargado", "encargada",
    "muy", "tan", "bastante", "demasiado", "algo", "mucho", "mucha", "muchos", "muchas",
    "poco", "poca", "pocos", "pocas", "tambien", "también", "solo", "solamente",
    "plaquita", "plaquitas", "tirita", "tiritas",
    "aparato", "aparatos", "equipo", "maquina", "maquinita",
    "estuche", "maletin", "cargador", "cable", "bateria", "baterias",
    "adaptador", "funda", "repuesto", "repuestos",

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
    # FORMAS VERBALES CON LAS QUE EL CLIENTE PREGUNTA, no filtradas antes.
    #
    # MEDIDO contra el catálogo real (provider 19): NINGUNA de estas 48 formas aparece como
    # palabra completa dentro de un nombre de producto, así que rechazarlas no borra ningún
    # término legítimo. (Mismo método que con las muletillas: 'sol', 'pedi', 'dias', 'mi' se
    # quedaron FUERA del gate precisamente por ser tokens reales.)
    #
    # Caso real (conv 2109, provider 19, 17:24:38):
    #     cliente: "Ya me diste el precio, gracias"     (una CORTESÍA: ya tenía el precio)
    #     LLM    : buscar_medicamento('diste')  -> 1 resultado
    #     agente : "Asumí que buscas DISCOLAYTE 👍" + COLAYTE SOBRE X 69,7 GR DISCOLAYTE
    # 'diste' pasó el gate porque NO estaba aquí, así que contaba como palabra "sustantiva";
    # el matcher difuso lo llevó a DISCOLAYTE (la marca dentro de 'COLAYTE ... DISCOLAYTE'),
    # una marca que el CLIENTE NUNCA ESCRIBIÓ. Otro tanto con 'sale' (11:11:03, 3 resultados).
    #
    # dar
    "diste", "distes", "dio", "dieron", "das", "dan", "denme", "darme", "darte",
    # salir / costar / valer
    "sale", "salen", "salio", "salió", "costaba", "cuesta", "cuestan",
    "valia", "valía", "valen", "costaria", "costaría",
    # tener / haber / poder
    "tenia", "tenía", "tuviera", "habia", "había", "podría",
    # decir / querer / necesitar
    "dice", "dijo", "dame", "queria", "quería", "necesitaba",
    # cortesía
    "porfa", "cuánto",
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


# Tokens de DOSIS y ENVASE: no aportan nombre de fármaco, pero tampoco convierten un
# medicamento en una frase. Se usan para no descartar términos largos LEGÍTIMOS como
# 'glimepiride 4 mg 30 tab' (ver _termino_es_medicamento_plausible).
_TOKENS_DE_DOSIS = {
    "mg", "ml", "mcg", "g", "gr", "gramos", "cc", "ui", "kg", "meq",
    "tab", "tabs", "tableta", "tabletas", "comp", "comprimido", "comprimidos",
    "cap", "caps", "capsula", "capsulas", "jbe", "jarabe", "susp", "sol", "sob",
    "unid", "unidad", "unidades", "amp", "ampolla", "fco", "cja",
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
    # Una VÍA DE ADMINISTRACIÓN no es un medicamento: 'intraarticular' describe cómo se usa
    # lo ya nombrado. Sin esto, "Es intraarticular" se cotizaba como producto (KENACORT).
    #
    # Una SAL tampoco: 'clorhidrato' lo comparten decenas de fármacos (betahistina,
    # metformina, bromexina, loperamida…). Cotizarla devuelve un catálogo al azar.
    if all(
        w in _VIAS_ADMINISTRACION
        or w in _CATEGORIAS_TERAPEUTICAS
        or w in _SALES_FARMACEUTICAS
        or w in _ENFERMEDADES
        or w in _ATRIBUTOS_NO_PRODUCTO
        or w in _PALABRAS_FUNCIONALES
        for w in palabras
    ):
        return False
    # Más de 4 palabras → probablemente una frase, no un medicamento.
    #
    # EXCEPCIÓN: cuando las palabras QUE SOBRAN son dosis/envase, el término es un
    # medicamento perfectamente normal con concentración y tamaño de caja. Medido:
    #     'glimepiride 4 mg 30 tab'   (5 palabras) -> se DESCARTAba
    #     'acido folico 10 mg 30 tab' (6 palabras) -> se DESCARTAba
    #     'amlodipina 5 mg 30 tab'    (5 palabras) -> se DESCARTAba
    # Caso real (conv 2109, provider 19): el cliente pidió 8 medicamentos
    #     "Glimepiride de. 4 MG x 30 Tab, candesatan 16 x 30 Tab, ..."
    # y GLIMEPIRIDE se perdió aquí — el trozo se descartó entero y el cliente nunca vio
    # ese medicamento.
    if len(palabras) > 4:
        # Las que no son número ni dosis/envase: si son pocas, sigue siendo un fármaco.
        # La DOSIS PEGADA a la unidad ('25mg', '5ml') cuenta como dosis, no como palabra:
        # sin esto, 'acido hialuronico 25mg 2 5ml' se descartaba como «no parece
        # medicamento» y el agente no buscaba nada. Caso real (conv 3132, provider 05):
        # el cliente escribió justo así y el turno se quedó sin consultar el catálogo.
        sobrantes = [
            w for w in palabras
            if not w.isdigit()
            and w not in _TOKENS_DE_DOSIS
            and not re.fullmatch(r"\d+(?:[.,]\d+)?(?:mg|ml|mcg|gr|g|ui|cc|kg|tab|caps?)", w)
        ]
        if len(sobrantes) > 3:
            return False
        # Si son pocas, NO se acepta sin más: sigue el resto del filtro (sustantivas).
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


# FRASES DE INDICACIÓN: describen PARA QUÉ es el medicamento, no CUÁL es.
#
# Caso real (conv 7, provider 19): "Broxol tos seca"
#     term='broxol'          -> 11 productos   (BROXOL EXISTE: BROXOL TOS JAB ADUL/PED…)
#     term='broxol tos seca' ->  3 productos   (toalla "MALLA SECA", protector "ACTIVA SEC",
#                                               jabón "PIEL SECA")
# La ENFERMEDAD degradaba la búsqueda: 'tos' casa con la abreviatura T/S de otros productos y
# 'seca' con toallas/jabones. El mismo patrón ya había pasado con 'beusan tos seca' (conv 2560).
#
# REGLA: se quita la FRASE, nunca la palabra suelta. Medido contra el catálogo real:
#     'tos'  solo -> 3 productos REALES (BROXOL TOS JAB ADUL, BROXOL TOS JAB PED, BEUSAN … TOS)
#     'seca' solo -> 2 productos REALES (TOALLA … MALLA SECA, JABON BIOS PIEL SECA)
#     'tos seca'  como frase -> 0 productos  -> quitarla es seguro
# Quitar 'tos' o 'seca' sueltas borraría productos legítimos.
#
# Las 32 frases de abajo se midieron una a una contra el catálogo: NINGUNA aparece como frase
# dentro de un nombre de producto. La única descartada fue 'piel seca' — ESA SÍ existe
# ('JABON BIOS PIEL SECA X 90 GR BARRA'), así que no se toca.
# (Sin tildes: se aplican sobre el texto ya normalizado.)
_FRASES_INDICACION = (
    "tos seca", "tos productiva", "tos con flema", "tos con moco", "tos con catarro",
    "dolor de cabeza", "dolor de garganta", "dolor de estomago", "dolor de barriga",
    "dolor de espalda", "dolor muscular", "dolor de oidos", "dolor de muela",
    "presion alta", "presion arterial", "azucar alta", "colesterol alto",
    "falta de aire", "congestion nasal", "nariz tapada", "malestar estomacal",
    "ardor de estomago", "falta de apetito", "garganta irritada",
    "mucha sed", "orina frecuente", "resfriado comun", "gripe", "fiebre",
    "infeccion urinaria", "hongos en los pies",
)

# ENFERMEDADES / INDICACIONES de UNA palabra — NO SON EL MEDICAMENTO.
# La enfermedad dice PARA QUÉ SIRVE. Sin esta lista, "tratamiento para toxoplasmosis" se
# buscaba como producto y el catálogo devolvía 20 tratamientos capilares (DOVE, ELVIVE).
# Caso real (provider 32, 2026-10-28 21:03).
_ENFERMEDADES = frozenset({
    "toxoplasmosis", "toxo", "gripe", "gripa", "resfriado", "catarro", "fiebre", "tos",
    "dolor", "dolores", "cefalea", "migrana", "migraña", "jaqueca", "infeccion",
    "infecciones", "hongos", "diarrea", "estrenimiento", "estreñimiento", "alergia",
    "alergias", "rinitis", "hipertension", "hipertensión", "tension", "tensión",
    "diabetes", "colesterol", "trigliceridos", "triglicéridos", "anemia", "vomito",
    "vómito", "nauseas", "náuseas", "mareo", "mareos", "ansiedad", "insomnio", "acne",
    "acné", "caspa", "psoriasis", "gastritis", "colon", "artritis", "artrosis",
    "embarazo", "lactancia", "covid", "neumonia", "neumonía", "bronquitis", "asma",
    "varicela", "sarampion", "sarampión", "herpes", "candidiasis", "parasitosis",
    "lombrices", "piojos", "hemorroides", "ulcera", "úlcera", "reflujo", "depresion",
    "depresión", "epilepsia", "tiroides", "hipotiroidismo", "hipertiroidismo", "prostata",
    "próstata", "menopausia", "menstruacion", "menstruación", "resaca", "quemadura",
    "quemaduras", "herida", "heridas", "cicatriz", "cicatrices", "deshidratacion",
    "deshidratación", "desmayo", "convulsion", "convulsión", "tumor", "cancer", "cáncer",
    # Adjetivos que acompañan a la indicación y quedan colgando al quitarla
    # ('infeccion urinaria', 'infeccion respiratoria', 'dolor lumbar').
    # NO se incluyen 'dental', 'vaginal', 'nasal', 'muscular' ni 'articular': son también
    # palabras de PRODUCTO ('crema dental', 'óvulos vaginales', 'spray nasal') y quitarlas
    # rompía 'crema dental sensodyne' → 'crema sensodyne' (valida_cortesia).
    "urinaria", "urinarias", "respiratoria", "respiratorias", "lumbar", "menstrual",
    "estomacal", "intestinal",
})

# Ordenadas de MÁS LARGA a más corta para que la frase específica gane a una más general.
_RE_FRASE_INDICACION = re.compile(
    r"\b(?:"
    + "|".join(re.escape(p).replace(r"\ ", r"\s+")
               for p in sorted(_FRASES_INDICACION, key=len, reverse=True))
    + r")\b"
)


def _sin_frase_de_indicacion(t_sin: str) -> str:
    """Quita las frases de indicación ('tos seca', 'dolor de cabeza') del término.

    Espera el texto YA normalizado (minúsculas y sin tildes), que es como se tokeniza.
    """
    return _RE_FRASE_INDICACION.sub(" ", t_sin or "")


# CATEGORÍA TERAPÉUTICA / INDICACIÓN IMPRESA EN LA CAJA — NO ES EL MEDICAMENTO.
#
# Caso real (2026-10-19): el cliente mandó la FOTO de una caja de
#     Fexofenadina Clorhidrato CALOX · Medicamento Genérico
#     Antialérgico · Antihistamínico · Via Oral · 10 Tabletas Recubiertas · 120 mg
# y el agente cotizó CUATRO "medicamentos": ANTIHISTAMINICO, FEXOFENADINA CLORHIDRATO,
# CALOX (21 productos de ese laboratorio) y ANTISEPTICO. Solo el segundo lo es.
#
# La caja trae, además del fármaco: la marca, el laboratorio, la CATEGORÍA («Antialérgico
# Antihistamínico») y la forma. Lo único que se busca es el fármaco.
# Es la misma regla que ya vale para la indicación en el texto («broxol tos seca» → broxol):
# PARA QUÉ SIRVE no es CUÁL ES.
_CATEGORIAS_TERAPEUTICAS = {
    "antihistaminico", "antihistaminica", "antihistaminicos", "antialergico",
    "antialergica", "antialergicos", "antiseptico", "antiseptica", "antisepticos",
    "analgesico", "analgesica", "analgesicos", "antibiotico", "antibiotica",
    "antibioticos", "antiinflamatorio", "antiinflamatoria", "antiinflamatorios",
    "antiflamatorio", "antipiretico", "antipiretica", "antiacido", "antiacida",
    "antifungico", "antifungica", "antimicotico", "antimicotica", "antiviral",
    "antivirales", "antitusivo", "antitusigena", "antitusigeno", "antiespasmodico",
    "antiespasmotico", "expectorante", "mucolitico", "descongestionante", "laxante",
    "antidiarreico", "antihemorroidal", "antihipertensivo", "antihipertensiva",
    "antidiabetico", "antidiabetica", "hipoglucemiante", "diuretico", "diuretica",
    "vasodilatador", "vasoconstrictor", "antiarritmico", "anticoagulante",
    "antiagregante", "antiagregantes", "corticoide", "corticosteroide", "esteroide",
    "broncodilatador", "antiemetico", "antiulceroso", "antiacne", "antimicotico",
    "multivitaminico", "vitaminico", "suplemento", "suplementos", "generico",
    "generica", "genericos", "genericas",
    # ANTICONCEPTIVOS: categoría completa. Caso real (provider 32, 2026-10-28): el cliente
    # escribió "anticonceptivas" y el agente lo buscó en el catálogo; el matcher difuso lo
    # apoyó en cualquier cosa y la respuesta acabó en "⚠️ No disponibles: ANTICONCEPTIVAS".
    "anticonceptivo", "anticonceptiva", "anticonceptivos", "anticonceptivas",
    "anticoncepcion", "anticoncepción",
}


# ATRIBUTOS Y SEGMENTOS — NO SON UN PRODUCTO.
#
# Describen PARA QUÉ o PARA QUIÉN es lo que el cliente busca, pero no lo nombran. En el
# catálogo están en cientos de títulos (cintas, ganchos, gorros, pañales, cánulas), así que
# buscar por ellos devuelve basura con total seguridad.
#
# Caso real (provider 32, 2026-10-29 09:46):
#   "Una crema para cabello rizado" → 20 artículos de peluquería (cepillos, rociadores)
#   "Para adultos"                  → pañales, cánulas nasales, preservativos, relojes
_ATRIBUTOS_NO_PRODUCTO = frozenset({
    # partes del cuerpo / del cabello
    "cabello", "pelo", "piel", "cara", "rostro", "manos", "pies", "unas", "uñas", "dientes",
    "boca", "ojos", "labios", "barba", "cuerpo", "cejas", "pestanas", "pestañas", "axilas",
    "cuero", "cabelludo", "encías", "encias", "nuca", "espalda",
    # segmento / a quién va dirigido
    "adulto", "adulta", "adultos", "adultas", "nino", "nina", "ninos", "niñas", "niño",
    "niña", "niños", "bebe", "bebes", "bebé", "bebés", "infantil", "pediatrico",
    "pediátrico", "pediatrica", "pediátrica", "mujer", "mujeres", "hombre", "hombres",
    "senora", "señora", "dama", "caballero", "unisex", "familiar", "junior", "senior",
    # descriptores del tipo de cabello/piel/producto
    "rizado", "rizada", "liso", "lisa", "ondulado", "ondulada", "graso", "grasa", "seco",
    "seca", "mixto", "mixta", "sensible", "sensibles", "delicado", "delicada", "natural",
    "normal", "extra", "fuerte", "suave", "humectante", "hidratante", "nutritivo",
    "anticaspa", "antifrizz", "frizz", "brillo", "suavidad", "crecimiento",
})

# VÍA DE ADMINISTRACIÓN — NO ES UN MEDICAMENTO.
#
# Caso real (conv 3132, provider 05, 2026-10-19 13:23): el cliente venía preguntando por
# ácido hialurónico y escribió "Es intraarticular". El agente le cotizó
#     INTRAARTICULAR
#     💊 1. KENACORT -A X 10 MG AMPOLLAS INTRAARTICULAR   $40,56
# o sea: trató la FORMA DE ADMINISTRAR como si fuera el producto que pedía — un corticoide
# en vez del ácido hialurónico. La vía dice CÓMO se usa lo ya nombrado; nunca es la petición.
# Se filtra del TÉRMINO (el nombre real del producto sigue intacto en el catálogo).
# Su gemelo en turn.py::_FILLER NO basta: hay dos caminos de limpieza (ver el historial de
# 'd3/k2' y de `dar`), así que la regla vive aquí y la usan los dos.
_VIAS_ADMINISTRACION = {
    "intraarticular", "intramuscular", "endovenoso", "endovenosa", "intravenoso",
    "intravenosa", "subcutaneo", "subcutanea", "subcutáneo", "subcutánea",
    "parenteral", "intradermico", "intradérmico", "intratecal",
    "oftalmico", "oftálmico", "oftalmica", "oftálmica", "otico", "ótico", "otica",
    "topico", "tópico", "topica", "tópica", "transdermico", "transdérmico",
    "inhalado", "inhalada", "inhalatorio", "nebulizacion", "nebulización",
    "rectal", "vaginal", "nasal", "sublingual", "intranasal",
    "intraoperatorio", "intralesional", "periarticular", "via", "vía",
    "oral", "orales", "parenterales",
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
    # La FRASE de indicación se quita ANTES de tokenizar: describe PARA QUÉ sirve el
    # medicamento, no CUÁL es. 'broxol tos seca' → 'broxol' (11 productos en vez de 3 basura).
    t_sin = _sin_frase_de_indicacion(t_sin)
    # 'de la opción 9' / 'la número 3' son una REFERENCIA A LA LISTA, no una dosis: si el
    # '9' sobrevive, el catálogo recibe 'macrodantina vitamina c 9' y no encuentra nada.
    t_sin = re.sub(r"\b(?:opci[oó]n|opciones|n[uú]mero)\s+\d+", " ", t_sin)
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
        # Los CÓDIGOS del nombre ('d3', 'k2', 'b12') tienen 2-3 caracteres pero
        # identifican el producto: sin ellos, 'vitamina d3/k2' se reducía a 'vitamina'
        # y el cliente recibía multivitaminas, B12 y hasta jabón con provitamina.
        # Cualquier token CON dígito cuenta: el relleno nunca lleva números.
        or (w not in _PALABRAS_FUNCIONALES
            and w not in _VIAS_ADMINISTRACION
            and w not in _CATEGORIAS_TERAPEUTICAS
            and w not in _ENFERMEDADES
            and (len(w) >= 3 or w.isdigit() or any(c.isdigit() for c in w)
                 # LA LETRA DE LA VITAMINA: 'vitamina c' no puede quedarse en
                 # 'vitamina' (el cliente recibiría E, K, B12 y colágeno).
                 or _es_letra_de_vitamina(w, t_sin)))
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


def _dosis_sin_unidad(texto: str) -> str:
    """Dosis del texto ASUMIENDO mg, cuando el cliente la escribe sin unidad.

    Caso real (provider 05): el cliente pidió 3 medicamentos cada uno con su dosis, sin 'mg':
        "Dame precio  de hidrocoticida de 12.5
         Cardesartan de 16
         Cardevidol 6.25"
    El pipeline descartaba los números (van sueltos, sin unidad) y el catálogo devolvía TODAS
    las concentraciones mezcladas: hidroclorotiazida 12.5 y 25; candesartán 8 y 16; carvedilol
    6.25, 12.5 y 25. El cliente había dicho la dosis de cada uno.

    DOS GUARDAS, ambas medidas:
      1. Un DECIMAL es siempre una dosis: un envase no se escribe "12.5" ni "6.25".
      2. Un ENTERO se acepta solo si NO es una cantidad de compra/envase. 'el de 30 tabletas'
         o '2 cajas' describen el ENVASE, no la concentración — asumir mg ahí mostraría un
         producto equivocado.

    Devuelve '' si no hay una dosis reconocible. NUNCA la unidad: el llamador decide (aquí se
    asume mg, que es lo que pide el caso).
    """
    if not texto:
        return ""
    t = texto.strip().lower()
    # Guarda 2: cantidad de compra o envase explícito → no es dosis.
    if _RE_CANTIDAD_COMPRA_DOSIS.search(t) or _RE_X_ENVASE_DOSIS.search(t):
        return ""
    # Guarda 1: decimal → dosis segura.
    m = re.search(r"\b(\d+[.,]\d+)\b", t)
    if m:
        return m.group(1).replace(",", ".")
    # Entero: solo precedido de 'de'/'del' ('de 16'), o suelto tras el fármaco.
    m = re.search(r"\b(?:de|del)\s+(\d{1,4})\b", t)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{1,4})\s*(?:mg|mcg|g|ml)?\s*$", t)
    if m and m.group(1):
        return m.group(1)
    return ""


# Cantidad de COMPRA o envase: nunca es una dosis. Se usa en `_dosis_sin_unidad`.
_RE_CANTIDAD_COMPRA_DOSIS = re.compile(
    r"\b\d+\s*(?:cajas?|unidades?|frascos?|paquetes?|blisters?|tabs?|tabletas?|"
    r"caps?|cápsulas?|capsulas?|comprimidos?|sobres?|ampollas?)\b",
    re.IGNORECASE,
)
# El "x N" del título ('x 30 tab') es envase, no dosis.
_RE_X_ENVASE_DOSIS = re.compile(r"\bx\s*\d{1,3}\b", re.IGNORECASE)


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


# Umbral de similitud para aceptar un token del catálogo como la grafía correcta.
# CALIBRADO con datos reales (provider 27): los typos verdaderos puntúan
# 'hidrocoticida'→hidroclorotiazida 0.800, 'cardesartan'→candesartan 0.909,
# 'cardevidol'→carvedilol 0.700, 'atamle'→atamel 0.833, 'omeprasol'→omeprazol 0.889.
# Los NO-typos quedan por debajo: 'magnesio plus life' 0.571,
# 'medicamento controlado manejan' 0.556, 'dovilin' 0.533, 'audio lead' 0.444.
# 0.65 separa ambos grupos con margen (el typo más flojo es 0.700).
_UMBRAL_TYPO_CATALOGO = 0.65

# Para RE-BUSCAR por el núcleo solo (ver `buscar_medicamento`) se exige más que para
# corregir un typo: que la corrección sea una VARIANTE DE ESCRITURA del mismo fármaco, no
# una palabra parecida de otro producto. MEDIDO contra los catálogos reales:
#     hialuronico -> hialuronato   0.818  (mismo principio activo: TOPTEAR)   -> DEBE pasar
#     sensodyne   -> sensitive     0.556  (otra marca: Colgate Sensitive)     -> NO debe pasar
#     acetalicio  -> acetilsalicilico 0.769 (typo real; no necesita esta rama)
# El umbral de typo (0.65) NO separa los dos primeros casos; 0.75 sí, con hueco 0.194.
# Con 0.65 la rama del núcleo cambiaba SENSODYNE por una pasta de otra marca y tapaba el
# aviso de que no está; con 0.85 se perdía el ácido hialurónico.
_UMBRAL_NUCLEO_MISMO_FARMACO = 0.75

# Prefijos con los que se sondea el catálogo, de más largo a más corto. Hace falta bajar
# hasta 3 letras: en 'cardevidol' el error está en la posición 4, así que 'card' NO trae
# CARVEDILOL — solo un prefijo más corto ('car') lo alcanza.
_PREFIJOS_SONDEO = (8, 6, 5, 4, 3)


def _tokens_de_catalogo(products: list[dict[str, Any]]) -> set[str]:
    """Tokens 'con cuerpo' (5+ letras) de los nombres del catálogo."""
    out: set[str] = set()
    for p in products:
        nom = str(p.get("producto") or p.get("nombre") or p.get("title") or "").lower()
        for w in re.split(r"[\s\-+/()]+", nom):
            w = w.strip(".,;:")
            if len(w) >= 5 and w.isalpha():
                out.add(w)
    return out


def _mejor_token_catalogo(nucleo: str, tokens: set[str]) -> tuple[str | None, float, float]:
    """El token del catálogo más parecido al núcleo escrito, con su margen.

    Devuelve (token, ratio, margen_sobre_el_segundo). El margen es lo que distingue
    'cardevidol'→carvedilol (margen 0.033, gana igual) de 'colmo'→colonia (margen 0.000,
    empate: no se debe elegir). Un empate significa que el catálogo tiene varias palabras
    igual de parecidas y elegir una sería adivinar.
    """
    if not nucleo or not tokens:
        return None, 0.0, 0.0
    scored = sorted(
        ((difflib.SequenceMatcher(None, nucleo, t).ratio(), t) for t in tokens),
        reverse=True,
    )
    if not scored:
        return None, 0.0, 0.0
    mejor_ratio, mejor = scored[0]
    segundo = scored[1][0] if len(scored) > 1 else 0.0
    return mejor, mejor_ratio, mejor_ratio - segundo


def _correccion_por_largo_plausible(escrito: str, candidato: str) -> bool:
    """¿La corrección tiene la FORMA de un typo y no de otra palabra?

    Un typo quita, cambia o traspone letras: la palabra queda igual de larga o MÁS
    larga (el cliente se comió una letra). No convierte una palabra en otra mucho más
    corta. Cuando lo hace, no es un typo de lo que escribió: es OTRA palabra.

    Caso real (conv 2109, provider 19):
        'panadol'  → 'panal'   (7 → 5 letras) → PANAL BABYGANICS, PANAL MIMLOT...
    El cliente pedía Panadol (acetaminofén) y recibía pañales. El ratio NO lo distingue:
    'panadol'→'panal' puntúa 0.833, EXACTAMENTE igual que 'atamle'→'atamel' (0.833) y
    por debajo de 'omeprasol'→'omeprazol' (0.889), que sí son typos legítimos. Subir el
    umbral rompería los buenos sin arreglar el malo.

    El LARGO SÍ los separa. MEDIDO con 90 typos sintéticos (borrado, transposición y
    sustitución) sobre 202 tokens reales del catálogo del provider 19: con y sin este
    guard se recuperan los MISMOS 63 (70,0%), y bloquea 'panadol'→'panal'. No cuesta
    ninguna recuperación legítima.

    Se permite UN carácter de menos: cubre al cliente que escribe una letra de más
    ('omeprazoll' → 'omeprazol') sin abrir la puerta a encoger la palabra.
    """
    return len(candidato) >= len(escrito) - 1


# ── FORMA FARMACÉUTICA: suspensión, tabletas, gotas, crema… ───────────────────────
# El cliente pide "apiret en suspensión" y el catálogo devuelve SEIS APIRET: dos tabletas,
# dos granulados en sobre, una solución oral y un frasco sin forma escrita. La forma es tan
# parte del pedido como la dosis, y hasta ahora solo se usaba para traducir unidades.
#
# Caso real (provider 05, 2026-10-27): "Me indica precio apiret en suspensión" → 6 opciones
# mezcladas, la que pidió en el puesto 6.
_FORMA_CANONICA: dict[str, str] = {
    # líquidos orales
    "suspension": "suspensión", "suspensión": "suspensión", "susp": "suspensión",
    "jarabe": "jarabe", "jarabes": "jarabe", "jbe": "jarabe", "jarb": "jarabe",
    "jarab": "jarabe",
    "solucion": "solución oral", "solución": "solución oral",
    "solucion oral": "solución oral", "solución oral": "solución oral",
    "sol oral": "solución oral",
    "gotas": "gotas", "gotitas": "gotas", "gtas": "gotas", "gts": "gotas",
    "elixir": "elixir", "emulsion": "emulsión", "emulsión": "emulsión",
    # sólidos orales
    "tableta": "tabletas", "tabletas": "tabletas", "tab": "tabletas", "tabs": "tabletas",
    "comprimido": "tabletas", "comprimidos": "tabletas", "comp": "tabletas",
    "gragea": "tabletas", "grageas": "tabletas",
    "capsula": "cápsulas", "cápsula": "cápsulas", "capsulas": "cápsulas",
    "cápsulas": "cápsulas", "cap": "cápsulas", "caps": "cápsulas",
    # polvos
    "polvo": "polvo", "granulado": "polvo", "granulados": "polvo",
    "granulada": "polvo", "sobre": "polvo", "sobres": "polvo", "sachet": "polvo",
    # tópicos
    "crema": "crema", "cremas": "crema", "pomada": "crema",
    "unguento": "ungüento", "ungüento": "ungüento",
    "gel": "gel", "locion": "loción", "loción": "loción",
    # otros
    "ampolla": "inyectable", "ampollas": "inyectable", "inyectable": "inyectable",
    "vial": "inyectable",
    "ovulo": "óvulos", "óvulo": "óvulos", "ovulos": "óvulos", "óvulos": "óvulos",
    # Abreviaturas que el catálogo pega al número de envase: 'METRONIDAZOL 500MG X 10OVU'
    # son ÓVULOS (reportado en provider 32, 2026-10-28: el agente negó los óvulos que sí
    # existían). Igual con SUP (supositorios), AMP (ampolla), SOB (sobre), CRM (crema).
    "ovu": "óvulos", "ovul": "óvulos", "ovus": "óvulos",
    # "X 10VU" es una OVU truncada (METRONIDAZOL 500MG X 10VU COLMED, provider 32).
    "vu": "óvulos",
    "supositorio": "supositorios", "supositorios": "supositorios",
    "sup": "supositorios", "supo": "supositorios",
    "amp": "inyectable", "amps": "inyectable",
    "sob": "polvo", "sobs": "polvo",
    "crm": "crema", "pom": "crema", "loc": "loción",
    "oft": "gotas oftálmicas",
    "spray": "spray", "aerosol": "spray",
    "colirio": "gotas oftálmicas", "colirios": "gotas oftálmicas",
    # SOLUCIÓN BUCAL. Caso real (provider 32, 2026-10-29): el cliente pidió "solución bucal",
    # el pedido se resolvía como "solución oral" y el aviso decía "No tengo SOLUNOVAL BUCAL en
    # solución oral" MIENTRAS listaba un SOL BUCAL. La forma bucal no estaba.
    # 'SOL OFT' es como el catálogo escribe las gotas oftálmicas ('QUINOMAX 0,50% X 5ML
    # SOL OFT OFTALMI'). Sin esto la forma pedida ('gotas oftálmicas', que la receta imprime
    # en el paréntesis) no casaba y el agente avisaba "No tengo QUINOMAX en gotas oftálmicas"
    # sobre un producto que ES solución oftálmica (provider 32, 2026-10-30).
    "sol oft": "gotas oftálmicas", "sol oftalmica": "gotas oftálmicas",
    "solucion oftalmica": "gotas oftálmicas", "solucion oftalmologica": "gotas oftálmicas",
    "oftalmica": "gotas oftálmicas", "oftalmico": "gotas oftálmicas",
    "gotas oftalmicas": "gotas oftálmicas", "gotas oftalmológicas": "gotas oftálmicas",
    "colirio": "gotas oftálmicas", "colirios": "gotas oftálmicas",
    "bucal": "solución bucal", "bucales": "solución bucal",
    "solucion bucal": "solución bucal", "solución bucal": "solución bucal",
    "sol bucal": "solución bucal",
}

# La FAMILIA es lo que se compara: quien pide 'suspensión' también acepta el jarabe y la
# solución oral (es el mismo líquido pediátrico), pero no una tableta.
_FAMILIA_DE_FORMA: dict[str, str] = {
    "suspensión": "liquida", "jarabe": "liquida", "solución oral": "liquida",
    "gotas": "liquida", "elixir": "liquida", "emulsión": "liquida", "líquido": "liquida",
    # TABLETAS y CÁPSULAS son familias DISTINTAS: quien pide cápsulas no quiere que le
    # ofrezcan la tableta del mismo fármaco (medido con 'omeprazol en capsulas' → la tableta
    # salía la primera). Si no hay cápsulas, el fallback ofrece la lista completa CON aviso.
    "tabletas": "tabletas", "cápsulas": "capsulas",
    "polvo": "polvo",
    "crema": "topica", "ungüento": "topica", "gel": "topica", "loción": "topica",
    "inyectable": "inyectable",
    "solución bucal": "liquida",
    "óvulos": "vaginal", "supositorios": "vaginal",
    "spray": "spray",
    "gotas oftálmicas": "oftalmica",
}


def _normalizar_forma(texto: str) -> str:
    return _normalizar_tildes((texto or "").lower())


def _sin_crema_dental(texto: str) -> str:
    """Quita 'crema dental' antes de buscar una FORMA.

    'CREMA DENTAL' es el NOMBRE del producto (pasta de dientes), no la forma
    farmacéutica: tratarla como crema tópica hacía que "crema dental sensodyne" se acotara a
    cremas, avisara "No tengo DENTAL SENSODYNE en crema" y dejara el título en 'DENTAL'.
    Caso real (provider 19, 2026-10-27).
    """
    t = _normalizar_forma(texto)
    return re.sub(r"\bcremas?\s+dental(es)?\b|\bcremas?\s+dentifric[ao]s?\b", " ", t)


def _forma_pedida(texto: str) -> tuple[str | None, str | None]:
    """(familia, etiqueta) de la forma que pide el cliente; (None, None) si no pide ninguna.

    'apiret en suspensión' → ('liquida', 'suspensión'). Se busca la palabra MÁS LARGA
    primero para que 'solución oral' gane a 'sol' y 'gotas oftálmicas' a 'gotas'.
    """
    t = _sin_crema_dental(texto)
    if not t:
        return None, None
    if re.search(r"\b(gotas|gtas|gts|colirio|colirios)\b", t) and re.search(
        r"\b(oftalmic\w*|ocular\w*|ojos?)\b", t
    ):
        return "oftalmica", "gotas oftálmicas"
    for palabra in sorted(_FORMA_CANONICA, key=len, reverse=True):
        if re.search(rf"\b{re.escape(palabra)}\b", t):
            etiqueta = _FORMA_CANONICA[palabra]
            return _FAMILIA_DE_FORMA.get(etiqueta), etiqueta
    return None, None


def _etiqueta_de_forma(titulo: str) -> str | None:
    """Qué forma es este producto, leída de su TÍTULO. None si el título no lo dice."""
    t = _normalizar_forma(titulo)
    if not t:
        return None
    if re.search(r"\bcremas?\s+dental(es)?\b", t) and not re.search(
        r"\b(tab|tabs|tableta|tabletas|caps|capsula|capsulas|sobre|granulado)\b", t
    ):
        # Pasta de dientes: producto, no forma farmacéutica. Sin familia a propósito.
        return "crema dental"
    if re.search(r"\b(gotas|gtas|gts)\b", t) and re.search(
        r"\b(oftalmic\w*|ocular\w*|colirio)\b", t
    ):
        return "gotas oftálmicas"
    # 'SOL OFT' / 'SOLUCION OFTALMICA': la forma oftálmica que el catálogo abrevia. Se exige
    # la palabra de vehículo (sol/gotas/colirio) delante para NO leer el laboratorio
    # 'OFTALMI' de las tabletas ('DICLOFENAC POTASICO 50MG X 10TAB OFTALMI').
    if re.search(r"(?:^|[^a-z])(?:sol|solucion|gotas|gtas|colirio)\s*\.?\s*oft", t):
        return "gotas oftálmicas"
    if re.search(r"\bcolirios?\b", t):
        return "gotas oftálmicas"
    pruebas = (
        (r"\bsusp", "suspensión"),
        (r"\b(jarabe|jarabes|jbe|jarb|jarab)\b", "jarabe"),
        (r"\b(solucion|sol\s*oral|solucion\s*oral)\b", "solución oral"),
        (r"\b(gotas|gtas|gts)\b", "gotas"),
        # SIN `\b` a la izquierda: el catálogo pega la forma al número
        # ('APIRET 500MGX10TAB', 'AMOXICILINA 500MGX20CAPS') y ahí no hay frontera de
        # palabra. Se exige que no venga precedido de una LETRA para no leer 'ESTABLE'.
        (r"(?:^|[^a-z])(tab|tabs|tableta|tabletas|comp|comprimido|comprimidos|gragea|"
         r"grageas)\b", "tabletas"),
        (r"(?:^|[^a-z])(cap|caps|capsula|capsulas)\b", "cápsulas"),
        (r"\b(sobre|sobres|granulado|granulados|granulada|polvo|sachet)\b", "polvo"),
        (r"\b(crema|cremas)\b", "crema"),
        (r"\b(unguento|pomada)\b", "ungüento"),
        (r"\bgel\b", "gel"),
        # 'SOL BUCAL' es solución bucal, no el cajón genérico 'líquido'.
        (r"(?:^|[^a-z])(?:sol\s*)?bucal(?:es)?\b", "solución bucal"),
        (r"\b(ampolla|ampollas|inyectable|vial)\b|(?:^|[^a-z])amp[s]?\b", "inyectable"),
        # ÓVULOS ≠ SUPOSITORIOS: antes los dos devolvían "supositorios" y la etiqueta
        # mentía. Sin `\b` a la izquierda porque la abreviatura va pegada al envase
        # ('X 10OVU', 'X 10OVULOS'); se exige que no la preceda una LETRA.
        (r"(?:^|[^a-z])(ovu|ovul|ovus|vu|ovulo|ovulos)\b", "óvulos"),
        (r"\b(supositorio|supositorios)\b|(?:^|[^a-z])sup[s]?\b", "supositorios"),
        (r"(?:^|[^a-z])sob[s]?\b|\b(sobre|sobres)\b", "polvo"),
        (r"\b(spray|aerosol)\b", "spray"),
        (r"\b(elixir)\b", "elixir"),
    )
    for patron, etiqueta in pruebas:
        if re.search(patron, t):
            return etiqueta
    # Un VOLUMEN en ml y ninguna forma sólida: es líquido aunque el título esté roto
    # ('APIRET 180ML75ML 60ML OFTALMI').
    if re.search(r"\d+\s*ml\b", t):
        return "líquido"
    return None


def _titulo_de_producto(p: dict[str, Any]) -> str:
    return str(p.get("producto") or p.get("nombre") or p.get("title") or "")


def _termino_sin_forma(termino: str) -> str:
    """El término sin la palabra de la forma ('apiret suspensión' → 'apiret').

    Se comparan también PARES de palabras, porque hay formas de dos palabras
    ('sol oral', 'gotas oftalmicas') que se perderían al ir palabra a palabra.
    """
    _CONECTORES = {
        "en", "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
        "con", "para", "por", "y", "o", "su", "mi", "que", "al",
    }
    palabras = [w for w in re.split(r"\s+", (termino or "").strip()) if w]
    fuera: list[str] = []
    i = 0
    while i < len(palabras):
        actual = _normalizar_forma(palabras[i]).strip(" -.,;:()[]")
        par = actual
        if i + 1 < len(palabras):
            par = f"{actual} {_normalizar_forma(palabras[i + 1]).strip(' -.,;:()[]')}"
        if par in _FORMA_CANONICA:
            i += 2
            continue
        if actual in _FORMA_CANONICA:
            i += 1
            continue
        fuera.append(palabras[i])
        i += 1
    # los conectores que quedaban colgando al quitar la forma ('apiret EN', 'de la')
    while fuera and _normalizar_forma(fuera[-1]).strip(" -.,;:()[]") in _CONECTORES:
        fuera.pop()
    while fuera and _normalizar_forma(fuera[0]).strip(" -.,;:()[]") in _CONECTORES:
        fuera.pop(0)
    return " ".join(fuera).strip() or (termino or "")


def _filtrar_por_forma(
    productos: list[dict[str, Any]], familia: str | None
) -> list[dict[str, Any]]:
    """Los productos de esa familia de forma. Si no hay NINGUNO, devuelve la lista entera.

    Nunca se queda sin ofrecer: negar el medicamento porque la forma no está sería peor
    que mostrar otras presentaciones — pero entonces el aviso lo dice (ver `_aviso_forma`).
    """
    if not familia or not productos:
        return list(productos or [])
    dentro = [
        p for p in productos
        if _FAMILIA_DE_FORMA.get(_etiqueta_de_forma(_titulo_de_producto(p)) or "") == familia
    ]
    return dentro or list(productos)


def _aviso_forma(
    productos: list[dict[str, Any]], termino: str, etiqueta_pedida: str | None
) -> str:
    """Aviso cuando la forma pedida no está entre los productos que se van a mostrar."""
    if not etiqueta_pedida or not productos:
        return ""
    etiquetas = {_etiqueta_de_forma(_titulo_de_producto(p)) for p in productos}
    if etiqueta_pedida in etiquetas:
        return ""
    # La FAMILIA manda: "solución oral" y "líquido" son lo mismo para el cliente, y negar
    # "en solución oral" mientras se lista un SOL BUCAL es una contradicción en dos líneas
    # seguidas (provider 32, 2026-10-29). Solo se avisa si NINGÚN producto es de la familia.
    # OJO: NO se relaja por FAMILIA. Quien pide "suspensión" y recibe "solución oral" tiene
    # que oír que la suspensión no está (APIRET), y el cajón 'líquido' —títulos sin forma
    # declarada— no prueba nada. El caso SOLUNOVAL se arregla en el vocabulario: 'bucal' es
    # una forma propia, igual en el pedido y en el título, y ahí manda la igualdad de arriba.
    droga = _termino_sin_forma(termino).upper()
    # 'líquido' es el cajón de los títulos rotos: no se anuncia como presentación.
    otras = sorted({e for e in etiquetas if e and e != "líquido" and e != etiqueta_pedida})
    if otras:
        return f"⚠️ No tengo {droga} en {etiqueta_pedida}. Lo tengo en: {', '.join(otras)}."
    return f"⚠️ No tengo {droga} en {etiqueta_pedida}."


def _aplicar_forma(
    productos: list[dict[str, Any]],
    termino: str,
    forma: tuple[str | None, str | None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """(productos acotados a la forma pedida, aviso si esa forma no está).

    `forma` permite pasar la familia ya resuelta desde el mensaje del cliente, porque el
    LLM suele buscar solo con el fármaco ('omeprazol' en vez de 'omeprazol en cápsulas').
    """
    familia, etiqueta = forma or _forma_pedida(termino)
    if not familia or not productos:
        return list(productos or []), ""
    filtrados = _filtrar_por_forma(productos, familia)
    return filtrados, _aviso_forma(filtrados, termino, etiqueta)




def _es_solo_sal(termino: str) -> bool:
    """¿El término es SOLO la sal del principio activo ('clorhidrato', 'maleato sodico')?

    Si al termino no le queda ninguna palabra que nombre un fármaco, no hay nada que buscar.
    """
    tokens = [
        w.strip(" -.,;:()[]")
        for w in _normalizar_tildes((termino or "").lower()).split()
        if w.strip(" -.,;:()[]")
    ]
    if not tokens:
        return False
    return all(w in _SALES_FARMACEUTICAS for w in tokens)


def _titulo_normalizado(valor: object) -> str:
    """Título de producto listo para comparar: sin tildes, minúsculas, sin signos en los bordes."""
    texto = _normalizar_tildes(str(valor or "").lower())
    return texto.strip(" -·.,;:()[]/*")


def _tokens_de_titulo(titulo: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9ñ]+", titulo) if t]


def _termina_con_token(titulo: str, token: str) -> bool:
    toks = _tokens_de_titulo(titulo)
    return bool(toks) and toks[-1] == token


def _empieza_con_token(titulo: str, token: str) -> bool:
    toks = _tokens_de_titulo(titulo)
    return bool(toks) and toks[0] == token


def _es_nombre_de_laboratorio(term: str, products: list[dict[str, Any]]) -> bool:
    """¿El término es el LABORATORIO de estos productos, y no el medicamento?

    La caja de un medicamento trae el fármaco y, aparte, el laboratorio. El OCR los devuelve
    como líneas separadas, y «CALOX» acabó cotizándose como si fuera un medicamento: los 21
    productos de ese laboratorio (acetaminofén, amlodipina, albendazol, loratadina…) como
    respuesta a la foto de una FEXOFENADINA. El cliente pidió un fármaco y recibió un
    catálogo entero de una marca.

    SE DETECTA POR DATOS, sin lista de laboratorios que mantener: si la mayoría de los
    productos devueltos declara ESE valor en su campo `laboratorio`, el término nombra al
    laboratorio. Un fármaco nunca cumple eso: 'losartan' devuelve productos de muchos
    laboratorios distintos.

    Exige UN SOLO token (los laboratorios son una palabra) y al menos 2 productos con el
    campo, para no decidir con un dato suelto.
    """
    tokens = [w for w in _normalizar_tildes((term or "").lower()).split() if w]
    if not tokens:
        return False
    # Los tokens que NO pueden nombrar un fármaco (relleno, sal) se descartan antes de
    # decidir: 'nombre calox' es lo mismo que 'calox'. Sin esto, el OCR que pierde el
    # fármaco y deja 'nombre del medicamento calox' se colaba como búsqueda de marca.
    significativos = [
        w for w in tokens
        if w not in _PALABRAS_FUNCIONALES and w not in _SALES_FARMACEUTICAS
    ]
    if len(significativos) > 1:
        # Dos o más palabras que pueden ser fármaco: 'fexofenadina clorhidrato calox' es
        # fármaco + marca, NO una marca sola.
        return False
    t = significativos[0] if significativos else tokens[-1]
    labs = [
        _normalizar_tildes(str(p.get("laboratorio") or "").strip().lower())
        for p in products
    ]
    labs = [x for x in labs if x]
    if len(labs) >= 2:
        iguales = sum(1 for x in labs if x == t)
        if iguales / len(labs) >= 0.8:
            return True

    # SIN CAMPO `laboratorio` EN EL PAYLOAD: se decide por la POSICIÓN del término en el
    # título. El CRM (/api/bot/products) devuelve solo {id, nombre, precio, precioBs,
    # disponible} y en el catálogo real (`products-providers`) el laboratorio NO tiene
    # campo propio: 'Brand' guarda cosas como 'MEDICINAS' y el laboratorio aparece como
    # ÚLTIMA palabra del título —
    #     'ACETAMINOFEN 500 MG X 10 TAB CALOX'
    #     'LOSARTAN POTASICO 50MG TAB X 30 ( CALOX)'
    #     'AMLODIPINA TAB 5MG X 10 -CALOX-'
    # Por eso la comprobación anterior (por campo) nunca disparaba en producción: se había
    # validado con productos sintéticos que sí traían `laboratorio`.
    #
    # La regla: el término es el LABORATORIO si cierra la mayoría de los títulos y NO los
    # abre. Un fármaco siempre abre el título ('ACETAMINOFEN 500 MG … CALOX'), así que
    # 'acetaminofen' no puede pasar la primera condición y 'calox' no puede pasar la
    # segunda.
    titulos = [
        _titulo_normalizado(p.get("producto") or p.get("nombre") or p.get("title"))
        for p in products
    ]
    titulos = [x for x in titulos if x]
    if len(titulos) < 3:
        return False
    cierra = sum(1 for x in titulos if _termina_con_token(x, t))
    abre = sum(1 for x in titulos if _empieza_con_token(x, t))
    if cierra / len(titulos) >= 0.8 and abre / len(titulos) <= 0.4:
        return True
    return False


def _nucleo_ausente(term: str, products: list[dict[str, Any]]) -> str | None:
    """El fármaco que el cliente NOMBRÓ no aparece en NINGÚN producto devuelto.

    Devuelve ese token (para intentar corregir su escritura), o None si no aplica.

    POR QUÉ: el motor del CRM matchea por palabras y devuelve lo que comparte CUALQUIER
    token. Si el cliente escribe un fármaco con un typo grande, el término puede acabar
    apoyándose en una palabra GENÉRICA compartida y devolver una lista entera de otra
    cosa — sin que la lista esté vacía, así que ningún camino de corrección se activa.

    Caso real (conv 2109, provider 19, 2026-10-16): el cliente pidió
        'acido acetalicio'   (typo de ácido acetilsalicílico = aspirina)
    y el CRM devolvió 20 productos que comparten solo 'ácido': ACIDO FOLICO, ACIDO
    ASCORBICO, ACIDO TRANEXAMICO... Ninguno es lo que pidió. `filtrar_relevantes`
    tampoco los quita, porque el rescate escalonado encuentra 'acido' (20 de 20) y lo da
    por bueno. La aspirina SÍ está en el catálogo como ACIDO ACETILSALICILICO.

    El núcleo es el ÚLTIMO token de fármaco: el cliente escribe el nombre y añade
    descriptores después ('magnesio plus life', 'acido acetalicio').
    """
    farmacos = tokens_farmaco(term)
    if len(farmacos) < 2:
        return None
    nucleo = farmacos[-1]
    if any(es_relevante(nucleo, str(p.get("nombre") or "")) for p in products):
        return None
    return nucleo


async def _tokens_catalogo_por_prefijos(
    crm: Any, provider_id: str | None, nucleo: str
) -> set[str]:
    """Sondea el catálogo con prefijos DECRECIENTES del núcleo y devuelve sus tokens.

    El catálogo no tiene endpoint de listado, solo búsqueda. En 'cardevidol' el error
    está en la posición 4, así que el prefijo 'card' NO trae CARVEDILOL: hay que bajar
    hasta 'car'. Se acumulan tokens hasta tener suficientes para comparar.
    """
    tokens: set[str] = set()
    for n_p in _PREFIJOS_SONDEO:
        if n_p > len(nucleo):
            continue
        try:
            d_p = await crm.get_products(provider_id, q=nucleo[:n_p], limit=25)
        except Exception:
            continue
        tokens |= _tokens_de_catalogo(d_p.get("products") or [])
        if len(tokens) >= 60:
            break
    return tokens


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
        # Los productos SIN precio van al final: no pueden encabezar la lista (el
        # catálogo del provider 32 trae ProductPrice=0 en algunos SKU y salían primeros
        # con "$—").
        key=lambda p: (
            p.get("precio") if isinstance(p.get("precio"), (int, float)) and p.get("precio") > 0
            else float("inf")
        ),
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
        # OCR del turno ANTERIOR (antes de que este turno lo pise). Vive aquí porque
        # `_tool_loop` no ve las variables locales de `run_turn`: tenerlo como local
        # produjo un NameError que mataba en silencio todos los turnos en los que el
        # cliente "referencia la foto" ("gracias, ya vi la foto" → sin respuesta).
        self.prev_last_ocr_term = ""
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
        # F14.2 — true cuando el mensaje traía "opción N" y la opción se resolvió
        # contra `last_options` (lista que el cliente YA vio). Es una COMPRA
        # CONFIRMADA, no una consulta nueva: el backstop de catálogo NO debe
        # reinterpretar el resto del mensaje como medicamento (caso real:
        # "Precio de la macrodantina?? Voy a querer una vitamina C de la opción
        # 9" extraía "macrodantina querer vitamina opción" y buscaba esa basura).
        self.opcion_resuelta = False
        # Texto del cliente de ESTE turno (lo escribe app/turn.py:542). Se
        # inicializa aquí para que cualquier lectura del runtime —incluido
        # `_forma_del_pedido`— no reviente si el turno todavía no lo puso.
        self.texto_cliente: str = ""
        # SKU reales que el BACKSTOP ya agregó al carrito en ESTE turno (pre-LLM
        # o en-loop). El LLM luego vuelve a llamar agregar_al_carrito con los
        # mismos productos (pensa que debe confirmar la selección); sin esto, el
        # ON CONFLICT de cart_add suma +1 y la cantidad sale doblada (pidió 1 y
        # quedan 2). Estos SKU se tratan como ya-agregados: idempotente.
        self.backstop_added_skus: set[str] = set()
        # CONFIRMACIONES del carrito para el texto final. Caso real (provider 32,
        # 2026-10-29): "Precio de la macrodantina?? Voy a querer una vitamina C de la
        # opción 9" → el backstop SÍ agregó la opción 9 al carrito (lo dice el log), pero
        # la respuesta fue solo la lista de precios: el cliente nunca supo que se lo
        # habíamos agregado.
        self.cart_confirmaciones: list[str] = []
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
        # true si el último `buscar_medicamento` descartó el término por ser el
        # LABORATORIO de los productos (CALOX, GENVEN...), no un medicamento. El turno lo
        # usa para IGNORAR ese término en silencio: no es un "no disponible", es que el
        # cliente nunca nombró un fármaco.
        self.es_laboratorio = False
        # true si el término era SOLO la sal del principio activo ('clorhidrato'): no es un
        # medicamento y no debe aparecer en la lista de "no disponibles".
        self.es_sal = False
        # Aviso cuando la FORMA pedida (suspensión, gotas, crema…) no está entre los
        # productos: el turno lo antepone a la respuesta para que el cliente sepa que le
        # estamos ofreciendo otra presentación.
        self.aviso_forma = ""
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
        # Se resetean en CADA búsqueda: si no, el flag de la búsqueda anterior
        # ('calox' era un laboratorio) se arrastra al siguiente medicamento de la misma
        # receta y lo saltaría en silencio.
        self.es_laboratorio = False
        self.es_sal = False
        self.aviso_forma = ""
        # Se enciende cuando el cliente DESCRIBE el producto sin nombrarlo ('crema para
        # cabello rizado'): el turno lo usa para no quedarse en silencio si el modelo gasta
        # las rondas reintentando búsquedas.
        self.pide_recomendacion = False
        self.pide_recomendacion_intentos = 0
        # Se enciende si la respuesta salió del PRINCIPIO ACTIVO de una marca
        # ('depomedrol' → 'metilprednisolona'): ese cambio de término es legítimo y el
        # guard de "otro fármaco" no debe dispararse.
        self.busqueda_por_principio = False
        if not nombre:
            return {"ok": False, "error": "nombre_vacio", "detalle": "indica qué medicamento buscas"}
        # Guarda anti-saludo: si el LLM llamó buscar_medicamento con un término
        # que es SOLO saludos/cortesía ("saludos", "buen día"), NO es una
        # consulta de medicamento. Devolver una lista de productos aquí sería
        # un error grave (el cliente saludó y le respondemos con 19 productos).
        # Se devuelve un resultado que le dice al LLM que responda con un
        # saludo, sin tocar el catálogo.
        # ¿EL TÉRMINO ES UNA PREGUNTA DE PRECIO, NO UN MEDICAMENTO?
        # Caso real (provider 19, 2026-10-27): "Buenas tardes tiene disponible crema dental
        # sensodyne y que costo porf" → el LLM buscó 'costo porf' y el matcher difuso lo
        # apoyó en 'POR': AMPICILINA "POR 1 AMPOLLA" y ANTILAX "POR 10 TABLETAS" aparecieron
        # como si fueran lo que el cliente pidió. `_limpiar_termino_medicamento` ya sabe que
        # 'costo' y 'porf' son palabras funcionales; si al limpiarlo no queda NADA, no hay
        # medicamento que buscar. No se marca `med_not_found` (no es un "no disponible": el
        # cliente no nombró ningún fármaco).
        _termino_limpio = _limpiar_termino_medicamento(nombre)
        if _termino_limpio.strip() and _es_solo_sal(_termino_limpio):
            logger.info(
                "buscar_medicamento: '%s' es la SAL del principio activo, no un medicamento "
                "— no busco en catálogo", nombre,
            )
            self.es_sal = True
            self.med_not_found = False
            return {
                "ok": False,
                "error": "solo_sal",
                "detalle": (
                    "Ese término es la SAL del principio activo (clorhidrato, maleato, "
                    "fosfato…), no un medicamento: decenas de fármacos distintos la "
                    "comparten. NO busques en el catálogo y NO muestres productos. Pide al "
                    "cliente el NOMBRE del medicamento que aparece en la caja (el que va "
                    "antes de 'clorhidrato'), o el que le recetaron."
                ),
            }
        if not _termino_limpio.strip():
            logger.info(
                "buscar_medicamento: '%s' son solo palabras funcionales (precio/cortesía) — "
                "no busco en catálogo",
                nombre,
            )
            return {
                "ok": False,
                "error": "sin_medicamento",
                "detalle": (
                    "El mensaje no nombra ningún medicamento: es una pregunta de precio o "
                    "cortesía. NO busques en el catálogo y NO inventes productos: si el "
                    "cliente ya recibió una lista de opciones, responde con el precio de lo "
                    "que acabas de listar."
                ),
            }
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
        # MULETILLAS DEL HABLA: si TODOS los tokens del término son cortesía/coletilla, el
        # LLM lo extrajo de una nota de voz y NO es un medicamento. Caso real (conv 2834):
        # "Buenos días mi amor... verdad?" produjo los términos 'amor' y 'verdad', se
        # consultaron y el cliente recibió "No disponibles: DÍAS AMOR, VERDAD".
        #
        # Va ANTES del guard de sustantivo porque ese los deja pasar: 'amor' y 'verdad'
        # tienen 5-6 letras y no son funcionales, así que parecen plausibles sueltos.
        if _es_solo_muletilla_habla(nombre):
            logger.info(
                "buscar_medicamento: '%s' es solo muletilla del habla — no busco "
                "en catálogo (extraída de una nota de voz)",
                nombre,
            )
            return {
                "ok": False,
                "error": "no_medicamento",
                "detalle": (
                    "Ese texto es parte de la CONVERSACIÓN del cliente (un saludo o una "
                    "coletilla del habla), NO un medicamento. NO lo presentes como un "
                    "producto que no está en el catálogo. Responde a lo que el cliente "
                    "realmente pide: si preguntó por un producto, búscalo con su NOMBRE; "
                    "si solo saludó, saluda y pregunta qué medicamento necesita. "
                    "NUNCA muestres lista de productos."
                ),
            }
        # CATEGORÍA TERAPÉUTICA SOLA ("anticonceptivas", "antibióticos"): no es un producto.
        # El cliente necesita que le PREGUNTEN cuál, no que le digan que no está.
        _palabras_cat = [
            _normalizar_tildes(w.strip(" -.,;:()[]").lower())
            for w in (nombre or "").split()
            if w.strip(" -.,;:()[]")
        ]
        if (
            _palabras_cat
            and any(
                w in _CATEGORIAS_TERAPEUTICAS or w in _ENFERMEDADES
                for w in _palabras_cat
            )
            and all(
                len(w) < 3
                or w in _CATEGORIAS_TERAPEUTICAS
                or w in _VIAS_ADMINISTRACION
                or w in _ENFERMEDADES
                or w in _PALABRAS_FUNCIONALES
                for w in _palabras_cat
            )
        ):
            logger.info(
                "buscar_medicamento: '%s' es una CATEGORÍA, no un producto — pregunto cuál",
                nombre,
            )
            return {
                "ok": False,
                "error": "solo_categoria",
                "detalle": (
                    f"El cliente pidió una CATEGORÍA ('{nombre}'), no un producto concreto. "
                    "NO digas que no lo tienes ni que no está en el catálogo: no has buscado "
                    "nada. Pregúntale con naturalidad qué marca o nombre exacto necesita "
                    "(una sola pregunta, corta) y ofrécele ayuda para encontrarlo. "
                    "NUNCA muestres listas de productos."
                ),
            }
        # DESCRIPCIÓN SIN NOMBRE ("una crema para cabello rizado", "para adultos"): el
        # cliente pide una recomendación, no un artículo concreto. Listar es garantía de
        # basura (pañales, cintas, rociadores). Se pregunta qué busca exactamente.
        _palabras_atr = [
            _normalizar_tildes(w.strip(" -.,;:()[]").lower())
            for w in (nombre or "").split()
            if w.strip(" -.,;:()[]")
        ]
        # La señal puede ser un ATRIBUTO ('cabello', 'adultos'), una ENFERMEDAD ('hongos') o
        # una CATEGORÍA ('antibiótico'): en los tres casos describe, no nombra.
        if _palabras_atr and any(
            w in _ATRIBUTOS_NO_PRODUCTO
            or w in _ENFERMEDADES
            or w in _CATEGORIAS_TERAPEUTICAS
            for w in _palabras_atr
        ) and all(
            # Los artículos y preposiciones de 1-2 letras ('el', 'la', 'de', 'y') no
            # cuentan: 'gel para el pelo' fallaba justo por 'el'.
            len(w) < 3
            or w in _ATRIBUTOS_NO_PRODUCTO
            or w in _CATEGORIAS_TERAPEUTICAS
            or w in _VIAS_ADMINISTRACION
            or w in _ENFERMEDADES
            or w in _PALABRAS_FUNCIONALES
            or w in _FORMA_CANONICA
            for w in _palabras_atr
        ):
            self.pide_recomendacion = True
            self.pide_recomendacion_intentos += 1
            logger.info(
                "buscar_medicamento: '%s' describe el producto pero no lo nombra — pregunto "
                "(intento %d)", nombre, self.pide_recomendacion_intentos,
            )
            if self.pide_recomendacion_intentos >= 2:
                # Ya intentó variantes: responder AHORA.
                return {
                    "ok": False,
                    "error": "pide_recomendacion",
                    "detalle": (
                        "YA intentaste buscar esto más de una vez y no hay nada que buscar: el "
                        "cliente NO ha nombrado ningún producto. NO vuelvas a llamar a "
                        "buscar_medicamento en este turno. RESPONDE AHORA al cliente con UNA "
                        "pregunta corta pidiéndole el producto o la marca que busca."
                    ),
                }
            return {
                "ok": False,
                "error": "pide_recomendacion",
                "detalle": (
                    f"El cliente describe PARA QUÉ o PARA QUIÉN quiere el producto "
                    f"('{nombre}'), pero NO nombra ninguno. NO has buscado nada y NO "
                    "puedes listar productos: en el catálogo esas palabras están en cientos "
                    "de artículos que no tienen nada que ver. Pregúntale, en UNA línea y con "
                    "naturalidad, qué producto o marca busca (por ejemplo: ¿champú, crema de "
                    "peinar, tratamiento?), y ofrécele ayuda para encontrarlo."
                ),
            }
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
        # F14.4 — ALIAS de escritura: si el término es una grafía CONOCIDA de un
        # medicamento, se reescribe a la canónica ANTES de consultar y se apaga
        # la tolerancia a typos de esa búsqueda. Una grafía conocida no necesita
        # que el matcher adivine, y el fuzzy de 2 ediciones es justo lo que hacía
        # saltar de categoría terapéutica ('vivacina' → VITAMINA K y C).
        nombre_original = nombre
        nombre_alias, es_alias = _reescribir_alias(nombre)
        if es_alias:
            logger.info(
                "buscar_medicamento: alias '%s' → '%s' (sin fuzzy en esta búsqueda)",
                nombre, nombre_alias,
            )
            nombre = nombre_alias
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
        products = filtrar_relevantes(nombre, products, permitir_typo=not es_alias)
        if len(products) != antes:
            logger.info(
                "buscar_medicamento: '%s' — %d/%d productos descartados por relevancia",
                nombre, antes - len(products), antes,
            )

        # ¿EL TÉRMINO ES UN LABORATORIO Y NO UN MEDICAMENTO? (ver el docstring)
        # Va ANTES de cualquier recuperación: no hay nada que recuperar, el cliente no
        # nombró un fármaco. Sin esto, 'calox' devolvía 21 productos de esa marca como
        # respuesta a la foto de una FEXOFENADINA.
        if products and _es_nombre_de_laboratorio(nombre, products):
            logger.info(
                "buscar_medicamento: '%s' es un LABORATORIO (no un medicamento) — "
                "descarto los %d productos de esa marca", nombre, len(products),
            )
            self.es_laboratorio = True
            # `med_not_found` NO se marca: no es un "no disponible" (el cliente no nombró
            # ningún fármaco) y marcarlo dispara el handoff determinista.
            self.med_not_found = False
            self.last_products = []
            self.last_options = []
            # RETURN INMEDIATO: sin esto, la cascada de "sin resultados" que viene después
            # intenta variantes por typo y RESUCITA el catálogo de la marca. Medido:
            #   'calox' es un LABORATORIO — descarto los 20 productos de esa marca
            #   'calox' sin resultados — encontrado con la variante 'salox' (20 productos)
            return {
                "ok": False,
                "error": "solo_laboratorio",
                "detalle": (
                    "Ese término es el LABORATORIO que aparece en la caja, no el medicamento: "
                    "esa marca fabrica decenas de fármacos distintos. NO busques en el catálogo "
                    "y NO muestres productos de esa marca. Pide al cliente el NOMBRE del "
                    "medicamento que va antes de la marca."
                ),
            }

        # EL FÁRMACO QUE EL CLIENTE NOMBRÓ NO ESTÁ EN LA LISTA (pero la lista NO está
        # vacía, así que ninguno de los caminos de abajo se activaría).
        #
        # Caso real (conv 2109, provider 19, 2026-10-16): 'acido acetalicio' (typo de
        # ácido acetilsalicílico) devolvía 20 productos que solo comparten 'ácido'
        # (ACIDO FOLICO, ACIDO ASCORBICO, ACIDO TRANEXAMICO...). Ninguno es lo que el
        # cliente pidió. Se intenta la MISMA corrección por catálogo que ya existe para
        # los typos largos, pero disparada por «el fármaco nombrado no está» en vez de
        # por «la lista está vacía». NUNCA se inventa el término: el token corregido
        # tiene que EXISTIR en el catálogo.
        if products:
            nucleo = _nucleo_ausente(nombre, products)
            token_ok = None
            if nucleo:
                tokens_cat = await _tokens_catalogo_por_prefijos(
                    self._ctx.crm, self._provider_id, nucleo
                )
                token_ok, ratio_t, margen_t = _mejor_token_catalogo(nucleo, tokens_cat)
                if (
                    token_ok
                    and token_ok != nucleo
                    and ratio_t >= _UMBRAL_TYPO_CATALOGO
                    and margen_t > 0.0
                    and _correccion_por_largo_plausible(nucleo, token_ok)
                ):
                    prefijo = " ".join(nombre.strip().split()[:-1])
                    term_corregido = (
                        f"{prefijo} {token_ok}".strip() if prefijo else token_ok
                    )
                    data_c = await self._ctx.crm.get_products(
                        self._provider_id, q=term_corregido, limit=20
                    )
                    candidatos_c = _dedupe_por_nombre(data_c.get("products") or [])
                    corregidos = filtrar_relevantes(term_corregido, candidatos_c)
                    if corregidos:
                        logger.info(
                            "buscar_medicamento: '%s' — el fármaco '%s' no está en los "
                            "%d resultados; recuperado por catálogo: '%s' "
                            "(ratio %.3f, margen %.3f)",
                            nombre, nucleo, len(products), term_corregido,
                            ratio_t, margen_t,
                        )
                        self.corregido_desde = nombre
                        self.corregido_a = term_corregido
                        self.last_term = term_corregido
                        data = data_c
                        products = corregidos

                # SI EL FÁRMACO SIGUE SIN APARECER, BUSCAR SOLO CON EL NÚCLEO.
                #
                # Corregir el typo no basta cuando el cliente usa un SINÓNIMO que el
                # catálogo escribe de otra forma: la frase corregida puede volver a caer
                # al OR por una palabra genérica. Hay que soltar el resto del término y
                # pedir el fármaco a secas.
                #
                # Caso real (conv 3132, provider 05, 2026-10-19):
                #     cliente: "Ácido hialuronico 25mg/ 2,5ml"
                #     'acido hialuronico'  -> corregido a 'acido hialuronato' -> 20 ACIDO
                #       FOLICO / ASCORBICO / BORICO (el AND falla y el OR se apoya en
                #       'acido'); TOPTEAR NO aparecía.
                #     'hialuronico' o 'hialuronato' SOLOS -> 1 producto:
                #       TOPTEAR 4 MG / 10 ML (HIALURONATO DE SODIO)   <- el que pedía
                # El catálogo lo tenía todo el tiempo; lo escondía la palabra 'acido'.
                if _nucleo_ausente(self.corregido_a or nombre, products):
                    nucle_buscar = token_ok or nucleo
                    # SOLO si el núcleo sigue siendo EL MISMO FÁRMACO. Una corrección
                    # «parecida» de OTRA marca no sirve: 'hialuronico'→'hialuronato'
                    # puntúa 0.95 (es el mismo principio activo, y en el catálogo está
                    # TOPTEAR), pero 'sensodyne'→'sensitive' puntúa 0.78 y lo único que
                    # consigue es tapar el aviso de que SENSODYNE no está, cambiándolo por
                    # una pasta de otra marca. Se exige variante de escritura.
                    # SOLO CUANDO EL NÚCLEO ERA UN TYPO. Si la palabra ausente EXISTE tal
                    # cual en el catálogo, buscar solo por ella no corrige nada: CAMBIA DE
                    # TEMA. Caso real (provider 32, 2026-10-27): "tiras reactivas para el
                    # glucómetro" → 3 tiras encontradas, el núcleo 'glucometro' no estaba en
                    # ellas, y el agente buscaba SOLO por 'glucometro' → el cliente preguntó
                    # por tiras y recibió glucómetros.
                    if nucle_buscar != nucleo and ratio_t >= _UMBRAL_NUCLEO_MISMO_FARMACO:
                        data_n = await self._ctx.crm.get_products(
                            self._provider_id, q=nucle_buscar, limit=20
                        )
                        cand_n = _dedupe_por_nombre(data_n.get("products") or [])
                        solo_nucleo = filtrar_relevantes(nucle_buscar, cand_n)
                        # Tiene que aparecer DE VERDAD en algún nombre: si no, no mejora.
                        _nu = _normalizar_tildes(nucle_buscar.lower())
                        if solo_nucleo and any(
                            _nu in _normalizar_tildes(
                                str(p.get("nombre") or p.get("producto") or "").lower()
                            )
                            for p in solo_nucleo
                        ):
                            logger.info(
                                "buscar_medicamento: '%s' — '%s' sigue sin estar en los %d "
                                "resultados; busco SOLO por el núcleo '%s' (%d productos)",
                                nombre, nucleo, len(products), nucle_buscar, len(solo_nucleo),
                            )
                            products = solo_nucleo
                            data = data_n
                            self.last_term = nucle_buscar
                            if not self.corregido_a:
                                self.corregido_desde = nombre
                                self.corregido_a = nucle_buscar

        # Dedupe por nombre de producto: el catálogo de Firebase repite el MISMO
        # ítem (mismo nombre) con distintos productId/precio (una entrada por
        # farmacia/precio). Quedarse con el de MENOR precio evita listas de 20
        # con 14 duplicados idénticos.
        products = _dedupe_por_nombre(products)
        # FAIL-SAFE del alias (F14.4): si la grafía canónica no dio NADA en este
        # catálogo, se reintenta con la grafía ORIGINAL del cliente y tolerancia
        # a typos. Un alias solo puede ayudar: nunca puede esconder un resultado
        # (p. ej. el catálogo tiene MACRODANTINA y no NITROFURANTOINA).
        if not products and es_alias and nombre_original:
            logger.info(
                "buscar_medicamento: alias '%s' sin resultados — reintento con la "
                "grafía original", nombre,
            )
            data_orig = await self._ctx.crm.get_products(
                self._provider_id, q=_normalizar_tildes(nombre_original), limit=20
            )
            candidatos_orig = _dedupe_por_nombre(data_orig.get("products") or [])
            products = filtrar_relevantes(
                _normalizar_tildes(nombre_original), candidatos_orig
            )
            if products:
                nombre = _normalizar_tildes(nombre_original)
                self.last_term = nombre
                es_alias = False

        # REINTENTO POR EL FÁRMACO CABEZA (cliente): la frase completa no trajo el
        # fármaco al catálogo (el motor matchea TODOS los tokens). Se aísla el
        # primer token con cuerpo de fármaco y se re-filtra con el término original.
        if not products:
            cabeza = _token_cabeza_farmaco(nombre)
            if cabeza and cabeza != nombre.lower():
                data_c = await self._ctx.crm.get_products(
                    self._provider_id, q=cabeza, limit=20
                )
                candidatos = _dedupe_por_nombre(data_c.get("products") or [])
                products = filtrar_relevantes(
                    nombre, candidatos, permitir_typo=not es_alias
                )
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
                products = filtrar_relevantes(nombre, products, permitir_typo=not es_alias)
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
                products = filtrar_relevantes(nombre, candidatos, permitir_typo=not es_alias)
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
            # TERCERA PASADA: typos LARGOS (distancia 3+), usando el CATÁLOGO como
            # diccionario. `_variantes_typo` genera variantes a ciegas y solo alcanza
            # distancia ≤2; estos dos casos reales se le escapan:
            #     'hidrocoticida' → hidroclorotiazida   (distancia 5)
            #     'cardevidol'    → carvedilol          (distancia 3)
            # El cliente los escribió así y recibió "no disponibles" aunque el
            # medicamento SÍ estaba (provider 27, conv 2824).
            #
            # CÓMO: se sondea el catálogo con prefijos decrecientes del término y se
            # busca el TOKEN REAL más parecido (difflib). NUNCA se inventa: el término
            # con el que se reintenta es un token que EXISTE en el catálogo.
            #
            # POR QUÉ CON PREFIJOS: el catálogo no tiene endpoint de listado, solo
            # búsqueda. En 'cardevidol' el error está en la posición 4, así que el
            # prefijo 'card' NO trae CARVEDILOL; hace falta bajar hasta 'car'.
            nucleo_typo = nombre.strip().lower().split()[-1] if nombre.strip() else ""
            prefijo_typo = " ".join(nombre.strip().split()[:-1])
            tokens_cat = await _tokens_catalogo_por_prefijos(
                self._ctx.crm, self._provider_id, nucleo_typo
            )
            token_ok, ratio_t, margen_t = _mejor_token_catalogo(nucleo_typo, tokens_cat)
            if (
                token_ok
                and ratio_t >= _UMBRAL_TYPO_CATALOGO
                and margen_t > 0.0
                and _correccion_por_largo_plausible(nucleo_typo, token_ok)
            ):
                term_corregido = (
                    f"{prefijo_typo} {token_ok}".strip() if prefijo_typo else token_ok
                )
                logger.info(
                    "buscar_medicamento: '%s' sin resultados — recuperado por catálogo: "
                    "'%s' (ratio %.3f, margen %.3f, %d tokens)",
                    nombre, term_corregido, ratio_t, margen_t, len(tokens_cat),
                )
                data_c = await self._ctx.crm.get_products(
                    self._provider_id, q=term_corregido, limit=20
                )
                # RE-FILTRAR contra el término CORREGIDO (no contra el escrito): el
                # token ya es del catálogo, así que la relevancia debe evaluarse sobre él.
                candidatos_c = _dedupe_por_nombre(data_c.get("products") or [])
                products = filtrar_relevantes(term_corregido, candidatos_c)
                if products:
                    self.corregido_desde = nombre
                    self.corregido_a = term_corregido
                    self.last_term = term_corregido
                    data = data_c
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
                self.es_laboratorio = False
                self.busqueda_por_principio = True
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
                    "agotado. "
                    # F14.3b — lo que NO se puede afirmar cuando la búsqueda no dio
                    # nada. Medido en el Laboratorio (persona 'errores_modismos'): el
                    # agente decía "el esoz está agotado" y "ya la habías consultado
                    # antes y estaba agotada" — INVENTA estado de inventario y memoria
                    # de turnos que no están en esta conversación. Los guards G6/G10 no
                    # lo cubren (exigen respaldo del catálogo y aquí SÍ se consultó), así
                    # que la regla va donde el modelo tiene el contexto: el resultado de
                    # la herramienta.
                    "PROHIBIDO decir que el medicamento 'está agotado', 'se acabó', "
                    "'no hay existencia' o 'no quedan': no tenemos estado de inventario. "
                    "PROHIBIDO decir que el cliente 'ya lo había consultado antes', "
                    "'la última vez' o 'sigue igual' si eso NO aparece en ESTA "
                    "conversación: no tienes memoria de otros hilos. Di simple y "
                    "honesto: 'no lo tengo en el catálogo'."
                ),
                "busqueda": nombre,
            }
        # ACOTAR A LA FORMA QUE PIDIÓ (suspensión, tabletas, gotas, crema…). Va aquí, en
        # el punto único de la búsqueda, para que TODOS los caminos lo hereden: el LLM
        # cuando redacta la lista y los backstops cuando la sustituyen.
        products, self.aviso_forma = _aplicar_forma(
            products, nombre, self._forma_del_pedido(nombre)
        )
        if self.aviso_forma:
            # La forma pedida no está con ese nombre, pero puede estar bajo una MARCA
            # ('metronidazol óvulos' → OVUMET 500MG X 10OVU). Se suman esas
            # presentaciones y el aviso dice de dónde salen.
            por_marca = await self._marcas_en_la_forma(nombre, self._forma_del_pedido(nombre))
            if por_marca:
                familia_ped, etiqueta_ped = self._forma_del_pedido(nombre)
                droga = _termino_sin_forma(nombre).upper()
                nombres = ", ".join(
                    _titulo_de_producto(p).split(" X ")[0][:38] for p in por_marca
                )
                products = list(products) + por_marca
                self.aviso_forma = (
                    f"⚠️ No tengo {droga} en {etiqueta_ped} con ese nombre. "
                    f"En {etiqueta_ped} tengo: {nombres}."
                )
                logger.info(
                    "buscar_medicamento: '%s' — %d presentación(es) de la forma pedida "
                    "encontradas por marca", nombre, len(por_marca),
                )
            logger.info(
                "buscar_medicamento: '%s' — %d productos tras acotar a la forma · %s",
                nombre, len(products), self.aviso_forma,
            )
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
        instrucciones = self._formatear_instrucciones_busqueda(products, nombre)
        if self.aviso_forma:
            # El LLM tiene que DECIRLO: si no, el cliente ve una lista bajo el nombre de
            # la presentación que pidió, que es el fallo que se está corrigiendo.
            instrucciones = (
                "EMPIEZA tu respuesta EXACTAMENTE con esta línea, tal cual: "
                + self.aviso_forma
                + "\nDespués, la lista de lo que sí hay.\n\n"
                + instrucciones
            )
        return {
            "ok": True,
            "products": products,
            "provider": data.get("provider"),
            "instrucciones": instrucciones,
        }

    def _forma_del_pedido(self, nombre: str) -> tuple[str | None, str | None]:
        """La forma que pidió el cliente para ESTE medicamento.

        Primero en el término y, si ahí no está (el LLM busca solo con el fármaco), en la
        línea del mensaje del cliente donde aparece el medicamento. Se mira línea a línea
        para no aplicar la forma de otro medicamento de una lista.
        """
        familia, etiqueta = _forma_pedida(nombre)
        if familia:
            return familia, etiqueta
        texto = self.texto_cliente or ""
        if not texto:
            return None, None
        clave = _normalizar_tildes((nombre or "").lower()).split()
        if not clave:
            return None, None
        for linea in re.split(r"[\n;,]|\s+y\s+", texto):
            if clave[0] not in _normalizar_tildes(linea.lower()):
                continue
            familia, etiqueta = _forma_pedida(linea)
            if familia:
                return familia, etiqueta
        return None, None

    async def _marcas_comerciales_de(self, principio: str) -> list[str]:
        """Marcas comerciales conocidas de un principio activo (para buscarlas en catálogo).

        Devuelve [] si el modelo no sabe o falla: el silencio es preferible a inventar una
        marca. Las marcas NO se ofrecen por sí solas — `_marcas_en_la_forma` exige que la
        marca exista de verdad en el catálogo y en la forma pedida.
        """
        principio = (principio or "").strip().lower()
        if len(principio) < 4:
            return []
        try:
            reply = await self._ctx.llm.complete(
                [
                    {
                        "role": "user",
                        "content": (
                            f"Enumera hasta 5 MARCAS COMERCIALES de '{principio}' que se "
                            "vendan en farmacias de Venezuela (por ejemplo, para "
                            "'metronidazol' podría ser 'Ovumet'). Una por línea, SOLO el "
                            "nombre de la marca, sin dosis, sin presentación y sin "
                            "explicaciones. Si no conoces ninguna, responde exactamente "
                            "'NINGUNA'."
                        ),
                    }
                ],
                tools=None,
            )
            crudo = (reply.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("marcas de '%s': el LLM falló (%s)", principio, exc)
            return []
        if not crudo or "ninguna" in crudo.lower()[:40]:
            return []
        marcas: list[str] = []
        for linea in crudo.splitlines():
            limpio = re.sub(r"^[\s\-*•\d.)]+", "", linea).strip(" \"'.,;:")
            limpio = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ ]+", "", limpio).strip()
            # Solo la marca: si la línea trae explicación ("Ovumet - óvulos de ..."), se
            # queda la primera palabra significativa.
            palabras = [w for w in limpio.split() if len(w) >= 4]
            if not palabras:
                continue
            marca = palabras[0]
            if marca.lower() in {"marca", "generico", "genérico", "ejemplo", "para", "como"}:
                continue
            if marca.lower() not in {m.lower() for m in marcas}:
                marcas.append(marca)
        logger.info("marcas comerciales de '%s': %s", principio, marcas[:5])
        return marcas[:5]

    async def _marcas_en_la_forma(
        self, nombre: str, forma: tuple[str | None, str | None] | None
    ) -> list[dict[str, Any]]:
        """Productos de la MISMA forma pedida que son el fármaco bajo una marca comercial.

        Se verifica en el catálogo: la marca tiene que aparecer en el título y el título
        tiene que ser de la forma pedida (óvulos, crema…). Máximo 3, sin repetir.
        """
        if not self._provider_id:
            return []
        familia, _etiqueta = forma or (None, None)
        if not familia:
            return []
        principio = _termino_sin_forma(nombre).strip().lower()
        if not principio:
            return []
        marcas = await self._marcas_comerciales_de(principio)
        if not marcas:
            return []
        dentro: list[dict[str, Any]] = []
        vistos: set[str] = set()
        for marca in marcas:
            try:
                data = await self._ctx.crm.get_products(
                    self._provider_id, q=marca, limit=10
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("marca '%s': fallo consultando el catálogo (%s)", marca, exc)
                continue
            for p in data.get("products") or []:
                titulo = _titulo_de_producto(p)
                titulo_n = _normalizar_tildes(titulo.lower())
                if _normalizar_tildes(marca.lower()) not in titulo_n:
                    continue
                if _FAMILIA_DE_FORMA.get(_etiqueta_de_forma(titulo) or "") != familia:
                    continue
                clave = str(p.get("id") or titulo)
                if clave in vistos:
                    continue
                vistos.add(clave)
                dentro.append(p)
                logger.info(
                    "marca del principio activo en la forma pedida: '%s' (%s) → %s",
                    principio, familia, titulo[:50],
                )
            if len(dentro) >= 3:
                break
        return dentro[:3]

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
            "composiciones adicionales. "
            # F14.3 — APERTURA SIN CONTRADICCIÓN. Caso real: el agente abría con
            # "No tengo SOLUNOVAL BUCAL" y acto seguido listaba los productos
            # encontrados (SOLUNOVAR, PINVEX). El cliente lee "no hay" y no ve la
            # lista. Si HAY resultados, el mensaje empieza por lo que SÍ tienes.
            "APERTURA: tienes productos ('products' NO está vacío), así que la "
            "respuesta EMPIEZA por ellos ('Lo más parecido que tengo es…' o "
            "'Tengo estas opciones:'). Está PROHIBIDO abrir o cerrar el mensaje "
            "con 'No tengo X', 'no lo tengo', 'no está disponible' o 'no lo "
            "encontré' cuando estos resultados existen: el 'no tengo el exacto' "
            "solo se escribe si NO hay ningún producto que mostrar. Puedes aclarar "
            "la diferencia DESPUÉS de la lista, en una línea (p. ej. \"no manejamos "
            "esa presentación, pero esto es lo mismo\")."
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
                # 2) por NOMBRE EXACTO normalizado
                objetivo = _norm(producto) if producto else ""
                if elegido is None and objetivo:
                    elegido = next(
                        (c for c in candidatos
                         if _norm(str(c.get("producto") or c.get("nombre") or "")) == objetivo),
                        None,
                    )
                # 3) por RELEVANCIA ESTRICTA, con DOSIS COINCIDENTE y resultado ÚNICO.
                #
                #    Hace falta un tercer intento porque el CRM busca DIFUSO: al pedirle
                #    un nombre del carrito devuelve productos PARECIDOS pero no idénticos.
                #    Caso real: para 'COLON VITAL LIFE X 60 CAP WALIFE' devolvió FATSLIM,
                #    CASTAÑA DE INDIA, ALCACHOFA… (todos de la marca WALIFE).
                #
                #    PERO LA RELEVANCIA NO BASTA: medido, el matcher estricto acepta
                #    'ACETAMINOFEN 500MG TAB X 20 -ELTER-' como equivalente de
                #    'ACETAMINOFEN 650 MG X 10 TAB ELTER' (mismo fármaco y marca, DISTINTA
                #    concentración). Colgarle ese precio en Bs sería un precio FALSO —
                #    peor que omitirlo, que es el problema que estamos arreglando.
                #
                #    DOS CONDICIONES, ambas necesarias:
                #      a) SI el nombre del carrito trae dosis, el candidato debe traer la
                #         MISMA. Si el carrito no la trae ('TILODRIN JBE'), no se exige —
                #         exigirla ahí descartaba coincidencias correctas.
                #      b) El resultado debe ser ÚNICO. Varios candidatos que pasan el
                #         filtro significa ambigüedad (misma dosis, distinto tamaño de
                #         envase → distinto precio): mejor omitir el Bs que arriesgar.
                if elegido is None and producto:
                    dosis_obj = _extraer_dosis(producto)
                    matches = [
                        c for c in candidatos
                        if es_relevante_estricto(
                            producto, str(c.get("producto") or c.get("nombre") or "")
                        )
                        and (
                            not dosis_obj
                            or _extraer_dosis(
                                str(c.get("producto") or c.get("nombre") or "")
                            ) == dosis_obj
                        )
                    ]
                    if len(matches) == 1:
                        elegido = matches[0]
                    elif len(matches) > 1:
                        logger.info(
                            "agregar_al_carrito: %d candidatos ambiguos para '%s' — "
                            "no recupero el Bs (mejor omitirlo que arriesgar otro precio)",
                            len(matches), producto,
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
