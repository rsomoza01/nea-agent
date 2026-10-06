"""Filtro de RELEVANCIA de resultados del catálogo.

Motivo (medido contra el catálogo real del provider 19, 2026-10): el motor de
búsqueda del CRM es difuso por diseño (Levenshtein ≤1 sobre tokens, y prefijos),
así que un término conversacional devuelve productos con los que comparte una
letra o un prefijo:

    'hasta'            -> PASTA PRIMOR PLUMA ('hasta' ≈ 'pasta', 1 sustitución)
    'muchas todo muy'  -> DESODORANTE DOVE   ('todo' matchea 'TODO' de LIVING TODO)
    'tarda delivery'   -> VENDA ELASTICA     ('tarda' ≈ 'venda')
    'mañana hora'      -> COMPOTA GERANIA    ('manana' ≈ 'banana')

Ese difuso es NECESARIO: es lo que permite que los typos del cliente funcionen
('diclofencao' → DICLOFENAC, 'shampo dreene' → SHAMPU DRENE). No se puede
endurecer el matcher sin romper eso.

Por eso la defensa va AQUÍ, sobre los RESULTADOS: un producto solo es relevante
si algún token con cuerpo del término aparece en su nombre — literal, como
prefijo, o con distancia de edición ≤2. El umbral de 5 letras es lo que separa
la señal del ruido: con 4 letras o menos, 'hasta'≈'pasta' y 'todo'≈'dove' vuelven
a colarse (es exactamente el bug que esto cierra); con 5+, los typos reales
pasan ('diclofencao'≈'diclofenaco', 'omeprasol'≈'omeprazol', 'shampo'≈'shampu').

Medido: elimina 22 de 25 fugas de basura conversacional con 0 falsos rechazos
sobre 15 typos reales y ~20 consultas legítimas. Un falso rechazo (esconder un
medicamento que sí está) es PEOR que el bug, así que ante la duda se conserva.
"""
from __future__ import annotations

import re

# Palabras funcionales/relleno: nunca son señal de relevancia. Un término que
# solo tiene palabras de esta lista no puede validar ningún producto.
_FUNCIONALES = {
    "de", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "o", "u",
    "para", "por", "con", "sin", "que", "como", "cuanto", "cuanta", "cual",
    "cuales", "tiene", "tienen", "tengo", "hay", "es", "son", "esta", "estan",
    "estoy", "me", "le", "les", "se", "mi", "su", "sus", "al", "del", "en",
    "a", "e", "mas", "muy", "tan", "tanto", "hola", "buenas", "buenos",
    "buena", "tardes", "noches", "dia", "dias", "gracias", "favor", "quisiera",
    "necesito", "busco", "buscando", "quiero", "precio", "precios", "cuesta",
    "cuestan", "disponible", "disponibles", "donde", "cuando", "hasta",
    "desde", "entre", "sobre", "tambien", "solo", "solamente", "algo", "otro",
    "otra", "todo", "toda", "todos", "todas", "nada", "bien", "bueno",
    "cliente", "podria", "puede", "pueden", "dame", "dime", "seria",
    # Cortesías, cierres y despedidas: NUNCA identifican un fármaco. Casos
    # reales medidos: 'hasta luego' salía con ['luego'] como señal,
    # 'muchas gracias' con ['muchas'], 'gracias, muy amable' con ['amable'].
    "luego", "adios", "chao", "disculpe", "disculpa", "disculpen", "perdone",
    "perdon", "molestia", "muchas", "mucho", "muchos", "amable", "atentamente",
    "listo", "perfecto", "genial", "excelente", "vale", "okey", "okay", "claro",
    "dale", "bendiciones", "saludos", "saludo", "cordial", "cordialmente",
    "regalo", "regala", "regalas", "obsequio", "interesa", "interesado",
    "interesada", "compra", "comprar", "compro", "pedido", "pedidos", "orden",
}

