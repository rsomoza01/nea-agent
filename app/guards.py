"""Guardas deterministas anti-alucinación (rama agent-qa, T4 del plan QA).

Por qué existen: el Laboratorio demostró (baseline 29/100 y controles A2/B2
14/100) que la alucinación es CROSS-MODEL — 14B, 7B y LongCat inventan
precios, políticas y "recibí tu foto". Es un problema de ARQUITECTURA, no del
modelo: donde ya existía una guarda determinista (ver_carrito, catálogo
forzado, precio inventado) el agente no falla. Este módulo replica ese patrón
para los tres huecos restantes + hostilidad.

Cada guarda es CÓDIGO, no generación libre: detecta (regex determinista) y
reemplaza por plantilla constante. Cada activación se loguea con el prefijo
"guarda G#" para el conteo de vetos del reporte QA.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# G1 — Precio sin dato real
# ---------------------------------------------------------------------------
# El cliente pregunta un precio y la conversación NO tiene respaldo del
# catálogo (runtime.last_products vacío). El LLM rellena con números inventados.
PIDE_PRECIO = re.compile(
    r"\b(precio[s]?\b|cu[aá]nto\s+(cuesta|vale|sale|es)|a\s+c[óo]mo\s+(est[áa]|sale)|"
    r"cu[aá]nto\s+me\s+(sale|costar[íi]a|ser[íi]a)|me\s+cuesta)\b",
    re.I,
)

TPL_SIN_PRECIO = (
    "Lo siento, no tengo un precio confirmado de eso en este momento y no "
    "quiero darte un número equivocado. Te puedo comunicar con un asistente "
    "de la farmacia para que te lo confirme. ¿Lo deseas?"
)

# ---------------------------------------------------------------------------
# G2 — Intención fuera del knowledge base (políticas de negocio)
# ---------------------------------------------------------------------------
# Temas que el agente NO puede resolver con sus herramientas (catálogo,
# carrito, pedido): el LLM inventa políticas de devolución/reclamo. Respuesta
# de escalado HARDCODEADA + handoff; jamás generación libre.
FUERA_KB = re.compile(
    r"\b(devoluci[óo]n|devolver|devuelv|reclam[oa]|garant[íi]a|reembols|"
    r"vencid[oa]|pol[íi]tic|dinero de vuelta|me lo cambian|hacen? (una )?(entrega|domicilio)|"
    r"env[íi]o a domicilio)",
    re.I,
)

TPL_FUERA_KB = (
    "Eso está fuera de lo que puedo resolver por este medio y no quiero "
    "darte información incorrecta. Te comunico con un asistente de la "
    "farmacia para que te responda con precisión."
)

# ---------------------------------------------------------------------------
# G3 — Canal sin media: prohibido afirmar que llegó una foto
# ---------------------------------------------------------------------------
# El agente decía "Recibí tu foto de receta" cuando NO llegó ninguna imagen
# (sin OCR y sin adjunto). Señal inyectada al prompt + veto post-generación.
MEDIA_MENCION = re.compile(
    r"\b(foto|fotos|imagen|im[áa]genes|adjunt|escane|captura)\b", re.I
)

CLAIM_RECIBIO_MEDIA = re.compile(
    r"(recib[íi]|me\s+lleg[óo]|vi|revis[ée]|ley[ée]|abrier?on?|tengo)\s+(tu|la|su|el)?\s*"
    r"(foto|imagen|adjunto|receta\s+en\s+(foto|imagen))|"
    r"(tu|la)\s+(foto|imagen)\s+(que\s+enviaste|de\s+la\s+receta|de\s+tu\s+receta)",
    re.I,
)

NO_MEDIA_ALERT = (
    "RESTRICCIÓN DEL CANAL (no la escribió el lead): este canal NO tiene "
    "media entrante en este turno. Si el lead menciona una foto, receta en "
    "imagen o adjunto, NO afirmes haberla recibido ni describirla: no existe. "
    "Pídele que escriba los medicamentos como TEXTO (nombre + miligramaje) y "
    "ayúdalo con el catálogo."
)

TPL_SIN_MEDIA = (
    "No me llegó ninguna imagen por este canal — aquí solo me llega texto. "
    "Escríbeme el nombre de los medicamentos (con el miligramaje si lo "
    "tienes) y te digo al instante qué tenemos disponible."
)

# ---------------------------------------------------------------------------
# G4 — Hostilidad: acusación de fraude → escalado inmediato
# ---------------------------------------------------------------------------
# hostility.py ya garantiza handoff al 3er strike. La acusación de fraude
# ("estafa", "ladrón") es el caso donde UN solo mensaje ya rompe la confianza
# y el bench mostró al agente "vendiendo" o negándose sin escalar.
ACUSACION_FRAUDE = re.compile(
    r"\b(estafa|estafador(?:es)?|est[áa]n\s+estafando|ladron(?:es|a|as)?|fraude|"
    r"me\s+quieren\s+robar|es\s+una\s+mentira|puro\s+humo)\b",
    re.I,
)

# Cierre constante de G4: NO lo redacta el LLM (en el bench generaba cierres
# débiles tipo "lamento tu experiencia"). Plantilla fija + handoff inmediato.
TPL_CIERRE_G4 = (
    "Entiendo tu molestia y la tomo en serio. Esto no lo puedo resolver yo: "
    "te estoy comunicando ahora mismo con un asistente humano de la farmacia "
    "para que revise tu caso personalmente. Se comunicará contigo por este "
    "mismo chat."
)


# ---------------------------------------------------------------------------
# G1-ext — Superlativos de precio sin respaldo de catálogo
# ---------------------------------------------------------------------------
# El agente afirma que algo "es el más económico", "el más barato" o "el
# genérico de X" cuando last_products está vacío: no hay datos del catálogo
# que respalden esa comparación. Veto con la misma plantilla de G1.
SUPERLATIVO_PRECIO = re.compile(
    r"\b(m[aá]s\s+econ[oó]mic[oa]|m[aá]s\s+barat[oa]|m[aá]s\s+accesible|"
    r"opci[oó]n\s+m[aá]s\s+barat[oa]|precio\s+m[aá]s\s+baj[oa]|"
    r"el\s+gen[eé]rico\s+de|gen[eé]rico\s+m[aá]s\s+econ[oó]mic[oa])\b",
    re.I,
)


def cita_superlativo_precio(texto: str) -> bool:
    """G1-ext: el texto del AGENTE usa un superlativo de precio sin respaldo."""
    return bool(texto) and bool(SUPERLATIVO_PRECIO.search(texto))


# ---------------------------------------------------------------------------
# G5 — Afirmación de composición/genérico sin respaldo de catálogo
# ---------------------------------------------------------------------------
# El agente afirma el principio activo, la composición o la equivalencia
# genérica de un medicamento cuando NO hay resultados de tool que lo
# respalden. Hallazgo del Laboratorio: Daflon con "principio activo ramiprilo"
# o Daflon con "el genérico más económico del...". Plantilla que no afirma
# nada y ofrece buscar en el catálogo.
# El detector corre sobre el texto NORMALIZADO (minúsculas y SIN tildes): los
# modelos escriben "composicion" y "composición" indistintamente (D-B3). Por eso
# los patrones van sin acentos.
AFIRMA_COMPOSICION = re.compile(
    r"principio\s+activo"                          # su/el/mismo principio activo
    r"|ingrediente\s+activo"
    r"|generico\s+(de|del|es|equivalente)"         # es el genérico de/del, el genérico es
    r"|equivalente\s+generico"
    r"|equivalente?\s+a(l|\s)"                      # es equivalente a/al, equivale a/al
    r"|equivale\s+a(l|\s)"
    r"|mismo\s+efecto"                             # tiene el mismo efecto que
    r"|se\s+(puede\s+)?reemplaza(r)?\s+por"        # se puede reemplazar por
    r"|mismo\s+medicamento"                        # es el mismo medicamento que
    r"|composicion\s+\w"                           # composición incluye/del/es (D-B3: sin \b final)
    r"|contiene\s+[a-z]{4,}"
    r"|lleva\s+[a-z]{4,}",
    re.I,
)

# Modo NO afirmativo: preguntas y negaciones/limitaciones honestas NO deben
# vetarse (D-B4). Se excluye el texto interrogativo y el que niega el hecho.
_NO_AFIRMA_COMPOSICION = re.compile(
    r"\bno\s+(tengo|puedo|se\b|quiero|hay|confirmo|estoy\s+segur)"
    r"|\bprefiero\s+no\b"
    r"|\bsin\s+(confirmar|datos)\b",
    re.I,
)

# Campos del payload del producto que exponen composición/principio activo. El
# catálogo ACTUAL (producto, nombre, precio, precioBs, presentacion, productId,
# laboratorio) NO expone ninguno → fail-closed: la afirmación nunca se respalda.
_CAMPOS_COMPOSICION = (
    "composicion", "principioactivo", "principio_activo",
    "ingredienteactivo", "ingrediente_activo", "monodroga",
    "sustanciaactiva", "sustancia_activa",
)

TPL_SIN_COMPOSICION = (
    "No tengo información confirmada sobre la composición o equivalencia "
    "genérica de ese medicamento en este momento y no quiero darte datos "
    "incorrectos. Puedo buscarlo en el catálogo para darte las opciones "
    "reales. ¿Lo buscamos?"
)


def _sin_tildes(texto: str) -> str:
    """Minúsculas y sin diacríticos (los modelos mezclan 'composicion'/'composición')."""
    t = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def afirma_composicion(texto: str) -> bool:
    """G5: el texto del AGENTE afirma —en modo AFIRMATIVO— principio activo,
    composición o equivalencia genérica. Excluye preguntas y negaciones honestas
    (D-B4) y normaliza tildes (D-B3).

    Se evalúa POR ORACIÓN: una afirmación declarativa dispara aunque OTRA oración
    del mensaje (p. ej. el pie '¿Otro medicamento?' del carrito) sea interrogativa.
    """
    if not texto:
        return False
    for oracion in re.findall(r"[^.!?\n]*[.!?\n]|[^.!?\n]+$", texto):
        if "?" in oracion or "¿" in oracion:
            continue  # interrogativa: no afirma
        norm = _sin_tildes(oracion)
        if _NO_AFIRMA_COMPOSICION.search(norm):
            continue  # negación/limitación honesta
        if AFIRMA_COMPOSICION.search(norm):
            return True
    return False


def hay_evidencia_composicion(
    products: list[dict] | None, texto: str
) -> bool:
    """G5 (D-B1) — evidencia por (afirmación, producto), FAIL-CLOSED: True solo si
    algún producto devuelto por la herramienta expone un campo de composición cuyo
    valor aparece en el texto del agente. Si el payload no expone composición (el
    catálogo de hoy no lo hace), devuelve False → la afirmación NO se respalda."""
    if not products or not texto:
        return False
    texto_norm = _sin_tildes(texto)
    for p in products:
        if not isinstance(p, dict):
            continue
        for clave, valor in p.items():
            clave_norm = _sin_tildes(str(clave)).replace(" ", "")
            if not any(c in clave_norm for c in _CAMPOS_COMPOSICION):
                continue
            if not isinstance(valor, str) or not valor.strip():
                continue
            valor_norm = _sin_tildes(valor)
            for token in re.findall(r"[a-z]{4,}", valor_norm):
                if token in texto_norm:
                    return True
    return False


# ---------------------------------------------------------------------------
# G6 — Precio, stock o principio activo directo sin respaldo de catálogo
# ---------------------------------------------------------------------------
# El agente afirma un precio con cifra concreta, una cantidad de stock ("hay
# N unidades") o la frase directa "el principio activo es X" sin haber
# obtenido resultados de buscar_medicamento en el turno actual. Complementa
# G1 (que requiere que el cliente preguntara el precio) y G5 (composición más
# amplia): G6 cubre el caso donde el LLM inventa datos sin que el usuario
# haya preguntado explícitamente.
AFIRMA_DATO_CATALOGO = re.compile(
    r"\$\s*\d+(?:[.,]\d{1,2})?"                                  # $5 / $12.50 / $12,50
    r"|\bBs\.?\s*\d+"                                            # Bs 50 / Bs. 50
    r"|\d+\s*(?:d[oó]lares?|bol[íi]vares?)\b"                   # 50 bolívares / 5 dólares
    r"|\b(?:hay|tenemos|tengo|disponemos\s+de)\s+\d+\s+"
    r"(?:unidades?|cajas?|piezas?|tabletas?|comprimidos?|frascos?|ampollas?)\b"  # stock
    r"|\bel\s+principio\s+activo\s+es\s+\w+"                    # el principio activo es ramiprilo
    r"|\bsu\s+principio\s+activo\s+es\s+\w+",                   # su principio activo es X
    re.I,
)

# Cierre constante de G6: sustituye la alucinación de datos de producto por
# una respuesta que reconoce la limitación y propone buscar en el catálogo.
TPL_VERIFICACION_CATALOGO = (
    "No tengo información confirmada sobre ese medicamento en este momento "
    "y no quiero darte datos incorrectos. Para precio, disponibilidad o "
    "composición exacta, déjame buscarlo en el catálogo. "
    "¿Cuál es el medicamento que buscas?"
)


# Cuando el cliente acaba de ver una lista, la frase que pide "¿cuál es el medicamento?"
# no tiene sentido: hay que preguntar POR LA OPCIÓN. Medido en la prueba en vivo con
# tráfico real (provider 05, 2026-11-07): a "Este", "De ese" y "Si tienen disponibilidad."
# el agente contestó "¿Cuál es el medicamento que buscas?".
TPL_VERIFICACION_CATALOGO_CON_LISTA = (
    "Para no darte un precio equivocado, dime cuál de las opciones que te mostré "
    "quieres (la 1, la 2…) y te lo confirmo."
)


def afirma_dato_catalogo(texto: str) -> bool:
    """G6: el texto del AGENTE afirma precio con dígitos, cantidad de stock o principio activo directo."""
    return bool(texto) and bool(AFIRMA_DATO_CATALOGO.search(texto))


# ---------------------------------------------------------------------------
# G7 — Eco: el agente repite casi literalmente el mensaje del cliente
# ---------------------------------------------------------------------------
# El LLM a veces devuelve el turno del cliente como si fuera su respuesta
# ("Perfecto, quiero 2 cajas de losartan 50 mg.") en lugar de ejecutar la
# acción (buscar en el catálogo, agregar al carrito, confirmar) o pedir el
# dato que falta. Esta guarda detecta el eco por solapamiento de tokens
# normalizado y lo reemplaza por una confirmación útil.

_ECO_MIN_TOKENS = 4  # textos con menos tokens no se evalúan (saludos cortos)

TPL_NO_ECHO = (
    "Perfecto, ya lo estoy buscando. En un momento te muestro las opciones disponibles."
)


def _normalizar_tokens(texto: str) -> list[str]:
    """Minúsculas, sin acentos, sin puntuación → lista de tokens."""
    texto = texto.lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[^\w\s]", " ", texto)
    return texto.split()


def es_echo(texto_agente: str, texto_cliente: str) -> bool:
    """G7: True si el texto del agente es casi idéntico al último mensaje del cliente.

    Normaliza ambos textos (minúsculas, sin acentos ni puntuación) y calcula
    solapamiento de tokens en ambas direcciones: si AMBAS coberturas superan
    el 70 % el agente está repitiendo al cliente en vez de actuar.
    """
    if not texto_agente or not texto_cliente:
        return False
    ta = _normalizar_tokens(texto_agente)
    tc = _normalizar_tokens(texto_cliente)
    if len(ta) < _ECO_MIN_TOKENS or len(tc) < _ECO_MIN_TOKENS:
        return False
    if ta == tc:
        return True
    set_a, set_c = set(ta), set(tc)
    inter = len(set_a & set_c)
    overlap_a = inter / len(set_a) if set_a else 0
    overlap_c = inter / len(set_c) if set_c else 0
    return overlap_a > 0.70 and overlap_c > 0.70


def pide_precio_sin_dato(texto: str) -> bool:
    """G1: el cliente pide un precio (la llamada del guardián decide si hay
    respaldo del catálogo)."""
    return bool(texto) and bool(PIDE_PRECIO.search(texto))


def intencion_fuera_kb(texto: str) -> bool:
    """G2: el turno del cliente trata políticas de negocio fuera del KB."""
    return bool(texto) and bool(FUERA_KB.search(texto))


def menciona_media(texto: str) -> bool:
    """G3: el mensaje del cliente habla de fotos/adjuntos."""
    return bool(texto) and bool(MEDIA_MENCION.search(texto))


def afirma_recibir_media(texto: str) -> bool:
    """G3: el texto del AGENTE afirma que recibió/leyó una imagen."""
    return bool(texto) and bool(CLAIM_RECIBIO_MEDIA.search(texto))


def acusacion_fraude(texto: str) -> bool:
    """G4: acusación de fraude/robo dirigida al negocio."""
    return bool(texto) and bool(ACUSACION_FRAUDE.search(texto))


# ---------------------------------------------------------------------------
# G8 — Placeholder sin ejecutar: el agente escribe la plantilla en vez del dato
# ---------------------------------------------------------------------------
# El LLM puede "imitar" el formato de una tool-call escribiendo literalmente
# '[inserta información del producto desde buscar_medicamento]' en su respuesta
# en vez de LLAMAR la herramienta. El cliente recibe el esqueleto de una
# plantilla interna — fallo de protocolo grave detectado en QA 'Errores y
# modismos'. Solo se activa cuando el contenido del corchete parece instrucción
# (verbo infinitivo en español o nombre de herramienta); los corchetes
# legítimos (unidades, referencias) no disparan la guarda.
_TPL_BRACKET = re.compile(r"\[([^\]]{3,})\]")

_VERBOS_INSTRUCCION = re.compile(
    r"^(inserta|agrega|completa|coloca|escribe|pon|incluye|a[ñn]ade|describe|"
    r"muestra|lista|copia|usa|utiliza|indica|especifica)\b",
    re.I,
)

_NOMBRES_HERRAMIENTA = re.compile(
    r"\b(buscar_medicamento|agregar_al_carrito|ver_carrito|finalizar_pedido|"
    r"update_ficha|propose_slots|book_session|reschedule_session|route_out|"
    r"handoff|sugerir_generico)\b",
    re.I,
)

TPL_BUSQUEDA_HONESTA = (
    "Tuve un problema al completar la búsqueda y no quiero darte información "
    "incompleta. ¿Me repites el nombre del medicamento y lo busco ahora mismo?"
)


def tiene_placeholder(texto: str) -> bool:
    """G8: True si el texto del agente contiene un placeholder de instrucción entre corchetes.

    Activa solo cuando el contenido entre corchetes empieza con un verbo
    infinitivo de instrucción en español o contiene un nombre de herramienta.
    Cubre explícitamente '[inserta...' (caso QA 'Errores y modismos').
    """
    if not texto:
        return False
    if re.search(r"\[inserta\b", texto, re.I):
        return True
    for m in _TPL_BRACKET.finditer(texto):
        contenido = m.group(1).strip()
        if _VERBOS_INSTRUCCION.match(contenido) or _NOMBRES_HERRAMIENTA.search(contenido):
            return True
    return False


# ---------------------------------------------------------------------------
# G9 — Cliente pide hablar con un humano pero no hay handoff real
# ---------------------------------------------------------------------------
# El LLM a veces responde "Entendido…" o "Te comunico…" sin llamar la
# herramienta handoff, dejando al cliente sin escalado y con un cierre seco.
# El juez del Laboratorio lo marca como debio_escalar/tono. Esta guarda
# detecta la petición explícita de humano en el turno del cliente y, si el
# runtime NO tiene handoff_reason asignado, reemplaza el texto por plantilla
# constante y fuerza el handoff de forma determinista.
PIDE_HUMANO = re.compile(
    r"\b(hablar?\s+con\s+(un[ao]?\s+)?(humano|persona|asesor|agente|representante|ejecutivo)|"
    r"com[uú]nicarme?\s+con\s+(un[ao]?\s+)?(humano|persona|asesor|agente)|"
    r"p[aá]same?\s+(con\s+)?(un[ao]?\s+)?(humano|persona|asesor|agente)|"
    r"quiero\s+(un[ao]?\s+)?(humano|persona|asesor|agente)\b|"
    r"atenci[oó]n\s+humana|"
    r"hablar?\s+con\s+alguien|"
    r"necesito\s+(un[ao]?\s+)?(humano|persona|asesor|agente)|"
    r"con\s+una?\s+(persona|asesor|agente)\s+real|"
    r"quiero\s+que\s+me\s+atiendan?)\b",
    re.I,
)

# Cierre constante de G9: NO lo redacta el LLM (en el bench generaba cierres
# secos tipo "Entendido" sin escalar). Plantilla fija + handoff garantizado.
TPL_CIERRE_ESCALADO = (
    "Claro, con gusto te paso con un asesor de la farmacia para que te ayude "
    "personalmente. En un momento alguien del equipo se comunicará contigo "
    "por este mismo chat. ¡Estamos para servirte!"
)


def pide_humano(texto: str) -> bool:
    """G9: el turno del cliente solicita explícitamente hablar con un humano."""
    return bool(texto) and bool(PIDE_HUMANO.search(texto))


# ---------------------------------------------------------------------------
# G14 — Consulta clínica → derivación humana fail-closed (Paso 8, UC-08)
# ---------------------------------------------------------------------------
# El cliente hace una consulta de CONSEJO clínico (diagnóstico, dosis,
# interacción, contraindicación, embarazo/lactancia, pediatría, reacción
# adversa, síntoma de alarma o sustitución individualizada). El bot NO
# responde clínicamente: no diagnostica, no prescribe, no ajusta dosis ni
# sustituye tratamiento (PLAN §7 "Seguridad clínica", ADR-0006). La única
# salida segura es NO aconsejar y derivar a un humano.
#
# La DETECCIÓN vive en app/application/clinical_handoff.clasificar (capa de
# aplicación, dirección de dependencias correcta); aquí solo viven las
# PLANTILLAS constantes de respuesta, junto al resto de guards (el texto
# fail-closed jamás lo redacta el LLM). turn.py cablea detección + handoff.

# Mensaje de RECEPCIÓN autorizado (estado SOLICITADO): confirma que un humano
# del equipo atenderá la consulta clínica. NO da ningún consejo clínico.
TPL_DERIVACION_CLINICA = (
    "Para cuidar tu salud, esta consulta la debe atender el equipo "
    "farmacéutico de forma personal: no puedo dar indicaciones médicas, de "
    "dosis ni de tratamiento por este medio. Ya la estoy derivando a un "
    "profesional de la farmacia, que te responderá por este mismo chat. Si "
    "sientes un malestar grave o urgente, acude de inmediato a un centro de "
    "salud. 🙏"
)

# Mensaje de ESTADO autorizado (derivación ya ABIERTA): el cliente insiste y el
# caso aún espera al humano. Solo se repite que el equipo lo atenderá; NO se
# reanuda consejo clínico mientras el caso está abierto (PLAN §8 punto 6).
TPL_DERIVACION_CLINICA_EN_CURSO = (
    "Tu consulta ya está en manos del equipo farmacéutico y te responderán por "
    "este mismo chat en cuanto puedan. No puedo adelantarte indicaciones "
    "médicas, de dosis ni de tratamiento. Si es una urgencia, acude a un "
    "centro de salud. 🙏"
)


# ---------------------------------------------------------------------------
# G10 — Composición/uso farmacológico o promesa de búsqueda sin respaldo
# ---------------------------------------------------------------------------
# El agente afirma la clase terapéutica o el uso de un medicamento ('es un
# antiinflamatorio AINE', 'sirve para dolores de cabeza') o promete una
# búsqueda futura ('voy a buscar', 'déjame verificar') sin haber llamado
# buscar_medicamento en el turno. Hallazgo del Laboratorio (caso
# 'pregunton_precios'): el agente describía composición/uso del Daflón 500 sin
# dato de herramienta. G5/G6 no cubren clases terapéuticas amplias ni
# promesas de búsqueda futura.
#
# FALSO POSITIVO principal: NO debe activarse cuando el turno SÍ llamó
# buscar_medicamento (runtime.consulted_catalog=True). La condición de turno se
# evalúa en turn.py; aquí solo se define la detección en el texto del agente.
G10_COMPOSICION = re.compile(
    r"\b(AINE[s]?|antiinflamatorio[s]?|antibi[oó]tico[s]?|"
    r"analg[eé]sico[s]?|antipir[eé]tico[s]?|antihipertensivo[s]?|"
    r"antidiab[eé]tico[s]?|antihistam[íi]nico[s]?|diur[eé]tico[s]?|"
    r"vasodilatador(?:es)?|anticoagulante[s]?|antif[úu]ngico[s]?|"
    r"broncodilatador(?:es)?|corticoesteroide[s]?|"
    r"es\s+un\s+(?:medicamento|f[aá]rmaco)|"
    r"sirve\s+para|se\s+utiliza\s+para|se\s+usa\s+para|"
    r"para\s+el\s+tratamiento\s+de|indicado\s+para)\b",
    re.I,
)

G10_PROMESA_BUSQUEDA = re.compile(
    r"\b(?:voy\s+a\s+(?:buscar|verificar|consultar|revisar|checar)|"
    r"d[eé]jame\s+(?:buscar|verificar|consultar|revisar|checar|ver)|"
    r"busco\s+y\b|"
    r"en\s+un\s+momento\s+(?:busco|verifico|consulto|te\s+digo)|"
    r"estoy\s+buscando|te\s+busco\b|"
    r"buscar[eé]|verificar[eé]|consultar[eé]|"
    r"voy\s+a\s+checar|me\s+fijo\s+y\b|"
    r"ahora\s+mismo\s+(?:busco|verifico|consulto))\b",
    re.I,
)

TPL_G10_COMPOSICION = (
    "No tengo información médica confiable sobre ese medicamento. Para datos "
    "de composición o uso, te recomiendo consultarlo con el equipo de la "
    "farmacia. ¿Te ayudo con algo más?"
)

TPL_G10_PROMESA = (
    "No tengo esa información disponible en este momento. ¿Te ayudo con tu "
    "pedido o deseas que tome tu mensaje para el equipo?"
)


def afirma_composicion_farmacologica(texto: str) -> bool:
    """G10a: el texto del AGENTE afirma clase terapéutica o uso de un medicamento."""
    return bool(texto) and bool(G10_COMPOSICION.search(texto))


def promete_busqueda_sin_accion(texto: str) -> bool:
    """G10b: el texto del AGENTE promete una búsqueda futura sin haber ejecutado herramienta."""
    return bool(texto) and bool(G10_PROMESA_BUSQUEDA.search(texto))


# ---------------------------------------------------------------------------
# G11 — Promesa de escalado sin llamar handoff
# ---------------------------------------------------------------------------
# El LLM afirma que "te comunico con un asesor", "te paso con alguien del
# equipo" o "un asesor te contactará" sin haber llamado la herramienta
# handoff. El cliente queda esperando un contacto que nunca ocurrirá.
# G9 solo cubre cuando el CLIENTE pide humano explícitamente; G11 cubre
# cuando el AGENTE promete por iniciativa propia la transferencia/contacto.
PROMESA_ESCALADO = re.compile(
    r"te\s+comunico\s+con\s+(un[ao]?\s+)?(asesor|asistente|agente|humano|persona|alguien|el\s+equipo)"
    r"|te\s+paso\s+con\s+(un[ao]?\s+)?(asesor|asistente|agente|humano|persona|alguien|el\s+equipo)"
    r"|te\s+transfiero\b"
    r"|te\s+voy\s+a\s+transferir\b"
    r"|transferir[eé]\s+(con|a)\b"
    r"|voy\s+a\s+comunicarte\s+con\s+(un[ao]?\s+)?(asesor|asistente|agente|humano|persona|alguien|el\s+equipo)"
    r"|alguien\s+(del\s+equipo\s+)?(te\s+contactar[aá]|se\s+comunicar[aá]\s+contigo|te\s+escribir[aá]|te\s+atiend\w+|te\s+llam[aá])\b"
    r"|un[ao]?\s+asesor\s+(te\s+contactar[aá]|te\s+llam[aá]|te\s+escribir[aá]|te\s+atiend\w+)\b"
    r"|el\s+equipo\s+(te\s+contactar[aá]|te\s+atender[aá]|se\s+comunicar[aá])\b"
    r"|(un[ao]?\s+)?(asesor|asistente|agente|humano|persona)\s+te\s+contactar[aá]\b"
    r"|(un[ao]?\s+)?(asesor|asistente|agente|humano|persona)\s+se\s+comunicar[aá]\s+contigo\b"
    # Promesas IMPERSONALES sin sujeto humano explícito (caso real del lab:
    # "Esa petición la manejarán en breve") — 3ª persona futura de verbos de
    # gestión, sin agente definido: el cliente queda esperando igual.
    r"|(?:la|lo|los|las|se)\s+(?:manejar[aá]n?|gestionar[aá]n?|revisar[aá]n?|atiender[aá]n?|encargar[aá]n?|procesar[aá]n?)\b",
    re.I,
)

# Plantilla G11: confirma que un asesor seguirá el caso por este chat.
# Redactada sin frases que disparen PROMESA_ESCALADO (sin "te paso con",
# "te comunico con", "alguien ... se comunicará contigo").
TPL_G11_ESCALADO = (
    "Con gusto, un asesor de la farmacia tomará tu caso y se pondrá en "
    "contacto contigo por este mismo chat. ¡Estamos para servirte!"
)


def promete_escalado(texto: str) -> bool:
    """G11: el texto del AGENTE promete conectar/transferir a un asesor o humano sin handoff real."""
    return bool(texto) and bool(PROMESA_ESCALADO.search(texto))


# ---------------------------------------------------------------------------
# G12 — Tool-call JSON cruda filtrada al texto del cliente
# ---------------------------------------------------------------------------
# El LLM a veces emite la llamada a herramienta como texto plano en vez de
# ejecutarla: el cliente recibe '{"name":"sugerir_generico","arguments":{...}}'.
# Caso real del Laboratorio (fase 10, G11b-A, escenario pregunton_precios).
# Familiar del placeholder G8, pero G8 solo cubre corchetes '[inserta...]', no
# JSON. Detecta cualquier objeto JSON cuyo contenido mencione un campo "name"
# con nombre de herramienta (o cualquier "name": seguido de identificador con
# forma de tool snake_case entre llaves en el texto visible).
_TOOLCALL_JSON = re.compile(
    r"\{[^{}]{0,200}?[\"']name[\"']\s*:\s*[\"'][a-z_]+[\"']", re.I,
)

# Plantilla G12: disculpa honesta + reintento, sin datos inventados ni
# promesas de escalado (no debe disparar G11 ni G10).
TPL_G12_TOOLCALL = (
    "Disculpa, tuve un problema técnico al procesar tu mensaje. "
    "¿Me lo repites para ayudarte ahora mismo?"
)


def contiene_toolcall_json(texto: str) -> bool:
    """G12: True si el texto final contiene una tool-call JSON cruda filtrada."""
    if not texto:
        return False
    if _TOOLCALL_JSON.search(texto):
        return True
    # Cobertura adicional: nombre de herramienta conocido dentro de llaves.
    for m in re.finditer(r"\{([^{}]*)\}", texto, re.S):
        if _NOMBRES_HERRAMIENTA.search(m.group(1)) and '"name"' in m.group(1).lower():
            return True
    return False


# ---------------------------------------------------------------------------
# G13 — Afirmación de una ACCIÓN que no ocurrió
# ---------------------------------------------------------------------------
# El juez marca `alucinacion` cuando el agente afirma una acción que nunca pasó.
# Caso medido (comprador_decidido): "Listo, quedó registrado." con el carrito
# VACÍO y SIN llamada a finalizar_pedido. Rojo visible al cliente.
#
# El detector es LÉXICO por familia. El veto solo dispara cuando la frase
# aparece Y la señal de estado del turno dice que la acción NO ocurrió (la
# condición de estado se evalúa en turn.py). La frase sola NUNCA dispara.
# Familias (spec G13 §2): pedido_registrado / agregado_al_pedido /
# apartado_o_agendado. No juzga escalado (G11), ni promesas, ni precios/stock.
# Nota: los stems son PARCIALES ('registrad' → registrado/registrada), así que la
# alternativa NO lleva `\b` de cierre (rompería 'registrado'); el `\b` de apertura sí.
G13_PEDIDO_REGISTRADO = re.compile(
    r"\b(qued[oó]\s+registrad|lo\s+registr[ée]|"
    r"finalic[ée]\s+(tu|el|su)\s+pedido|cerr[ée]\s+(tu|el|su)\s+pedido|"
    r"pedido\s+(ya\s+)?(qued[oó]\s+)?(registrad|confirmad|finalizad|cerrad)|"
    r"tu\s+pedido\s+(ya\s+)?(qued[oó]|est[áa])\s+(registrad|list)|"
    r"listo,?\s+qued[oó]\s+registrad)",
    re.I,
)

G13_AGREGADO_AL_PEDIDO = re.compile(
    r"\b((ya\s+)?lo\s+(agregu[ée]|a[ñn]ad[íi])\s+(a\s+tu|al)\s+(pedido|carrito)|"
    r"(ya\s+)?(agregu[ée]|a[ñn]ad[íi])\s+\w+\s+(a\s+tu|al)\s+(pedido|carrito)|"
    r"ya\s+est[áa]\s+en\s+tu\s+pedido)",
    re.I,
)

G13_APARTADO_O_AGENDADO = re.compile(
    r"\b((te\s+)?lo\s+(apart[ée]|reserv[ée])|"
    r"(te\s+)?(lo\s+)?agend[ée]|queda\s+(apartad|reservad)|"
    r"reservad[oa]\s+a\s+tu\s+nombre)",
    re.I,
)

# Guarda de negación: no dispara si el texto NIEGA el hecho (spec §2.1).
G13_NEGACION = re.compile(
    r"\bno\s+(puedo|se\s+puede|hay|qued[oó]|logr[ée]|alcanc[ée]|pude)\b",
    re.I,
)

# Plantilla constante (spec §2.4): no afirma nada nuevo, ofrece dejarlo listo.
TPL_G13_ACCION = (
    "No puedo confirmar que eso quedara registrado: en este chat no hay un "
    "pedido cerrado. Si quieres, lo dejamos listo ahora — dime el medicamento "
    "y cuántas cajas."
)


def familia_accion_afirmada(texto: str) -> str | None:
    """G13: devuelve la familia de acción que el AGENTE afirma —'pedido',
    'agregado' o 'apartado'— o None. Con guarda de negación: una frase que
    NIEGA el hecho ('no puedo cerrar el pedido') no cuenta. La señal de estado
    (si la acción ocurrió) la evalúa turn.py; aquí solo está la detección léxica.
    """
    if not texto:
        return None
    if G13_NEGACION.search(texto):
        return None
    if G13_PEDIDO_REGISTRADO.search(texto):
        return "pedido"
    if G13_AGREGADO_AL_PEDIDO.search(texto):
        return "agregado"
    if G13_APARTADO_O_AGENDADO.search(texto):
        return "apartado"
    return None
