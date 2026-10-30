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
# VOCATIVO / CARIÑO: el cliente saluda y llama al agente "mi amor", "corazón", "cariño".
# Caso real (provider 32, 2026-10-29): "Buenas tardes!! Mi amor que precio tiene la
# nitrofurantoina de 50mg y la vitamina C" → el agente buscó 'AMOR NITROFURANTOINA 50MG',
# avisó "No tengo NITROFURANTOINA. De AMOR 50MG hay esto" y cotizó TE AMO PRESERV. 3PCS.
VOCATIVOS_R = {
    "amor", "cariño", "carino", "corazon", "corazón", "bebe", "bebé", "preciosa",
    "precioso", "linda", "lindo", "hermosa", "hermoso", "bonita", "bonito",
    "principe", "príncipe", "reina", "rey", "amiga", "amigo", "senora", "señora",
    "senor", "señor", "doctor", "doctora", "licenciada", "licenciado", "jefe", "jefa",
    "vecina", "vecino", "pana", "panita", "compatriota",
}
# INTENCIÓN DE COMPRA: no es un medicamento.
INTENCION_COMPRA_R = {
    "quiero", "queria", "quería", "quisiera", "querer", "querría", "querria",
    "deseo", "deseamos", "llevo", "llevare", "llevaré", "llevaria", "llevaría",
    "apartame", "apártame", "separame", "sepárame", "encargame", "encárgame",
}
# REFERENCIA A LA LISTA ANTERIOR: 'de la opción 9', 'la número 3'.
REFERENCIA_LISTA_R = {
    "opcion", "opción", "opciones", "opcioncita", "numerito",
}

# «¿SOLO LO TIENES POR SEPARADO?» — es una PREGUNTA sobre la respuesta anterior, no un
# producto. Caso real (provider 32, 2026-10-29): tras cotizar la caja de FIN-AL-GRIP FORTE
# como si fueran cuatro medicamentos, el cliente preguntó "No entendí?? Solo lo tienes por
# separado?" y el agente contestó "⚠️ No disponibles en el catálogo: SEPARADO" con un
# ENTERUNI al lado.
POR_SEPARADO_R = {
    "separado", "separada", "separados", "separadas", "suelto", "suelta", "sueltos",
    "sueltas", "individual", "individuales", "independiente", "independientes", "aparte",
}

# «NO ENTENDÍ» — el cliente pide una ACLARACIÓN, no un medicamento. Sin esto, "No entendí??
# Solo lo tienes por separado?" se buscaba como 'ENTENDI SEPARADO' (provider 32, 2026-10-29).
COMPRENSION_R = {
    "entendi", "entiendo", "entender", "entendido", "entendiste", "entendieron",
    "comprendi", "comprendo", "comprender", "comprendido", "comprende",
    "repite", "repites", "repetir", "repitas", "reitero", "reiteras",
    "explicame", "explicar", "explicas", "explicacion", "aclarame", "aclarar", "aclaracion",
    "confundi", "confundido", "confundida", "duda", "dudas",
}

# FELICITACIONES / DESPEDIDA DE BUENAS NOCHES: "Feliz noche, ¿el precio de la buscapina?"
# no es un medicamento llamado "FELIZ NOCHE BUSCAPINA" (provider 32, 2026-10-29).
FELICITACION_R = {
    "feliz", "felicidad", "felices", "abrazo", "abrazos", "beso", "besitos", "besos",
    "bendiciones", "bendicion", "próspero", "prospero", "prospera", "éxito", "exito",
    "exitos", "excelente", "excelentes", "lindas", "hermosos", "bonita", "bonito",
}

# SALUDOS DE TIEMPO EN SINGULAR: "buenas noches" ya estaba, el 'noche' suelto y
# 'feliz noche' no. Son la misma muletilla (provider 32, 2026-10-29).
SALUDO_TIEMPO_R = {
    "noche", "tarde", "dia", "día", "manana", "mañana", "madrugada",
    "nochecita", "medio dia", "medio dia",
}

# BENDICIONES_R AL SALUDAR: "Hola buen día, Dios le bendiga" no es una línea de productos
# (provider 32, 2026-10-30: el agente cotizó COLLAR DIOS / COLLAR PERLA FE CORAZON DIOS
# para una receta de 5 medicamentos que venía en la foto).
BENDICIONES_R = {
    "dios", "diosito", "bendiga", "bendice", "bendecir", "bendecida", "bendecido",
    "bendecidos", "jesus", "jesús", "cristo", "virgen", "amen", "amén", "salmo",
    "oracion", "oración", "bendicion", "bendiciones",
}

# «ME DAN EL PRESUPUESTO DE ESTOS MEDICAMENTOS»: el verbo de la petición y el
# demostrativo no son el nombre de nada. Sin esto quedaba 'dad estos' como término
# (provider 32, 2026-10-30).
DEMOSTRATIVOS_R = {
    "dad", "dan", "dame", "danos", "da", "den", "denme", "deme", "darme", "darnos",
    "estos", "estas", "este", "esta", "esto", "esos", "esas", "eso", "aquel",
    "aquellos", "aquellas", "aquella", "dichos", "dichas", "mencionados",
    "mencionadas", "indicados", "indicadas", "referidos", "referidas", "antelacion",
    "antelación", "sus", "mis",
}