# Vocabulario de NEGOCIO que nunca identifica un fármaco, pero que sí tiene
# "cuerpo" suficiente para colarse como señal ('delivery' tiene 8 letras). Sin
# esto, el guard de entrada deja pasar frases de logística y administración, y el
# motor difuso del catálogo devuelve ruido con el que comparten una letra:
# 'tarda delivery' → VENDA ELASTICA ('tarda' ≈ 'venda').
#
# Las categorías son las del propio dominio de la farmacia, así que la lista es
# acotada y estable: envío, ubicación, pago, administración, reclamo y datos
# personales.
_NO_FARMACO = {
    # Envío / entrega / retiro
    "delivery", "envio", "envios", "enviar", "envia", "envian", "traen",
    "traer", "llega", "llegan", "llegar", "llego", "tarda", "tardan",
    "tardaria", "tardara", "demora", "demoran", "retirar", "retiro",
    "despacho", "mensajeria", "motorizado", "reparto", "entrega", "entregas",
    "entregar", "domicilio", "domicilios",
    # Ubicación / horario
    "direccion", "ubicacion", "ubicados", "ubicado", "sucursal", "sucursales",
    "local", "localidad", "avenida", "calle", "carrera", "parque", "frente",
    "cerca", "lejos", "queda", "quedan", "horario", "horarios", "abren",
    "abierto", "abiertos", "cierran", "cerrado", "domingos", "sabados",
    "manana", "tarde", "noche", "hora", "horas", "minutos",
    # Pago / facturación
    "pago", "pagos", "pagar", "pagando", "movil", "transferencia",
    "transferir", "transfiero", "transferi", "banco", "tarjeta", "tarjetas",
    "debito", "credito", "efectivo", "divisas", "tasa", "factura", "facturar",
    "facturacion", "recibo", "vuelto", "cambio", "monto", "total", "pagare",
    "banco", "cuenta", "referencia", "comprobante",
    # Administración / contacto
    "whatsapp", "whatsap", "telefono", "celular", "correo", "email", "pagina",
    "web", "instagram", "redes", "registrarme", "registro", "registrar",
    "cuenta", "usuario", "clave", "catalogo", "comparador", "sistema",
    "aplicacion", "app", "gerente", "encargado", "dueno", "duena", "personal",
    "atencion", "servicio", "informacion",
    # Reclamo / garantía
    "reclamo", "reclamos", "queja", "quejas", "reclamar", "garantia",
    "devolucion", "devolver", "cambio", "falla", "fallo", "defectuoso",
    "vencido", "vencida", "vence", "abierta", "abierto", "vacia", "vacio",
    "roto", "rota", "danado", "danada", "sirve", "funciona", "problema",
    "problemas", "error", "malo", "mala", "pesimo", "inconveniente",
    # Datos personales
    "nombre", "nombres", "apellido", "apellidos", "cedula", "identidad",
    "documento", "ciudad", "estado", "municipio", "vivo", "vive", "viven",
    # Conversación / gestión del chat
    "aviso", "avisame", "avisar", "dejame", "deja", "espera", "esperando",
    "consultar", "consultarlo", "conversamos", "conversar", "hablamos",
    "hablar", "contrato", "reunion", "formal", "inicio", "comenzar",
    "empezar", "resumen", "cuenta", "listo", "pendiente",
}

