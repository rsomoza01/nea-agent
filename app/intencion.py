"""F15 — Capa de comprensión del acto de conversación.

**Para qué existe.** El agente de farmacia buscaba en el catálogo antes de entender
qué le estaban diciendo. Quejas reales (oct-2026), reproducidas:

- «No puede ser Indu.» (el cliente habla del LABORATORIO) → el agente contestó
  «⚠️ No tengo SER INDU».
- «Vivo en Ciudad Bolivar.» (dónde vive) → 13 consultas al catálogo en un turno
  (`vivo ciudad bolivar` → transposiciones → `boliva`, `boliv`, `boli`, `bol`),
  contaminando `med_queries` (de donde se derivan los alias de F14.4).
- «Solo una marca?» → término `marca`; «Por ahroa no» → término `ahroa`.

El decisor anterior era un heurístico de FORMA (`turn._parece_nombre_producto`):
«sin '?', ≤4 palabras, ninguna de saludo y una palabra de ≥5 letras» ⇒ se buscaba.
Eso acepta cualquier oración, y ninguna lista negra lo arregla de forma estable
(«Shampo Dreene» DEBE buscar y «Vivo en Ciudad Bolivar» NO, con los mismos rasgos).

**Qué hace esta capa.** Decide, antes de buscar: ¿el mensaje trae una consulta de
medicamento, y cuál es el término? Es decir, primero entiende el acto de
conversación (saludo, cortesía, dato personal, pregunta de atributo, dilatoria,
reclamo, consulta de medicamento, otro) y solo entonces se busca. En la duda,
pregunta en vez de buscar.

**Cómo lo hace, en tres caminos** (para no pagar una llamada en cada turno):

1. `nombre` — camino rápido: mensaje corto (1–2 palabras) con forma de nombre
   pelado y sin ninguna palabra conversacional conocida («shampo dreene»,
   «vivacina»). Es el caso que motivó el heurístico de forma original.
2. `capa` — LLM: **todo lo demás**, incluidos los mensajes con verbo de consulta.
   Una sola llamada con contrato JSON cerrado. Es el único camino que cuesta
   dinero y solo se paga donde antes el heurístico adivinaba.

   *(Hubo un camino rápido por VERBO — «tienen», «hay», «necesito», «precio»… — y se
   retiró al medirlo: el verbo aparece también en frases de charla («tienen
   delivery?», «por ahora no necesito nada») y no es evidencia de fármaco. Ver
   `camino_rapido`.)*

**Fail-safe.** Si la capa no responde (timeout, proveedor caído, JSON inválido),
`hay_consulta=None` y el llamador conserva el comportamiento anterior. La mejora
se mide con la capa viva; un proveedor caído no cambia el flujo.

**Auditoría.** Cada veredicto lleva `fuente`, `acto`, `motivo` y `confianza`, y el
turno los guarda junto a la búsqueda (`med_queries`) para poder medir la capa y
saber a quién culpar (¿entendió mal la capa o falló el catálogo?) en un caso real.

No toca `guards.py` ni la cadena de vetos: decide ANTES de la búsqueda; los vetos
siguen siendo la última palabra sobre la respuesta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from app.llm import OpenAiLlm

logger = logging.getLogger("nea.intencion")

# Actos de conversación que la capa reconoce. `consulta_medicamento` es el único
# que dispara búsqueda; el resto se responde conversacionalmente.
ACTOS = (
    "saludo",
    "cortesia",
    "dato_personal",
    "atributo_producto",
    "dilatoria",
    "reclamo",
    "consulta_medicamento",
    "otro",
)

# Palabras que, si aparecen, mandan el mensaje a la CAPA en vez de al camino
# rápido de "nombre pelado": son palabras de conversación, no de producto.
# (Solo desvían la decisión hacia el modelo; NO deciden por sí mismas.)
_PALABRAS_CONVERSACION = {
    "vivo", "vives", "vive", "ciudad", "estado", "calle", "avenida", "sector",
    "marca", "marcas", "laboratorio", "laboratorios", "generico", "genérico",
    "original", "india", "indu", "chino", "chinos", "nacional", "importado",
    "ahora", "ahroa", "hoy", "manana", "mañana", "luego", "despues", "después",
    "gracias", "hola", "buenas", "buenos", "tardes", "noches", "saludos",
    "listo", "ok", "okay", "dale", "perfecto", "claro", "igual", "entonces",
    "cuenta", "banco", "pago", "pagare", "pagaré", "efectivo", "zelle", "pago movil",
    "delivery", "envio", "envíos", "envios", "domicilio", "sucursal", "factura",
    "horario", "abierto", "cerrado", "caja", "cajas", "unidad", "unidades",
    "transferencia", "devolucion", "devolución", "reclamo", "gerente", "encargado",
    "receta", "medico", "médico", "doctora", "doctor", "consultorio",
    "bendicion", "bendición", "dios", "familia", "hijo", "hija", "mama", "mamá",
}
_PALABRAS_CONVERSACION = {re.sub(r"[áéíóú]", lambda m: "aeiou"["áéíóú".index(m.group())], p)
                          for p in _PALABRAS_CONVERSACION}

# Formas farmacéuticas: si el término las nombra, hay evidencia de que es un producto
# ('jarabe', 'crema'...) y el nombre pelado no es ambiguo.
_FORMAS_FARMACEUTICAS = {
    "jarabe", "crema", "gotas", "tabletas", "tableta", "capsulas", "capsula", "cap",
    "tab", "suspension", "solucion", "oral", "bucal", "inyectable", "ampolla",
    "supositorio", "ovulos", "ovulo", "pomada", "gel", "spray", "polvo", "sobre",
    "sobres", "jbe", "gts", "crema", "locion", "shampoo", "champu", "jabon",
}

_SISTEMA = """Eres la etapa de COMPRENSIÓN de un asistente de farmacia por WhatsApp.
Tu único trabajo: decir si el mensaje del cliente pide un medicamento (y cuál), o si es
conversación. NO respondes al cliente, NO buscas, NO saludas: solo clasificas.