_FUNCIONALES = {
    # RÓTULOS DEL CAMPO que el OCR devuelve pegados al contenido
    # ('nombre del medicamento fexofenadina clorhidrato calox'). Sin esto se cuelan como
    # token de fármaco: el núcleo ausente salía 'nombre'/'medicamento' y una lectura con
    # solo la marca se daba por completa. Caso real provider 32, 2026-10-27.
    "nombre", "nombres", "rotulo", "etiqueta", "descripcion", "presentacion",
    # Vocativo, intención de compra y referencia a la lista (gemelos de tools/turn).
    *VOCATIVOS_R, *INTENCION_COMPRA_R, *REFERENCIA_LISTA_R,
    *POR_SEPARADO_R,
    *COMPRENSION_R,
    *FELICITACION_R,
    *SALUDO_TIEMPO_R,
    *BENDICIONES_R,
    *DEMOSTRATIVOS_R,
    "medicamento", "medicamentos",

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
    # EL EQUIPO / EL PERSONAL: pedir el pase no nombra un producto. 'derivalo al equipo'
    # dejaba 'equipo' como término de búsqueda.
    "equipo", "staff", "asesor", "asesora", "supervisor", "encargado", "encargada",
    # FOTO/IMAGEN: pedir una foto no es pedir un producto. Buscar 'fotos' en el catálogo
    # devolvía cualquier cosa (caso real provider 32: "Me puede enviar unas fotos" →
    # "Actualmente no tenemos linternas"). Hay guard propio (`_pide_foto`).
    "foto", "fotos", "fotografia", "fotografias", "imagen", "imagenes", "selfie",
    "plaquita", "plaquitas", "tirita", "tiritas",
    "aparato", "aparatos", "equipo", "maquina", "maquinita",
    "estuche", "maletin", "cargador", "cable", "bateria", "baterias",
    "adaptador", "funda", "repuesto", "repuestos",

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

# APELATIVOS DE CORTESÍA (F14.1 / C1). Son vocativos con los que el cliente
# abre el mensaje ("mi amor, ¿cuánto sale la nitrofurantoina?") y NUNCA
# identifican un fármaco. Caso real medido: el término extraído era
# "amor nitrofurantoina 50mg vitamina" y el token 'amor' (4 letras) validó
# "TE AMO PRESERV. 3PCS" — un preservativo — como resultado relevante.
#
# Se suman a `_FUNCIONALES` (no pueden ser señal de relevancia) y turn.py los
# quita del término extraído antes de consultar el catálogo.
APELATIVOS = {
    "amor", "vida", "cielo", "reina", "reyna", "rey", "linda", "lindo",
    "hermosa", "hermoso", "bella", "bello", "preciosa", "precioso", "corazon",
    "gordita", "gordito", "mami", "papi", "mama", "papa", "mijo", "mija",
    "hermano", "hermana", "jefe", "jefa", "doctor", "doctora", "senor",
    "senora", "camarada", "amigo", "amiga", "panita", "pana", "chamo", "chama",
    "princesa", "principe", "negra", "negro", "flaco", "flaca", "socio",
    "compadre", "comadre", "madrina", "padrino", "estimado", "estimada",
    "tia", "tio", "muchacho", "muchacha", "caballero", "dama", "senorita",
}
_FUNCIONALES = _FUNCIONALES | APELATIVOS

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
    # F14.5 — formas del verbo 'decir'/'querer'/'poder' en 2ª/3ª persona que se
    # colaban como "fármaco" al partir por conectores: "quiero amoxicilina y
    # QUE ME DIGAS el precio" daba el lado derecho 'digas' y lo trataba como un
    # medicamento. Solo entran formas inequívocas (nunca un nombre comercial).
    "digas", "diga", "digan", "diras", "dira", "diria", "dirias", "decirme",
    "decirte", "mencionas", "menciona", "comentas", "comenta", "pregunto",
    "preguntas", "pregunta", "podras", "podra", "podriamos", "quieras",
    "quisieras", "tengas", "tendras", "tendra", "vendes", "vender", "venden",
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
    # Palabras GENÉRICAS del dominio que nombran la CATEGORÍA, no un fármaco.
    # Bug real (2026-10): la lista tenía 'medicamento' y 'farmacia', pero 'medicina'
    # —su sinónimo más usado en Venezuela— NO estaba, así que el cliente escribía
    # "MEDICINA" y el agente le devolvía 10 presentaciones de ÁCIDO FÓLICO como si
    # hubiera pedido algo. Faltaban también 'remedio(s)', 'medicinas', 'pastillas',
    # 'inyecciones'. Al añadir una palabra de esta familia, añade la familia completa:
    # el hueco se abre justo por el sinónimo que nadie listó.
    "medicina", "medicinas", "remedio", "remedios", "botica", "boticas",
    "pastilla", "pastillas", "pildora", "pildoras", "droga", "drogas",
    "farmaco", "farmacos", "articulo", "articulos",
    "inyeccion", "inyecciones", "ampolla", "ampollas",
    # Verbos de CONSULTA con los que el cliente pide (faltaban, y dejaban pasar
    # 'tienes medicina' / 'que medicina me recomienda' como si fueran fármacos).
    "tienes", "tiene", "tienen", "tenemos", "tengan",
    "algun", "alguna", "algunos", "algunas",
    "recomienda", "recomiendas", "recomiendan", "recomiende", "recomiendame",
    "sugiere", "sugieres", "sugieran", "sugiereme",
    "vende", "vendes", "venden", "maneja", "manejan", "manejas",
    "trabaja", "trabajan", "existe", "existen", "conseguir", "consigues",
    "tengan", "hay", "haber", "dispongan", "disponen", "dispone",
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

# LA LETRA DE LA VITAMINA ES PARTE DEL NOMBRE. Caso real (provider 32, 2026-10-29):
# "la vitamina C" se reducía a 'vitamina' (una letra se descarta por corta) y el cliente
# recibía 20 productos de Vitamina E, K, B12, colágeno… y hasta LIMPIADOR FACIAL.
_VITAMINAS = {"a", "b", "c", "d", "e", "k"}
_RE_VITAMINA_EN_TERMINO = re.compile(r"\bvitamin|\bmultivit|\bcomplejo\s+b\b", re.IGNORECASE)


def _es_letra_de_vitamina(w: str, termino: str = "") -> bool:
    """¿Es la letra PEGADA a la palabra 'vitamina' ('vitamina c', 'vit d3')?

    Tiene que ir INMEDIATAMENTE después: con solo "el término dice vitamina" se colaba la
    preposición 'a' de "¡Voy a querer una vitamina C!" como si fuera VITAMINA A.
    """
    if w not in _VITAMINAS:
        return False
    return bool(
        re.search(rf"\bvitaminas?\s*[-/+]?\s*{re.escape(w)}\b", termino or "", re.IGNORECASE)
    )


def _relevante_letra_de_vitamina(w: str, nombre: str) -> bool:
    """La letra solo vale si el producto es de ESA vitamina.

    Sin esto, 'c' casaría con cualquier 'C' del título (VITAMINA E 400UI X 10CAP).
    """
    n = _norm(nombre)
    if re.search(rf"\bvitaminas?\s*[-/]?\s*{w}\b", n):
        return True
    if re.search(rf"\bvit\s*[-/]?\s*{w}\b", n):
        return True
    # 'C' suelta como palabra completa (p. ej. 'VITAMINA C + ZINC').
    return bool(re.search(rf"(?:^|\s){w}(?:\s|$)", n))

_MIN_LEN_ENTRADA = 3
_MIN_LEN_TYPO = 5

# Número + unidad PEGADOS: '25mg', '500ml', '10mcg', '2gr'. Es una DOSIS, no un nombre de
# fármaco. (Los códigos del nombre —'d3', 'k2', 'b12'— no llevan unidad y no caen aquí.)
_RE_DOSIS_TOKEN = re.compile(r"\d+(?:[.,]\d+)?(?:mg|ml|mcg|gr|g|ui|cc|kg|tab|caps?)$")


def _es_codigo_del_nombre(w: str) -> bool:
    """¿Es un CÓDIGO del nombre (d3, k2, b6, b12, q10)? Entonces NO es una dosis.

    Los NÚMEROS puros sí son dosis y no identifican nada ('40', '850': el catálogo los
    filtra aparte). Pero un código letra+número es parte del NOMBRE: VITAMINA D3 no es
    lo mismo que VITAMINA B12, y son dos productos distintos del catálogo.

    Sin esto, `tokens_farmaco('vitamina d3 k2')` devolvía `['vitamina']`: el filtro de
    relevancia no podía exigir ni 'd3' ni 'k2', así que se dejaba pasar TODO lo que
    tuviera 'vitamina' (multivitaminas, jabón con provitamina, B12...). Medido: el
    producto que el cliente quiere —`VITAMINA D3+K2 2000 IU X 30 TAB PHARMACORP`— no
    aparecía en la respuesta.

    Se excluye la forma 'x30' (marcador de ENVASE, no del nombre) para que no se exija
    como si fuera parte del fármaco.
    """
    return bool(re.fullmatch(r"[a-wyz]{1,2}\d{1,3}", w))

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


# -----------------------------------------------------------------------------
# C2 (F14.4) — TYPO CREÍBLE: el fuzzy de 2 ediciones no puede cruzar de categoría
# terapéutica cuando el término es UN SOLO token.
#
# Caso real medido: 'vivacina' (8 letras, sin resultados propios) matcheaba
# 'VITAMINA' con distancia 2 (v→t, c→m) y el cliente que pedía otra cosa recibía
# VITAMINA K y VITAMINA C. Dos ediciones arbitrarias sobre un token largo son
# demasiada libertad: cualquier palabra española acaba a distancia 2 de otra.
#
# Regla: con distancia 1 (typo de una letra, incluida la transposición) no se
# toca nada — es el caso que el proyecto ya validó. Con distancia 2 se exige
# EVIDENCIA DE PARENTESCO: prefijo común de 3+ letras ('omeprasol'≈'omeprazol',
# 'amoxicilna'≈'amoxicilina', 'metmorfina'≈'metformina') o transposición de dos
# letras adyacentes ('lorsatan'≈'losartan', 'diclofencao'≈'diclofenaco').
# 'vivacina' vs 'vitamina': prefijo 2 y ninguna transposición → NO pasa.
#
# Solo aplica cuando el término tiene UN token de señal: con 2+ tokens la
# estructura AND/OR ya aporta redundancia (y ahí está el comportamiento medido
# de las fases previas, que no se toca).
_TYPO_PREFIJO_MIN = 3


def _prefijo_comun(a: str, b: str) -> int:
    """Letras iniciales idénticas entre a y b."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _es_desplazamiento(a: str, b: str) -> bool:
    """¿b se obtiene MOVIENDO un solo carácter de a a otra posición?

    True:  'diclofencao'→'diclofenaco' (transposición de dos letras),
           'lorsatan'→'losartan' (la 'r' se corre dos puestos),
           'flucozanol'→'fluconazol'.
    False: 'vivacina'→'vitamina' — dos letras DISTINTAS cambiadas por otras
           ('v'→'t', 'c'→'m'), que es el salto de categoría que hay que frenar.

    Un desplazamiento es el typo más benigno que existe: todas las letras son
    las mismas y en el mismo orden salvo un carácter que se movió. Se comprueba
    quitando cada carácter de `a` e insertándolo en cada posición.
    """
    if a == b or len(a) != len(b):
        return False
    n = len(a)
    for i in range(n):
        movido = a[i]
        sin = a[:i] + a[i + 1:]
        for j in range(n):
            if sin[:j] + movido + sin[j:] == b:
                return True
    return False


def _typo_aceptable(a: str, b: str, dist: int, estricto: bool = False) -> bool:
    """¿La distancia `dist` entre a y b es un typo aceptable?

    dist ≤ 1 → siempre (typo de una letra / transposición: comportamiento ya
    validado). dist 2 → siempre, salvo con `estricto=True` (término de UN solo
    token), donde se exige evidencia de parentesco: prefijo común de 3+ letras
    o desplazamiento de un carácter.
    """
    if dist <= 1 or not estricto:
        return True
    return _prefijo_comun(a, b) >= _TYPO_PREFIJO_MIN or _es_desplazamiento(a, b)


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
        # UNA DOSIS NO ES UN FÁRMACO. '25mg', '500ml', '10mcg' son número+unidad pegados:
        # no identifican nada. Sin esta regla, en 'acido hialuronico 25mg 2' el token de
        # señal '25mg' acababa siendo el NÚCLEO del fármaco, la corrección por catálogo lo
        # "arreglaba" a 'kimiceg' (un LABORATORIO) y el cliente recibía 20 productos de
        # Kimiceg en vez del ácido hialurónico que había pedido.
        # (No confundir con los códigos del nombre 'd3'/'k2'/'b12': esos no llevan unidad.)
        if _RE_DOSIS_TOKEN.fullmatch(w):
            continue
        if len(w) < umbral or w.isdigit():
            # EXCEPCIÓN 1: los CÓDIGOS del nombre ('d3', 'k2', 'b12') son cortos pero
            # identifican el producto. Ver `_es_codigo_del_nombre`.
            # EXCEPCIÓN 2: la LETRA de la vitamina ('vitamina c').
            if not (_es_codigo_del_nombre(w) or _es_letra_de_vitamina(w, termino)):
                continue
        if w in _FUNCIONALES:
            continue
        out.append(w)
    return out


def es_relevante(
    termino: str,
    nombre_producto: str,
    permitir_typo: bool = True,
    typo_estricto: bool | None = None,
) -> bool:
    """¿El producto comparte señal real con el término buscado?

    True si algún token del término aparece en el nombre del producto como
    palabra completa, como prefijo (catálogo truncado: 'ATORVASTATIN'), dentro
    de un compuesto ('FOTORRETIN' para 'fotorretin'), o con distancia ≤2
    (typos: 'omeprasol'~'omeprazol').

    Si el término NO tiene ningún token de señal (todo relleno: 'gracias por
    todo'), devuelve False: no hay evidencia de que el producto tenga relación.

    `permitir_typo=False` (F14.4): el término viene de un ALIAS reescrito a la
    grafía canónica del catálogo, así que no hace falta tolerar typos — y no
    tolerarlos evita que la coincidencia la dé un producto ajeno.
    `typo_estricto=True` (C2/F14.4): término de UN solo token de señal → la
    distancia 2 exige evidencia de parentesco (prefijo 3+ o transposición).
    """
    senales = tokens_senal(termino, _MIN_LEN_SENAL)
    if not senales:
        return False
    # C2/F14.4: con UN solo token de señal, la distancia 2 de typo exige
    # evidencia de parentesco ('vivacina' ≈ 'vitamina' NO cruza de categoría).
    # Se deduce del TÉRMINO para que el resultado no dependa de quién llame.
    if typo_estricto is None:
        typo_estricto = len(senales) <= 1
    hay = _norm(nombre_producto)
    hay_tokens = hay.split()
    hay_set = set(hay_tokens)
    # LA LETRA DE LA VITAMINA SE EXIGE. Buscando 'vitamina c', el producto VITAMINA E
    # comparte 'vitamina' y entraba en la lista (20 productos de E, K, B12 y colágeno
    # para quien pidió C). Con la letra pedida, ese producto queda fuera.
    letras = [w for w in senales if w in _VITAMINAS]
    if letras:
        if not all(_relevante_letra_de_vitamina(w, hay) for w in letras):
            return False
        resto = [w for w in senales if w not in _VITAMINAS]
        if not resto:
            return True
        return any(_coincide_token(w, hay, hay_tokens, hay_set) for w in resto)
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
        if not permitir_typo:
            continue
        for t in hay_tokens:
            if (len(t) >= _MIN_LEN_TYPO and abs(len(t) - len(w)) <= _MAX_DIST
                    and not _difiere_por_sal(w, t)):
                dist = _levenshtein(w, t)
                if dist <= _MAX_DIST and _typo_aceptable(
                    w, t, dist, estricto=typo_estricto
                ):
                    return True
    return False


def _coincide_token(w: str, hay: str, hay_tokens: list[str], hay_set: set[str]) -> bool:
    """¿Este token del término aparece en el nombre del producto? (cuerpo de `es_relevante`)"""
    if w in hay_set:
        return True
    for t in hay_tokens:
        if (len(t) >= _MIN_LEN_SENAL - 1 and (t.startswith(w) or w.startswith(t))
                and not _difiere_por_sal(w, t)):
            return True
    if len(w) >= 6 and w in hay:
        return True
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
    # Sinónimos COLOQUIALES de la forma farmacéutica. Faltaban 'pastillas' y
    # 'pildoras' —que es como el cliente PIDE— y con el rescate escalonado por
    # selectividad se colaban: 'omeprazol pastillas' daba ['omeprazol','pastillas']
    # y 'pastillas' matcheaba 6 productos (LAFARCAINA, ALURON) frente a 8 de
    # 'omeprazol', así que el rescate elegía la FORMA y devolvía productos que no
    # tienen omeprazol. Una forma farmacéutica nunca identifica el fármaco.
    "pastilla", "pastillas", "pildora", "pildoras",
    # Audiencia
    "adulto", "adultos", "pediatrico", "pediatrica", "nino", "nina", "ninos",
    "ninas", "infantil", "bebe", "bebes", "lactante", "lactantes", "mayores",
    "anciano", "ancianos",
}

# SUFIJOS COMERCIALES: acompañan a la marca pero NO la identifican.
#
# "ATAMEL PLUS" ya se identifica con 'atamel'; el 'plus' solo dice que es la variante
# reforzada. Por eso NO pueden usarse para RESCATAR productos: 'plus' aparece en 51
# nombres del catálogo (PAPEL PLUS, ALIVET PLUS, BETADEX PLUS, VITISIVAL PLUS...), así
# que un rescate por 'plus' le muestra al cliente papel, cremas y analgésicos cuando
# pidió un medicamento concreto.
#
# Caso real: "Isopray plus" (ISOSPRAY PLUS, escrito con una S menos). El AND exigía
# {isopray, plus} y fallaba porque ISOSPRAY no está en el catálogo del provider 27; el
# rescate por 'plus' devolvía 41 productos ajenos. Medido sobre 1000 consultas reales:
# dejando que el sufijo rescate → 41; sin dejar que rescate → 5, sin un solo falso
# rechazo nuevo en los catálogos 19 y 27.
#
# OJO: el sufijo SÍ sigue participando del AND (paso 1). Es lo que pide el cliente que
# consulta "isopray plus" en conjunto: si el producto exacto existe, gana el AND.
_SUFIJOS_COMERCIALES = {
    "plus", "forte", "duo", "retard", "compuesto", "extra", "ultra", "active",
    "pack", "super", "max", "pro", "lp", "sr", "xr", "mr", "cr", "er",
}


def _es_sufijo_comercial(palabra: str) -> bool:
    """¿La palabra es un sufijo comercial, aunque esté mal escrita?

    'plus'/'pluss'/'plas' → sí.  'plastico', 'losartan' → no.

    Se compara también con distancia ≤1 porque el cliente escribe 'plus' de muchas
    formas; un sufijo mal escrito que no se reconozca vuelve a colarse por el rescate.
    """
    for s in _SUFIJOS_COMERCIALES:
        if palabra == s:
            return True
        # Solo se tolera typo en sufijos de 4+ letras: 'pro'/'max'/'duo' son demasiado
        # cortos y cualquier palabra cercana colisionaría ('pro' ~ 'pero', 'max' ~ 'mas').
        if len(s) < 4 or abs(len(palabra) - len(s)) > 1:
            continue
        if _levenshtein(palabra, s) <= 1:
            return True
    return False



# ── SALES DEL PRINCIPIO ACTIVO: 'clorhidrato' no es un medicamento ────────────────
# 'CLORHIDRATO' lo comparten decenas de fármacos distintos (betahistina, metformina,
# bromexina, loperamida, fexofenadina…). Buscar SOLO por la sal devuelve un catálogo al azar.
#
# Caso real (provider 05, 2026-10-27): el OCR de una caja de 'Fexofenadina Clorhidrato
# CALOX' devolvió solo 'CLORHIDRATO' y el agente cotizó betahistina, metformina, bromexina y
# loperamida como si fueran lo que el cliente pidió.
#
# NO están en la lista 'sodico', 'potasico', 'cloruro' ni 'bicarbonato': esos SÍ pueden ser
# el nombre de un producto ('cloruro de sodio', 'bicarbonato de sodio').
_SALES_FARMACEUTICAS = {
    "clorhidrato", "hidrocloruro", "hidroclorhidrato", "diclorhidrato", "bromhidrato",
    "maleato", "succinato", "fumarato", "tartrato", "bitartrato", "citrato",
    "fosfato", "difosfato", "sulfato", "nitrato", "acetato", "benzoato",
    "estearato", "valerato", "propionato", "dipropionato", "mesilato", "besilato",
    "tosilato", "oxalato", "lactato", "gluconato",
    "monohidrato", "dihidrato", "trihidrato", "anhidro", "anhidra",
    "micronizado", "micronizada",
}

def tokens_farmaco(termino: str) -> list[str]:
    """Tokens del término que DEBEN identificar un fármaco.

    Excluye presentación y audiencia: 'dovilin jarabe adulto' → ['dovilin']. Si el
    término es SOLO presentación ('jarabe para la tos'), devuelve []: no hay fármaco
    que exigir y el comportamiento permisivo es el correcto.

    Cubre también las VARIANTES con typo de la presentación ('ampola', 'ampollla',
    'jaravbe'): una variante que no esté en la lista cuenta como token de fármaco y
    valida productos ajenos. Caso real: el filtro descartaba las 20 ampollas de
    'depofem ampolla' (correcto) pero la VARIANTE 'depofem ampola' las dejaba pasar
    TODAS, porque 'ampola' no estaba en la lista → 20 medicamentos ajenos al cliente.
    """
    out: list[str] = []
    for w in tokens_senal(termino, _MIN_LEN_SENAL):
        if w in _PRESENTACION_AUDIENCIA:
            continue
        # La SAL tampoco identifica un fármaco: 'clorhidrato' lo comparten betahistina,
        # metformina, bromexina, loperamida… Sin esto, el núcleo ausente de
        # 'fexofenadina clorhidrato 120 mg' era 'clorhidrato' y la recuperación buscaba por
        # la sal (medido en producción, provider 32, 2026-10-27:
        # "busco SOLO por el núcleo 'clorhidrato'").
        if w in _SALES_FARMACEUTICAS:
            continue
        # ¿es una presentación mal escrita? Se compara por prefijo/Levenshtein contra
        # las formas conocidas, no por igualdad exacta.
        if _parece_presentacion(w):
            continue
        out.append(w)
    return out


def _parece_presentacion(palabra: str) -> bool:
    """¿La palabra es una forma farmacéutica, aunque esté mal escrita?

    'ampola', 'ampollla', 'ampoylla' → sí (variantes de 'ampolla').
    'depofem', 'losartan', 'acetaminofen' → no.
    """
    for forma in _PRESENTACION_AUDIENCIA:
        if abs(len(palabra) - len(forma)) > 2:
            continue
        if palabra == forma:
            return True
        # Prefijo largo común: cubre las transposiciones ('ampola' vs 'ampolla').
        if len(forma) >= 5 and palabra[:4] == forma[:4] and _levenshtein(palabra, forma) <= 2:
            return True
    return False


def _tokens_senal_ordenados(termino: str) -> list[str]:
    """Tokens de señal del término, de MAYOR a menor longitud.

    El orden importa para el diagnóstico y para que el más específico ('citrato')
    mande sobre el genérico ('potasio').
    """
    return sorted(tokens_senal(termino, _MIN_LEN_SENAL), key=len, reverse=True)


def es_relevante_estricto(
    termino: str,
    nombre_producto: str,
    permitir_typo: bool = True,
    typo_estricto: bool | None = None,
) -> bool:
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
    # C2/F14.4: con UN solo token de señal, el typo de distancia 2 exige
    # evidencia de parentesco (ver _typo_aceptable). Si el llamador ya decidió,
    # se respeta su valor.
    if typo_estricto is None:
        typo_estricto = len(tokens_senal(termino, _MIN_LEN_SENAL)) <= 1
    if len(senales) < 2:
        return (
            es_relevante(
                senales[0], nombre_producto,
                permitir_typo=permitir_typo, typo_estricto=typo_estricto,
            )
            if senales else False
        )
    for w in senales:
        if not es_relevante(
            w, nombre_producto,
            permitir_typo=permitir_typo, typo_estricto=typo_estricto,
        ):
            return False
    return True


def filtrar_relevantes(
    termino: str, productos: list[dict], permitir_typo: bool = True
) -> list[dict]:
    """Devuelve solo los productos relevantes para el término.

    En CUATRO pasos, del más estricto al más permisivo:

      1. AND — el producto comparte señal con TODOS los tokens de FÁRMACO del término.
         Es lo que el cliente quiere ("citrato potasio" = citrato Y potasio) y quita de
         raíz los fármacos que solo comparten una palabra (DICLOFENAC POTASICO ante
         "citrato potasio"). Medido: quita 27/27 ajenos en esa consulta.
      2. OR — basta un token de FÁRMACO. Rescata los casos en que el catálogo nombra al
         revés ("nitrato de miconazol" → "MICONAZOL 400MG ... (NITRATO)") o trae el
         nombre partido. LOS SUFIJOS COMERCIALES NO RESCATAN: se prueban primero los
         tokens ESPECÍFICOS y solo si no hay ninguno se cae al OR clásico. Ver
         `_SUFIJOS_COMERCIALES`.
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

    POR QUÉ EL OR EXCLUYE LOS SUFIJOS COMERCIALES: "Isopray plus" (ISOSPRAY PLUS, con una
    S menos) exigía {isopray, plus} en el AND; como ISOSPRAY no está en el catálogo del
    provider 27, el AND fallaba y el OR rescataba con 'plus' → 41 productos ajenos
    (PAPEL PLUS, BETADEX PLUS, VITISIVAL PLUS...). Medido sobre 1000 consultas reales de
    los catálogos 19 y 27: sin el rescate por sufijo, 0 falsos rechazos nuevos.

    Verificado a escala: 6.146 nombres reales buscados por su propio nombre → 0
    pérdidas, con los controles legítimos intactos (losartan potasico 11, diclofenac
    sodico 11, acetaminofen 650 mg 1, omeprasol 3, amoxicilna 10).
    """
    if not productos:
        return productos
    farmacos = tokens_farmaco(termino)
    utiles = farmacos or tokens_senal(termino, _MIN_LEN_SENAL)
    # LA LETRA DE LA VITAMINA MANDA (F14/C1): si el cliente pidió "vitamina C",
    # ninguna otra vitamina puede entrar — ni en el estricto ni en el rescate.
    # Sin esto el rescate, que se apoya en el token genérico 'vitamina', volvía a
    # colar VITAMINA K y VITAMINA E (medido en la simulación de conversaciones).
    letra_vit = _letra_de_vitamina_del_termino(termino)
    if letra_vit and productos:
        productos = [
            p for p in productos
            if not _es_otra_vitamina(
                str(p.get("nombre") or p.get("producto") or ""), letra_vit
            )
        ]
    # C2/F14.4: con UN solo token de señal, la distancia 2 de typo exige
    # evidencia de parentesco ('vivacina' ≈ 'vitamina' ya no cruza de categoría).
    # Con 2+ tokens se mantiene el comportamiento medido en las fases previas.
    typo_estricto = len(tokens_senal(termino, _MIN_LEN_SENAL)) <= 1

    def _ok(w: str, nombre: str) -> bool:
        return es_relevante(
            w, nombre, permitir_typo=permitir_typo, typo_estricto=typo_estricto
        )

    estrictos = [
        p for p in productos
        if all(_ok(w, str(p.get("nombre") or "")) for w in utiles)
    ]
    if estrictos:
        return estrictos

    # RESCATE ESCALONADO, DEL TOKEN MÁS SELECTIVO AL MÁS GENÉRICO (cliente).
    #
    # (Se conserva la explicación original del autor de este bloque: el rescate
    # prueba de a UN token, del más selectivo al más genérico, en vez del OR con
    # todos a la vez. F14 solo añade los flags `_ok` para que el rescate respete
    # `permitir_typo` cuando el término viene de un ALIAS reescrito.)
    especificos = [w for w in utiles if not _es_sufijo_comercial(w)]

    # EL FÁRMACO CABEZA MANDA: si el PRIMER token específico no comparte señal con
    # NINGÚN producto de la lista, NO se rescata con los tokens que le siguen
    # (marca ≠ fármaco). Ver el caso real de 'magnesio plus life'.
    if len(especificos) > 1:
        cabeza = especificos[0]
        if not any(
            _ok(cabeza, str(p.get("nombre") or "")) for p in productos
        ):
            return []

    for rescate in (especificos, utiles if not especificos else []):
        if not rescate:
            continue
        for token in sorted(
            rescate,
            key=lambda w: sum(
                1 for p in productos if _ok(w, str(p.get("nombre") or ""))
            ),
        ):
            relajados = [
                p for p in productos
                if _ok(token, str(p.get("nombre") or ""))
            ]
            if relajados:
                return relajados


    # No hay NINGÚN producto que comparta señal con los tokens útiles.
    if farmacos:
        # El cliente nombró un fármaco concreto y no está en el catálogo: NO mostrarle
        # otro medicamento. Vacío a propósito.
        return []
    # Término sin fármaco (solo relleno/presentación): fail-safe clásico.
    filtrados = [
        p for p in productos
        if es_relevante(
            termino, str(p.get("nombre") or ""),
            permitir_typo=permitir_typo, typo_estricto=typo_estricto,
        )
    ]
    return filtrados or productos


def _token_cabeza_farmaco(termino: str) -> str | None:
    """El token que NOMBRA el fármaco: el primero específico del término.

    'magnesio plus life' → 'magnesio'.  'asaprol as pina' → 'asaprol'.

    Es el token con el que hay que reintentar la búsqueda cuando la frase completa
    no trajo el fármaco al catálogo (el motor del CRM matchea todos los tokens).
    El cliente escribe el nombre del producto primero y añade descriptores después
    ('plus', 'life', 'forte', la marca, la presentación): el primero es el fármaco.
    """
    for w in tokens_farmaco(termino):
        if not _es_sufijo_comercial(w):
            return w
    return None


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


def tokens_posible_farmaco(termino: str) -> list[str]:
    """Versión pública y CONSERVADORA de `_tokens_farmaco` (F14.5).

    La usa la partición de consultas multi-medicamento: un lado del conector
    solo cuenta como medicamento si le queda algún token que NO sea relleno,
    vocabulario de negocio ni verbo de discurso. Sin esto, "quiero amoxicilina y
    que me DIGAS el precio" se partía en dos y el agente buscaba 'digas' en el
    catálogo. `tokens_farmaco` (la de relevancia de resultados) es deliberadamente
    más permisiva — no es la que se usa aquí.
    """
    return _tokens_farmaco(termino)


def _letra_de_vitamina_del_termino(termino: str) -> str | None:
    """La letra de la vitamina que pidió el cliente ('vitamina c' → 'c')."""
    for w in _norm(termino).split():
        if _es_letra_de_vitamina(w, termino):
            return w
    return None


def _es_otra_vitamina(nombre: str, letra: str) -> bool:
    """¿El producto es de una vitamina DISTINTA a `letra`?

    Hallazgo de la simulación de conversaciones (2026-10-30): con el término
    'vitamina c' el guard POR PRODUCTO acertaba (VITAMINA K no es relevante),
    pero el RESCATE ESCALONADO volvía a colarlos porque se apoya en el token
    genérico 'vitamina' — el cliente pedía C y recibía C, K y E.
    """
    for otra in _VITAMINAS:
        if otra == letra:
            continue
        if _relevante_letra_de_vitamina(otra, nombre):
            return True
    return False


def correccion_creible(original: str, corregido: str) -> bool:
    """¿La corrección de escritura es un typo del MISMO fármaco? (regla C2)

    Puerta ÚNICA para cualquier corrección que el código haga "hacia" un término
    del catálogo: un typo creíble (misma letra inicial/salvo un desplazamiento)
    sí, un salto de categoría no.

    Por qué existe (hallazgo de la simulación de conversaciones, 2026-10-30):
    la recuperación por "núcleo" del árbol integrado validaba el resultado
    contra el término YA CORREGIDO, así que 'vivacina' → 'vitamina' pasaba con
    los productos de VITAMINA C/K/E y el cliente recibía vitaminas pidiendo otra
    cosa (con el añadido «Asumí que buscas VITAMINA 👍»). El filtro de relevancia
    de C2 no lo veía porque nunca comparaba el original.

    Casos reales que SÍ pasan: 'omeprasol'→'omeprazol' (prefijo 6),
    'diclofencao'→'diclofenaco' (desplazamiento), 'solunoval'→'solunovar'
    (1 edición), 'hialuronico'→'hialuronato' (prefijo 8).
    Casos que se rechazan: 'vivacina'→'vitamina' (prefijo 2, 2 sustituciones).
    """
    a = _norm(original).split()
    b = _norm(corregido).split()
    if not a or not b:
        return False
    for x, y in zip(a, b):
        if x == y:
            continue
        dist = _levenshtein(x, y)
        if not _typo_aceptable(x, y, dist, estricto=True):
            return False
    return True


# El OCR de una imagen que NO es un medicamento devuelve una FRASE, no un nombre.
#
# Caso real (provider 19, 2026-10): el cliente mandó el COMPROBANTE DE UN PAGO
# ("Hola buenos días este es el pago muchas gracias" + la captura de Pagomóvil
# BDV) y el OCR respondió:
#
#   "No se puede extraer texto relacionado con medicamentos o recetas de la
#    imagen proporcionada, ya que no contiene dicha información."
#
# Esa frase es PEOR que un OCR vacío: pasa el blindaje anti-prompt (no repite el
# vocabulario del prompt) y se usa como TÉRMINO DE BÚSQUEDA. El catálogo difuso
# resolvió `'extraer texto relacionado proporcionad'` a FREEGELLS EXTRA FUERTE y
# JABON ANITA PEARL EXTRACT; en el otro caso (`'texto legible proporcionada
# cuchara medidora'`) a UNA PERA VAGINAL. El cliente mandó un pago y recibió
# productos que no pidió.
#
# El prompt del OCR ASUME que la imagen es un medicamento ("Extrae SOLO el texto
# legible de esta imagen de medicamento o receta"), así que ante un comprobante el
# modelo no puede cumplir y escribe esa negación. Hay que tratarla como OCR vacío:
# el marcador queda honesto (imagen sin texto de medicamento) y el agente responde
# al CONTEXTO (el texto del cliente dice que es un pago), no a la frase.
_PISTAS_OCR_SIN_MEDICAMENTO = (
    r"no\s+(?:se\s+puede|hay|es\s+posible|contiene|muestra|aparece|se\s+observa)",
    r"(?:imagen|foto|captura)\s+(?:proporcionada|adjunta|enviada)",
    r"sin\s+(?:informaci[oó]n|texto)\s+(?:sobre|de|relacionad)",
    r"no\s+(?:contiene|muestra|presenta|incluye)\s+(?:informaci[oó]n|texto|medicamento)",
    r"no\s+(?:es|se\s+trata\s+de)\s+(?:una?\s+)?(?:medicamento|receta)",
    r"no\s+hay\s+texto",
)


def parece_presentacion_personal(texto: str) -> bool:
    """¿El texto es una PRESENTACIÓN de la persona, no una consulta de medicamento?

    'soy víctor', 'me llamo Pedro', 'soy la dueña', 'mi nombre es Ana'.

    La construcción es la señal: "soy/me llamo/mi nombre es" + un nombre. El nombre
    propio es un token sustantivo de 6 letras que ningún guard de "cuerpo de fármaco"
    puede distinguir de 'losartan', así que hay que reconocer la ESTRUCTURA.

    Caso real (conv 2720, 2026-10): el cliente venía de un audio donde alguien
    preguntaba por él y escribió "Soy víctor". El agente buscó "Víctor" en el catálogo
    y respondió "No te tengo información sobre 'Víctor' en el catálogo" — tratando a
    una persona como un medicamento.

    OJO: solo la construcción COMPLETA. 'soy' suelto o 'quiero 2 de la opción 1' no
    cuentan. Si la frase trae una dosis ('soy diabético, necesito metformina 850 mg')
    NO es una presentación a secas: hay un medicamento que buscar, y el guard de
    entrada no debe bloquearlo.
    """
    if not texto:
        return False
    t = texto.strip().lower()
    if not t:
        return False
    # Si trae dosis con unidad, hay una consulta de medicamento de verdad: no bloquear.
    if re.search(r"\d+\s*(?:mg|ml|mcg|gr|g|cc|ui|%)\b", t):
        return False
    # Nombre de medicamento explícito tampoco: 'soy María, tienes atamel?' es consulta.
    _VERBOS_MEDICINA = re.compile(
        r"\b(tienes|tienen|tiene|hay|busco|necesito|quiero|venden|precio|"
        r"cuesta|disponible|consigo|manejan)\b"
    )
    if _VERBOS_MEDICINA.search(t):
        return False
    # ESTADOS que empiezan con "soy" pero NO son una presentación: "soy alérgico a la
    # penicilina", "soy diabético", "soy hipertenso". Ahí hay información clínica útil
    # (y a veces un fármaco), no un nombre. Solo cuentan como presentación si lo que
    # sigue es un nombre propio, no un adjetivo de condición.
    _ESTADOS = re.compile(
        r"^\s*(?:soy|me\s+llamo)\s+(?:al[eé]rgic\w*|diab[eé]tic\w*|hipertens\w*|"
        r"asm[aá]tic\w*|embarazad\w*|operad\w*|client\w*|usuari\w*|"
        r"un\s+paciente|una\s+paciente)\b"
    )
    if _ESTADOS.search(t):
        return False
    return bool(
        re.search(
            r"^\s*(?:hola\s+|buenas\s+|buenos\s+d[ií]as\s+|buenas\s+tardes\s+)?"
            r"(?:soy|me\s+llamo|mi\s+nombre\s+es|aqui\s+habla|les\s+habla)\b",
            t,
        )
        or re.fullmatch(
            r"\s*(?:soy|me\s+llamo)\s+(?:el|la|un|una)?\s*\w{2,20}\s*[.!]?\s*", t
        )
    )


def parece_ocr_sin_medicamento(texto: str) -> bool:
    """¿El texto del OCR dice que la imagen NO trae un medicamento?

    True para las respuestas que hablan de la IMAGEN o de la AUSENCIA de
    medicamento ("no contiene información sobre medicamentos", "la imagen no
    muestra ningún medicamento"). Esas frases NO son un término de búsqueda.

    También cubre el marcador EXACTO `NO_APLICA` que el prompt del OCR pide cuando
    la imagen no es de un medicamento (comprobante de pago, utensilio, comida): es
    una señal deliberada y estable, mucho más fiable que una frase libre — el
    modelo la redactaba distinto en cada corrida.

    Guarda contra el falso positivo: una dosis con unidad (`500 mg`, `2 ml`) es
    señal inequívoca de un medicamento de VERDAD — aunque la frase diga "no" (un
    OCR que transcribe "NO USAR MÁS DE 500 MG" sigue siendo un medicamento). Con
    dosis presente, nunca se descarta.
    """
    if not texto:
        return False
    t = texto.lower().strip()
    if t.startswith("no_aplica") or t == "no aplica":
        # El marcador deliberado: puede venir SOLO o con una explicación detrás.
        return True
    if re.search(r"\d+\s*(?:mg|ml|mcg|g|ui|%)", t):
        return False
    return any(re.search(p, t) for p in _PISTAS_OCR_SIN_MEDICAMENTO)