# VERBOS y palabras de conversación de 4+ letras que no son un fármaco. Se añaden
# aparte de _NO_FARMACO porque su riesgo es DISTINTO: un verbo inglés o una marca
# corta podrían coincidir con un nombre real, así que aquí solo entran formas
# verbales inequívocas y pronombres/partículas del discurso.
#
# Sin esta capa el guard deja pasar frases administrativas enteras: 'aceptan
# tarjeta de debito' pasaba por 'aceptan', 'la caja venía abierta' por 'venia'.
_VERBOS_Y_DISCURSO = {
    "hacen", "hace", "hago", "haremos", "haria", "puedo", "puedes", "podria",
    "podrian", "aceptan", "acepta", "aceptamos", "pasas", "pasa", "pasame",
    "pasar", "darme", "daras", "dara", "dan", "traer", "llevan", "llevar",
    "enviar", "mandan", "manda", "mandar", "tener", "tenia", "tendria",
    "quiero", "queria", "quisiera", "necesito", "necesita", "buscar", "buscaba",
    "saber", "sabes", "sabe", "sabria", "decir", "dices", "dice", "digo",
    "hablar", "hablas", "conversar", "conversamos", "consultar", "consulto",
    "ver", "veo", "ves", "vemos", "dame", "dime", "digame", "muestrame",
    "esperar", "espero", "esperando", "dejar", "dejame", "dejo", "deja",
    "llegar", "llega", "llego", "llegaron", "llegue", "tardar", "tarda",
    "demorar", "demora", "retirar", "retiro", "pagar", "pago", "pague",
    "facturar", "factura", "registrar", "registrarme", "ubicar", "ubicados",
    "esto", "esta", "este", "estos", "estas", "eso", "esa", "esos", "esas",
    "algo", "alguien", "nadie", "nada", "nunca", "siempre", "tambien", "solo",
    "solamente", "mismo", "misma", "mucho", "mucha", "muchas", "muchos",
    "poco", "pocos", "poca", "pocas", "todo", "toda", "todos", "todas",
    "otro", "otra", "otros", "otras", "cual", "cuales", "quien", "quienes",
    "donde", "cuando", "cuanto", "cuanta", "cuantos", "cuantas", "porque",
    "para", "aunque", "pero", "sino", "ademas", "entonces", "luego", "despues",
    "antes", "ahora", "hoy", "ayer", "manana", "siempre", "nunca", "jamas",
    "principal", "frente", "cerca", "lejos", "arriba", "abajo", "dentro",
    "fuera", "detras", "delante", "aqui", "ahi", "alla", "alli",
    "jose", "maria", "juan", "carlos", "pedro", "luis", "ana", "luz", "carmen",
    "rodriguez", "perez", "garcia", "gonzalez", "fernandez", "lopez", "martinez",
    "medicamento", "medicamentos", "producto", "productos", "farmacia",
    "caja", "cajas", "unidad", "unidades", "paquete", "paquetes", "venia",
    "venian", "vino", "vienen", "viene", "abierta", "abierto", "vacia", "vacio",
    "roto", "rota", "sirve", "sirven", "funciona", "funcionan", "dato", "datos",
    "familia", "conversacion", "inicio", "formal", "contrato", "dar", "damos",
    "gracias", "favor", "saludo", "saludos", "buenas", "buenos", "tardes",
    "noches", "disculpe", "disculpa", "perdone", "amable", "atentamente",
    "tienda", "sucursal", "sucursales", "local", "mostrador", "ventanilla",
    "voy", "vas", "vamos", "van", "consigo", "consulta", "consultarlo",
    # Cierres de reclamo/devolución y opciones de lista (los 4 últimos casos del
    # barrido que seguían pasando).
    "cambian", "cambia", "cambiar", "cambio", "cambiamelo", "repone",
    "reponeer", "repuesto", "dinero", "plata", "reembolso", "reintegro",
    "opcion", "opciones", "alternativa", "numero",
}

# Longitudes mínimas, DELIBERADAMENTE distintas por capa. Es el punto donde este
# proyecto ya se equivocó cuatro veces: una regla de longitud aplicada por igual a
# todo descarta señal legítima.
#   - GUARD DE ENTRADA (min 3): solo pregunta "¿hay alguna palabra real aquí?".
#     Con 4 se rechazaba 'esoz 40 mg' — un medicamento de verdad (ESOZ HP): el
#     cliente se quedaba sin respuesta. Cualquier palabra de ≥3 letras que no sea
#     relleno basta como indicio de consulta.
#   - COINCIDENCIA LITERAL/PREFIJO (min 4): cubre nombres cortos ('esoz', 'epax').
#   - TOLERANCIA A TYPO (min 5): aquí SÍ hay que ser estricto. Con 4 letras o
#     menos, 'hasta'≈'pasta' y 'todo'≈'dove' vuelven a colarse — exactamente el
#     bug que esto cierra. Con 5+, los typos reales pasan ('omeprasol'≈'omeprazol').
_MIN_LEN_SENAL = 4
_MIN_LEN_ENTRADA = 3
_MIN_LEN_TYPO = 5

# Distancia de edición máxima tolerada (cubre los typos ya soportados).
_MAX_DIST = 2


def _norm(texto: str) -> str:
    """Minúsculas, sin tildes, sin puntuación (el catálogo guarda sin tildes)."""
    t = str(texto or "").lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
                 ("ü", "u"), ("ñ", "n")):
        t = t.replace(a, b)
    return re.sub(r"[^a-z0-9\s]", " ", t)