Devuelves SOLO un objeto JSON, sin texto alrededor ni ```:
{"acto": "<uno de: saludo|cortesia|dato_personal|atributo_producto|dilatoria|reclamo|consulta_medicamento|otro>",
 "hay_consulta": true|false,
 "termino": "<texto del medicamento, o null>",
 "confianza": 0.0-1.0,
 "motivo": "<máximo 12 palabras>"}

Reglas:
- `hay_consulta` es true SOLO si el cliente pide/informa/consulta un medicamento concreto
  (o pregunta por su precio, existencia o presentación de forma que haga falta buscarlo).
- Ejemplos que NO son consulta (hay_consulta=false, termino=null):
  * «No puede ser Indu.» → habla del LABORATORIO del producto ya mostrado (acto=atributo_producto).
  * «Vivo en Ciudad Bolivar.» → da un dato personal (acto=dato_personal).
  * «Solo una marca?» → pregunta por el atributo del producto ya mostrado (acto=atributo_producto).
  * «Por ahroa no» → dilatoria/negativa temporal (acto=dilatoria).
  * «gracias, linda», «hola mi amor», «que Dios te bendiga» → saludo/cortesía.
- `termino` lleva SOLO el medicamento, limpio: sin verbos («tienen», «precio de», «quiero»),
  sin relleno y sin cortesías. «precio de la macrodantina de 100» → «macrodantina 100».
- CONSULTAS POR CATEGORÍA O SÍNTOMA son consulta (hay_consulta=true) aunque no haya un nombre:
  el cliente describe lo que necesita, no la marca. El `termino` es la categoría o el síntoma
  LIMPIO (sirve para buscar en el catálogo):
  * «busco algo para el dolor de cabeza» → hay_consulta=true, termino="dolor de cabeza".
  * «hay crema para las manchas?» → hay_consulta=true, termino="crema manchas".
  * «que antibioticos tienen?» → hay_consulta=true, termino="antibioticos".
  * «algo para la gripe» → hay_consulta=true, termino="gripe".
  * «un jarabe para la tos» → hay_consulta=true, termino="jarabe tos".
- NO son consulta aunque nombren un producto: «eso me parece caro», «me vendieron vencido»,
  «ya lo tengo», «no me sirvio».
- Si el cliente escribe un nombre que no reconoces, pero claramente es un nombre de
  medicamento (marca o fármaco, incluso mal escrito: «vivacina», «omeprasol»), hay_consulta=true.
- F15.1 — Comprueba SIEMPRE el «Término candidato» contra el mensaje del cliente: es el
  término que el sistema (o el asistente) quiere buscar. Solo hay_consulta=true si el mensaje
  pide o describe ESE medicamento (nombre, categoría o síntoma). Un candidato que no aparece en
  el mensaje es ruido: hay_consulta=false. (Confirmar algo del producto ya mostrado — «sí, esa es
  la presentación que quiero» — NO es una consulta nueva.)
  * Mensaje «tienen vivacina?» con candidato «vitamina» → hay_consulta=false (el cliente pidió
    vivacina; «vitamina» es otra categoría, aunque suene parecido).
- Si la pregunta lleva «PREGUNTA ESTRICTA», contesta SOLO por el término candidato: es el
  término que el asistente quiere buscar. hay_consulta=true si el cliente pidió ESE medicamento,
  lo describió (categoría o síntoma), es el producto que el asistente acaba de mostrar, o es un
  nombre mal escrito / variante del mismo. Un candidato que NO aparece en el mensaje del cliente
  solo se aprueba si es, con claridad, un medicamento: fármaco o marca, con o sin dosis. Nombres
  de personas, cédulas o números de documento, direcciones, fechas, cantidades sueltas, «firma»,
  «sello» y palabras de relleno (oiga, colmo, bueno) NO son medicamentos: hay_consulta=false y
  termino=null — aunque el mensaje contenga otra consulta distinta.
- Si dudas entre conversación y medicamento, pon hay_consulta=false y confianza baja:
  el asistente preguntará. Es preferible preguntar que responder sobre algo que el cliente
  no pidió.
- Cuando hay_consulta=false, el `acto` NUNCA es `consulta_medicamento`: usa el acto real del
  mensaje (saludo, cortesia, dato_personal, atributo_producto, dilatoria, reclamo u otro). Un
  `acto=consulta_medicamento` con `hay_consulta=false` es un contrato inválido, el veredicto se
  descarta y queda como no concluyente: el asistente no puede saber si le negaste algo o no te
  entendieron."""


@dataclass
class Entendimiento:
    """Veredicto de la capa. `hay_consulta=None` = no concluyente (fail-safe)."""

    acto: str = "otro"
    hay_consulta: bool | None = None
    termino: str | None = None
    confianza: float = 0.0
    motivo: str = ""
    fuente: str = "fail-safe"  # nombre | capa | fail-safe | desactivada | desvio | llm

    @property
    def concluyente(self) -> bool:
        return self.hay_consulta is not None


def _norm(texto: str) -> str:
    t = (texto or "").lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u")):
        t = t.replace(a, b)
    return t


def _tokens(texto: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _norm(texto))


def camino_rapido(texto: str, termino_candidato: str | None) -> Entendimiento | None:
    """Decide sin LLM SOLO cuando no hay duda razonable. `None` = que decida la capa.

    Qué NO está aquí y por qué: hubo una versión con un camino rápido por VERBO
    («tienen», «hay», «necesito», «precio»…). Se retiró al medirlo con el extractor
    real: el verbo no es evidencia de fármaco y disparaba búsquedas en frases
    conversacionales — «Solo una marca o hay varias?» → `marca varias`,
    «Por ahora no necesito nada» → `ahora`, «tienen delivery?» → `delivery`,
    «aceptan zelle?» → zelle. Es exactamente la clase de error que esta capa existe
    para evitar, así que el verbo (que sí aparece en frases de charla) ya no decide
    nada: lo juzga la capa con el contexto completo.
    """
    if not texto or not texto.strip():
        return Entendimiento(acto="otro", hay_consulta=False, motivo="mensaje vacío", fuente="desvio")
    # Nombre pelado corto ('shampo dreene', 'vivacina', 'atamel 500'): es el caso que
    # motivó el heurístico de forma original (un nombre sin verbo ni pregunta). Se exige
    # que NINGUNA palabra sea de conversación y que no haya '?'.
    palabras = _tokens(texto)
    termino_tokens = _tokens(termino_candidato or "")
    # Evidencia positiva (revisión externa #4): el nombre pelado solo se acepta si
    # tiene algo que lo respalde — una dosis, una forma farmacéutica o un token largo
    # (marca/fármaco). Sin evidencia, es ambiguo y lo juzga la capa.
    con_evidencia = bool(
        re.search(r"\d", termino_candidato or "")
        or any(t in _FORMAS_FARMACEUTICAS for t in termino_tokens)
        or any(len(t) >= 7 for t in termino_tokens)
    )
    if (
        termino_candidato
        and con_evidencia
        and 1 <= len(palabras) <= 2
        and all(len(p) >= 3 for p in palabras)
        and not any(p in _PALABRAS_CONVERSACION for p in palabras)
        and "?" not in texto
    ):
        return Entendimiento(
            acto="consulta_medicamento", hay_consulta=True, termino=termino_candidato,
            confianza=0.7, motivo="nombre pelado corto", fuente="nombre",
        )
    # Duda: que decida la capa (mensajes con verbo, frases, datos personales, atributos).
    return None


def _extraer_json(texto: str) -> dict[str, Any] | None:
    """Saca el primer objeto JSON del texto (tolera ```json, prólogo y cola)."""
    if not texto:
        return None
    limpio = texto.strip()
    limpio = re.sub(r"^```(?:json)?|```$", "", limpio, flags=re.M).strip()
    inicio = limpio.find("{")
    if inicio < 0:
        return None
    # Recorte por llaves balanceadas (ignorando las que van dentro de cadenas).
    profundidad = 0
    en_cadena = False
    escape = False
    for i in range(inicio, len(limpio)):
        c = limpio[i]
        if en_cadena:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c == "{":
            profundidad += 1
        elif c == "}":
            profundidad -= 1
            if profundidad == 0:
                try:
                    datos = json.loads(limpio[inicio : i + 1])
                except Exception:
                    return None
                return datos if isinstance(datos, dict) else None
    return None


def _veredicto_desde_json(datos: dict[str, Any]) -> Entendimiento | None:
    """Veredicto desde el JSON del modelo, con **esquema estricto**.

    Revisión externa (gpt-6-astra-pro, 2026-11): la primera versión aceptaba
    `{"motivo": "x"}` como negativa concluyente, `"hay_consulta": "quizás"` vía
    `bool()` y `hay_consulta=True` sin término. Un contrato incoherente no puede
    decidir: se devuelve `None` (= inconcluyente) y el turno pregunta.

    Devuelve `None` si el contrato no es válido o es incoherente.
    """
    if not isinstance(datos, dict):
        return None
    acto = datos.get("acto")
    if not isinstance(acto, str) or acto.strip().lower() not in ACTOS:
        return None
    acto = acto.strip().lower()
    crudo = datos.get("hay_consulta")
    if not isinstance(crudo, bool):  # nada de bool() sobre cadenas/números/listas
        return None
    hay = crudo
    termino = datos.get("termino")
    if termino is not None and not isinstance(termino, str):
        return None
    termino = re.sub(r"\s+", " ", termino).strip(" .,;:!?") if termino else None
    termino = termino or None
    # Coherencia: una consulta necesita término; un acto de consulta no puede venir
    # con hay_consulta=False.
    if hay and not termino:
        return None
    if acto == "consulta_medicamento" and not hay:
        return None
    if not hay and acto == "consulta_medicamento":
        return None
    confianza = datos.get("confianza")
    if not isinstance(confianza, (int, float)) or isinstance(confianza, bool):
        return None
    confianza = float(confianza)
    if not math.isfinite(confianza) or not 0.0 <= confianza <= 1.0:
        return None
    motivo = datos.get("motivo")
    motivo = motivo.strip()[:120] if isinstance(motivo, str) else ""
    return Entendimiento(
        acto=acto,
        hay_consulta=hay,
        termino=termino if hay else None,
        confianza=confianza,
        motivo=motivo,
        fuente="capa",
    )


# Por debajo de esta confianza el veredicto NO decide: el turno pregunta.
# (Revisión externa: «la confianza era decorativa; una negativa con confianza cero
# seguía siendo concluyente».)
CONFIANZA_MINIMA = 0.35

# Circuito de corte: si el proveedor de la capa falla N veces seguidas, se deja de
# llamar durante un enfriamiento (evita castigar cada turno con un timeout largo).
_FALLOS_SEGUIDOS = 0
_CORTE_HASTA = 0.0
_FALLOS_PARA_CORTE = 3
_ENFRIAMIENTO_S = 120.0

_CLIENTES: dict[tuple[str, str, str], Any] = {}


def cliente_intencion(settings: Any, llm_chat: Any) -> Any:
    """Cliente de la capa: si no hay `INTENT_MODEL`, es el mismo del chat.

    Mismo patrón que `VISION_*` del árbol del cliente: la etapa puede vivir en otro
    proveedor (barato/rápido) sin tocar el modelo del chat.
    """
    modelo = str(getattr(settings, "intent_model", "") or "").strip()
    if not modelo:
        return llm_chat
    # Clave con la configuración EFECTIVA (la que de verdad usa el cliente): si no,
    # dos configuraciones con distinto `openai_*` reutilizaban el cliente equivocado.
    api_key = str(getattr(settings, "intent_api_key", "") or "") or str(
        getattr(settings, "openai_api_key", "") or ""
    )
    base = str(getattr(settings, "intent_base_url", "") or "") or str(
        getattr(settings, "openai_base_url", "") or ""
    )
    clave = (modelo, api_key, base)
    if clave not in _CLIENTES:
        _CLIENTES[clave] = OpenAiLlm(api_key, modelo, base_url=base or None)
    return _CLIENTES[clave]


def _contexto(texto: str, termino_candidato: str | None, producto_previo: str | None,
              estado: str | None = None, candidato_obligatorio: bool = False) -> str:
    partes = [f"Mensaje del cliente: {texto.strip()}"]
    if producto_previo:
        partes.append(f"Producto que el asistente acaba de mostrar: {producto_previo}")
    if estado:
        # Estado conversacional compacto (últimos turnos, última lista, pregunta
        # pendiente): sin esto la elipsis y el cambio de producto se juzgan a ciegas.
        partes.append(f"Estado de la conversación:\n{estado.strip()}")
    if termino_candidato:
        partes.append(
            f"Término candidato extraído por el sistema (puede ser basura): {termino_candidato}"
        )
    if candidato_obligatorio:
        # F15.1 — el asistente quiere BUSCAR ese término: el veredicto tiene que ser sobre
        # ÉL, no sobre el mensaje en general. Sin esta pregunta explícita la capa aprobaba
        # «hay una consulta en el mensaje» (la del cliente) y la puerta aplicaba ese
        # veredicto al término del modelo: entraron al catálogo `oiga`, `firma`,
        # `maria fernandez cmp 45231`, `medica`.
        partes.append(
            "PREGUNTA ESTRICTA: el asistente quiere BUSCAR ese término candidato. El "
            "veredicto es sobre ÉL, no sobre el mensaje en general: hay_consulta=true SOLO "
            "si el cliente pidió o describió ESE medicamento (o es el producto mostrado, o "
            "un nombre mal escrito / variante del mismo). Si el candidato es relleno, un "
            "documento, un dato del cliente o de la receta, hay_consulta=false con "
            "termino=null — aunque el mensaje traiga otra consulta distinta."
        )
    partes.append("Responde SOLO con el JSON.")
    return "\n".join(partes)


async def entender(
    texto: str,
    *,
    llm: Any,
    settings: Any,
    termino_candidato: str | None = None,
    producto_previo: str | None = None,
    estado: str | None = None,
    candidato_obligatorio: bool = False,
) -> Entendimiento:
    """Veredicto sobre el mensaje: camino rápido → capa LLM → inconcluyente.

    `hay_consulta=None` significa **no concluyente**: el llamador NO debe buscar
    (antes reaplicaba el heurístico de forma, que era justamente el que buscaba
    basura — revisión externa: «fail-open, no fail-safe»).

    `candidato_obligatorio=True` (F15.1, puerta de herramientas): el veredicto se pide
    sobre el TÉRMINO candidato, no sobre el mensaje. Es la diferencia que cierra el hueco
    de `oiga` / `firma` / `maria fernandez cmp 45231`: el mensaje traía una consulta real
    (fexofenasina, una receta) y ese «sí» terminaba autorizando el término que el modelo
    eligió por su cuenta.
    """
    global _FALLOS_SEGUIDOS, _CORTE_HASTA
    if not bool(getattr(settings, "intent_layer", True)):
        return Entendimiento(acto="otro", hay_consulta=None, motivo="capa desactivada",
                             fuente="desactivada")
    if not texto or not texto.strip():
        return Entendimiento(acto="otro", hay_consulta=False, motivo="mensaje vacío",
                             fuente="desvio")
    rapido = camino_rapido(texto, termino_candidato)
    if rapido is not None:
        return rapido
    # Circuito de corte: si el proveedor viene fallando, no se paga el timeout en
    # cada turno (el turno pregunta en vez de buscar).
    ahora = time.monotonic()
    if ahora < _CORTE_HASTA:
        return Entendimiento(hay_consulta=None, motivo="circuito de corte activo",
                             fuente="fail-safe")
    try:
        timeout = float(getattr(settings, "intent_timeout", 12) or 12)
    except (TypeError, ValueError):
        timeout = 12.0
    if timeout <= 0:  # 0 o negativo no es un timeout válido: se usa el default
        timeout = 12.0
    try:
        cliente = cliente_intencion(settings, llm)
    except Exception as exc:  # construcción del cliente (config inválida)
        logger.warning("capa de comprensión: cliente inválido (%s)", exc)
        return Entendimiento(hay_consulta=None, motivo=f"cliente inválido: {exc}"[:120],
                             fuente="fail-safe")
    if cliente is None:
        return Entendimiento(hay_consulta=None, motivo="sin cliente LLM", fuente="fail-safe")
    try:
        reply = await asyncio.wait_for(
            cliente.complete(
                [
                    {"role": "system", "content": _SISTEMA},
                    {"role": "user", "content": _contexto(texto, termino_candidato,
                                                          producto_previo, estado,
                                                          candidato_obligatorio)},
                ]
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        _FALLOS_SEGUIDOS += 1
        _CORTE_HASTA = ahora + _ENFRIAMIENTO_S if _FALLOS_SEGUIDOS >= _FALLOS_PARA_CORTE else _CORTE_HASTA
        logger.warning("capa de comprensión: timeout de %.0fs — no concluyente", timeout)
        return Entendimiento(hay_consulta=None, motivo="timeout", fuente="fail-safe")
    except Exception as exc:  # proveedor caído, cuota, red
        _FALLOS_SEGUIDOS += 1
        _CORTE_HASTA = ahora + _ENFRIAMIENTO_S if _FALLOS_SEGUIDOS >= _FALLOS_PARA_CORTE else _CORTE_HASTA
        logger.warning("capa de comprensión: fallo (%s) — no concluyente", exc)
        return Entendimiento(hay_consulta=None, motivo=f"fallo: {exc}"[:120], fuente="fail-safe")
    _FALLOS_SEGUIDOS = 0

    datos = _extraer_json(getattr(reply, "content", "") or "")
    if not datos:
        logger.warning("capa de comprensión: respuesta sin JSON válido — no concluyente: %r",
                       (getattr(reply, "content", "") or "")[:200])
        return Entendimiento(hay_consulta=None, motivo="JSON inválido", fuente="fail-safe")
    veredicto = _veredicto_desde_json(datos)
    if veredicto is None:
        logger.warning("capa de comprensión: contrato inválido o incoherente — no concluyente: %r",
                       str(datos)[:200])
        return Entendimiento(hay_consulta=None, motivo="contrato inválido", fuente="fail-safe")
    if veredicto.confianza < CONFIANZA_MINIMA:
        # La confianza decide: una negativa (o afirmación) con poca confianza no
        # autoriza nada; el turno pregunta.
        logger.info("capa de comprensión: confianza %.2f < %.2f — no concluyente (%s)",
                    veredicto.confianza, CONFIANZA_MINIMA, veredicto.motivo)
        veredicto.hay_consulta = None
        veredicto.motivo = f"confianza baja ({veredicto.confianza:.2f}): {veredicto.motivo}"[:120]
        veredicto.fuente = "fail-safe"
        return veredicto
    logger.info(
        "capa de comprensión: acto=%s consulta=%s termino=%r confianza=%.2f (%s)",
        veredicto.acto, veredicto.hay_consulta, veredicto.termino,
        veredicto.confianza, veredicto.motivo,
    )
    return veredicto