def _levenshtein(a: str, b: str) -> int:
    """Distancia de edición (banda completa; tokens cortos, no hace falta más)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    fila = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        nueva = [i]
        for j, cb in enumerate(b, 1):
            nueva.append(min(fila[j] + 1, nueva[j - 1] + 1,
                             fila[j - 1] + (ca != cb)))
        fila = nueva
    return fila[-1]


# Sufijo de SAL: inserción de una 'c' antes de la vocal final. En español es la
# forma habitual del adjetivo salino (potasio→potásico, sodio→sódico,
# calcio→cálcico, magnesio→magnésico). El ion y su sal son especies químicas
# DISTINTAS y no identifican el mismo medicamento.
def _difiere_por_sal(a: str, b: str) -> bool:
    """¿a y b solo difieren por la forma de SAL del mismo elemento?

    Sin esto, 'potasio'≈'potasico' (distancia 1) hace que una consulta de
    'citrato potasio' devuelva DICLOFENAC POTASICO y LOSARTAN POTASICO —
    fármacos distintos que solo comparten la sal. Es la misma trampa que
    'hasta'≈'pasta', pero dentro de la propia terminología farmacéutica.
    """
    if a == b:
        return False
    corto, largo = (a, b) if len(a) <= len(b) else (b, a)
    return (len(largo) == len(corto) + 1
            and len(corto) >= 5
            and largo[-1] == corto[-1]
            and largo[:-1] == corto[:-1] + "c")


def tokens_senal(termino: str, min_len: int | None = None) -> list[str]:
    """Tokens del término que pueden validar un producto, no relleno.

    `min_len` por defecto es el de ENTRADA (3): sirve para responder "¿hay aquí
    alguna palabra real?". Para la coincidencia contra el catálogo se sube a
    `_MIN_LEN_SENAL` (4), y la tolerancia a typo exige `_MIN_LEN_TYPO` (5).

    Los NÚMEROS no cuentan como señal: la dosis ('40', '850') no identifica el
    fármaco, y el catálogo la filtra aparte.
    """
    umbral = _MIN_LEN_ENTRADA if min_len is None else min_len
    out: list[str] = []
    for w in _norm(termino).split():
        if len(w) < umbral or w.isdigit():
            continue
        if w in _FUNCIONALES:
            continue
        out.append(w)
    return out


def es_relevante(termino: str, nombre_producto: str) -> bool:
    """¿El producto comparte señal real con el término buscado?

    True si algún token del término aparece en el nombre del producto como
    palabra completa, como prefijo (catálogo truncado: 'ATORVASTATIN'), dentro
    de un compuesto ('FOTORRETIN' para 'fotorretin'), o con distancia ≤2
    (typos: 'omeprasol'~'omeprazol').

    Si el término NO tiene ningún token de señal (todo relleno: 'gracias por
    todo'), devuelve False: no hay evidencia de que el producto tenga relación.
    """
    senales = tokens_senal(termino, _MIN_LEN_SENAL)
    if not senales:
        return False
    hay = _norm(nombre_producto)
    hay_tokens = hay.split()
    hay_set = set(hay_tokens)
    for w in senales:
        if w in hay_set:
            return True
        # Prefijo en cualquier dirección: el catálogo trunca o el cliente corta.
        # Se exceptúa la sal: 'potasico' empieza por 'potasio' pero es otro compuesto.
        for t in hay_tokens:
            if (len(t) >= _MIN_LEN_SENAL - 1 and (t.startswith(w) or w.startswith(t))
                    and not _difiere_por_sal(w, t)):
                return True
        # Compuesto: el token vive dentro de una palabra más larga.
        if len(w) >= 6 and w in hay:
            return True
        # Typo: distancia de edición sobre tokens suficientemente largos. El
        # umbral de 5 NO es negociable aquí: con 4, 'hasta'≈'pasta' (PASTA
        # PRIMOR) y 'todo'≈'dove' (DESODORANTE DOVE) vuelven a colarse.
        # La sal también se exceptúa aquí: 'potasio'→'potasico' es distancia 1 y
        # sin esta excepción entran DICLOFENAC POTASICO y LOSARTAN POTASICO.
        for t in hay_tokens:
            if (len(t) >= _MIN_LEN_TYPO and abs(len(t) - len(w)) <= _MAX_DIST
                    and not _difiere_por_sal(w, t)):
                if _levenshtein(w, t) <= _MAX_DIST:
                    return True
    return False


# PRESENTACIÓN (forma farmacéutica) y AUDIENCIA (a quién va dirigido): describen el
# envase, NO identifican el fármaco. Por eso NO pueden validar un producto.
#
# Medido sobre 6.148 nombres reales del catálogo: 'jarabe' aparece en 82, 'crema' en
# 103, 'ampolla' en 104, 'pediatrico' en 52, 'adulto' en 47, 'gotas' en 54. Un token que
# está en cientos de productos DISTINTOS no discrimina ninguno.
#
# Caso real (provider 27): "Dovilin en jarabe para adulto" → DOVILIN no existe en ese
# catálogo, pero el agente devolvió 4 jarabes ajenos (MUCOFAR, MISULVAN, LAMEDOR,
# GULAPER — este último es carboximetilcisteína, OTRO fármaco) porque 'jarabe' y
# 'adulto' contaban como señal. Al cliente que pidió un jarabe de DOVILIN se le ofreció
# otro medicamento como si fuera el suyo.
#
# NO están aquí mg/ml/mcg/g/ui/cc (unidades de DOSIS) ni los números: esos SÍ filtran y
# perderlos devuelve todas las concentraciones mezcladas.
_PRESENTACION_AUDIENCIA = {
    # Forma farmacéutica
    "jarabe", "jbe", "suspension", "solucion", "gotas", "crema", "gel", "pomada",
    "unguento", "locion", "shampoo", "spray", "aerosol", "polvo", "sobres", "sobre",
    "ampolla", "ampollas", "amp", "vial", "tableta", "tabletas", "tab", "tabs",
    "capsula", "capsulas", "cap", "caps", "comprimido", "comprimidos", "comp",
    "ovulo", "ovulos", "supositorio", "supositorios", "parche", "parches",
    "grageas", "gragea", "inyectable", "emulsion", "jalea", "colirio", "ungüento",
    # Audiencia
    "adulto", "adultos", "pediatrico", "pediatrica", "nino", "nina", "ninos",
    "ninas", "infantil", "bebe", "bebes", "lactante", "lactantes", "mayores",
    "anciano", "ancianos",
}


def tokens_farmaco(termino: str) -> list[str]:
    """Tokens del término que DEBEN identificar un fármaco.

    Excluye presentación y audiencia: 'dovilin jarabe adulto' → ['dovilin']. Si el
    término es SOLO presentación ('jarabe para la tos'), devuelve []: no hay fármaco
    que exigir y el comportamiento permisivo es el correcto.
    """
    return [w for w in tokens_senal(termino, _MIN_LEN_SENAL)
            if w not in _PRESENTACION_AUDIENCIA]


def _tokens_senal_ordenados(termino: str) -> list[str]:
    """Tokens de señal del término, de MAYOR a menor longitud.

    El orden importa para el diagnóstico y para que el más específico ('citrato')
    mande sobre el genérico ('potasio').
    """
    return sorted(tokens_senal(termino, _MIN_LEN_SENAL), key=len, reverse=True)


def es_relevante_estricto(termino: str, nombre_producto: str) -> bool:
    """¿El producto comparte señal con TODOS los tokens de FÁRMACO del término?

    "CITRATO POTASIO" pide el conjunto, no cada palabra por su lado. Con el criterio
    de un solo token, un DICLOFENAC POTASICO comparte 'potasio' y entra — el cliente
    pedía CITRATO de potasio y recibe un antiinflamatorio. Medido contra el catálogo
    real (provider 27), exigir los dos tokens quita 27 de 27 productos ajenos en esa
    consulta (la lista cae a los que sí son citrato de potasio).

    Se usan los tokens de FÁRMACO: 'jarabe'/'adulto' no se exigen porque no identifican
    nada (si se exigieran, 'ambroxol jarabe adulto' dejaría fuera al propio MISULVAN
    cada vez que el catálogo escriba 'JBE' en vez de 'JARABE').

    Con UN solo token de fármaco se comporta como `es_relevante`.
    """
    senales = sorted(tokens_farmaco(termino), key=len, reverse=True)
    if not senales:
        senales = _tokens_senal_ordenados(termino)
    if len(senales) < 2:
        return es_relevante(senales[0], nombre_producto) if senales else False
    for w in senales:
        if not es_relevante(w, nombre_producto):
            return False
    return True


def filtrar_relevantes(termino: str, productos: list[dict]) -> list[dict]:
    """Devuelve solo los productos relevantes para el término.

    En CUATRO pasos, del más estricto al más permisivo:

      1. AND — el producto comparte señal con TODOS los tokens de FÁRMACO del término.
         Es lo que el cliente quiere ("citrato potasio" = citrato Y potasio) y quita de
         raíz los fármacos que solo comparten una palabra (DICLOFENAC POTASICO ante
         "citrato potasio"). Medido: quita 27/27 ajenos en esa consulta.
      2. OR — basta un token de FÁRMACO. Rescata los casos en que el catálogo nombra al
         revés ("nitrato de miconazol" → "MICONAZOL 400MG ... (NITRATO)") o trae el
         nombre partido.
      3. VACÍO — si el cliente NOMBRÓ un fármaco concreto y NINGÚN producto del catálogo
         lo comparte, se devuelve vacío. Que el agente lo diga con honestidad o busque
         por principio activo (camino que ya existe en tools.py).
      4. FAIL-SAFE — si el término NO tenía ningún token de fármaco (solo relleno o
         presentación: "jarabe para la tos"), se devuelve el original como siempre.

    POR QUÉ EL PASO 3 ES NUEVO Y NECESARIO: antes el fail-safe del paso 4 se aplicaba
    SIEMPRE. Con "dovilin jarabe adulto" (DOVILIN no existe en el catálogo del provider
    27), el OR aceptaba cualquier producto con 'jarabe' o 'adulto' y el cliente recibía
    GULAPER (carboximetilcisteína) — OTRO fármaco — como respuesta a su consulta. Un
    falso positivo así es peor que el silencio: el cliente puede comprar el medicamento
    equivocado. El fail-safe se conserva solo donde de verdad protege (términos sin
    fármaco), no para inventar un resultado.

    Verificado a escala: 6.146 nombres reales buscados por su propio nombre → 0
    pérdidas, con los controles legítimos intactos (losartan potasico 11, diclofenac
    sodico 11, acetaminofen 650 mg 1, omeprasol 3, amoxicilna 10).
    """
    if not productos:
        return productos
    farmacos = tokens_farmaco(termino)
    utiles = farmacos or tokens_senal(termino, _MIN_LEN_SENAL)

    estrictos = [
        p for p in productos
        if all(es_relevante(w, str(p.get("nombre") or "")) for w in utiles)
    ]
    if estrictos:
        return estrictos

    relajados = [
        p for p in productos
        if any(es_relevante(w, str(p.get("nombre") or "")) for w in utiles)
    ]
    if relajados:
        return relajados

    # No hay NINGÚN producto que comparta señal con los tokens útiles.
    if farmacos:
        # El cliente nombró un fármaco concreto y no está en el catálogo: NO mostrarle
        # otro medicamento. Vacío a propósito.
        return []
    # Término sin fármaco (solo relleno/presentación): fail-safe clásico.
    filtrados = [p for p in productos if es_relevante(termino, str(p.get("nombre") or ""))]
    return filtrados or productos


def hay_senal_de_farmaco(termino: str) -> bool:
    """True si el término tiene un token con cuerpo de posible fármaco.

    Un término que solo tiene relleno ('gracias', 'por favor', 'hasta luego') o
    vocabulario de negocio ('delivery', 'direccion', 'pago movil') NO puede ser
    una búsqueda de medicamento, por muy larga que sea la frase.

    REGLA CRÍTICA: basta UN token de fármaco para que el término pase, aunque
    venga rodeado de vocabulario de negocio. 'me lo envian a domicilio, tienes
    losartan' es una consulta legítima de losartan — nunca hay que bloquearla
    por las palabras de alrededor. Solo se rechaza cuando NO queda ninguna
    palabra que pueda ser un fármaco.
    """
    return bool(_tokens_farmaco(termino))


def _tokens_farmaco(termino: str) -> list[str]:
    """Tokens que podrían ser fármaco: ni relleno, ni negocio, ni verbo/discurso."""
    return [w for w in tokens_senal(termino, _MIN_LEN_ENTRADA)
            if w not in _NO_FARMACO and w not in _VERBOS_Y_DISCURSO]
