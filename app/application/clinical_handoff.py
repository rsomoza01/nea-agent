"""Seguridad clínica y derivación humana (UC-08, Paso 8).

Formaliza, de forma **fail-closed**, la derivación de consultas clínicas a un humano
siguiendo [ADR-0006](../../docs/architecture/ADR/ADR-0006-handoff-clinico-fail-closed.md),
[USE-CASES.md](../../docs/architecture/USE-CASES.md) UC-08 y PLAN §7 "Seguridad clínica".

El objetivo del Paso 8 es **derivar de forma segura, no construir un médico virtual**:

  1. **Clasificación conservadora** de consultas clínicas (`clasificar`): diagnóstico,
     síntomas de alarma, dosis, interacciones, contraindicaciones, embarazo/lactancia,
     pediatría, reacciones adversas y sustituciones individualizadas. Es un detector
     determinista de ALTA PRECISIÓN sobre el texto del CLIENTE: ante una solicitud de
     consejo clínico explícita devuelve la categoría; ante una consulta comercial normal
     (precio, disponibilidad, "¿tienen X?") devuelve `None` y NO deriva. No consultar
     F14/F15/F15.1 ni el catálogo: este módulo no busca, no autoriza y no llama al LLM.
  2. **No genera** diagnóstico, prescripción ni cambios de tratamiento: la única salida
     segura ante una consulta clínica es **no aconsejar y derivar** (ADR-0002/ADR-0006).
  3. **Máquina de estados de derivación** (`ClinicalHandoffState`) con transiciones
     explícitas: detectado → solicitado → asignado → atendido → retomado/cerrado.
  4. **Anti-duplicado idempotente** (`ClinicalHandoffStore`): una sola derivación ABIERTA
     por conversación; un segundo mensaje clínico mientras se espera al humano NO crea
     otro handoff, devuelve el existente (y habilita un mensaje de estado autorizado).
  5. **Dato clínico mínimo**: se persiste la CATEGORÍA (enum acotado), nunca el texto
     clínico del cliente (PLAN §7 punto 5, [ADR-0006]). El `correlation_id` liga el caso.

**Lo que este módulo NO decide (owner-PENDIENTE, se mantiene fail-safe — PLAN §8 q6):**
  - El **proceso humano real** (quién atiende, SLA, canal) y el **profesional responsable**:
    no hay proceso documentado; se modela la máquina de estados pero las transiciones más
    allá de SOLICITADO las dispara el proceso humano real cuando exista.
  - **Timeout/reintento/escalamiento/cierre "según operación real"**: sin proceso aprobado
    se deja el default fail-safe (mientras el caso esté ABIERTO, el bot solo emite mensajes
    de recepción/estado autorizados; no reanuda consejo). Las duraciones quedan PENDIENTE.
  - Añadir el motivo `clinico` al catálogo del CRM (`HANDOFF_REASONS`): decisión del
    propietario de **no añadirlo todavía**; la derivación clínica se mapea a un motivo
    seguro ya existente (`cliente`) para no romper el contrato del CRM (un reason fuera de
    catálogo da 422 y pierde el handoff — cert. 002). La categoría clínica vive solo en el
    estado/auditoría interno de NEA.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class ClinicalCategory(str, Enum):
    """Categorías de consulta clínica que SIEMPRE se derivan (PLAN §7 "Seguridad clínica").

    Son las categorías listadas por el plan como de derivación obligatoria. El bot no
    responde ninguna de forma autónoma: no diagnostica, no prescribe, no ajusta dosis ni
    sustituye tratamiento. El orden de evaluación en `clasificar` prioriza la urgencia
    (síntoma de alarma primero).
    """

    SINTOMA_ALARMA = "sintoma_alarma"
    DIAGNOSTICO = "diagnostico"
    DOSIS = "dosis"
    INTERACCION = "interaccion"
    CONTRAINDICACION = "contraindicacion"
    EMBARAZO_LACTANCIA = "embarazo_lactancia"
    PEDIATRIA = "pediatria"
    REACCION_ADVERSA = "reaccion_adversa"
    SUSTITUCION_INDIVIDUALIZADA = "sustitucion_individualizada"


# --------------------------------------------------------------- clasificador ---
# Detector determinista de ALTA PRECISIÓN. Diseño conservador: "conservador" aquí
# significa conservador respecto a la SEGURIDAD (derivar cuando la consulta es
# genuinamente de consejo clínico), NO disparar ante cualquier palabra de salud. Debe
# NO activarse en el flujo comercial ("¿tienen X?", "precio de X", "¿hay disponible?",
# "¿para qué sirve X?") para no debilitar F14/F15/F15.1 ni G5/G6. Ver pruebas negativas
# en tests/test_clinical_handoff.py.
#
# Cada patrón exige un marco de SOLICITUD DE CONSEJO (verbo de ingesta/permiso/pregunta
# clínica), no la mera mención de un fármaco o un síntoma dentro de una compra.

# Verbos de INGESTA (tomar algo), distintos de los de COMPRA (llevar/comprar/apartar):
# la pregunta clínica es sobre consumir, no adquirir.
_V_INGESTA = r"(?:tomar(?:me|lo|la|se)?|tomo|consumir|ingerir|administrar(?:le)?|us(?:ar|o)|aplicar(?:me|le)?)"

_PATRONES: tuple[tuple[ClinicalCategory, re.Pattern[str]], ...] = (
    # Síntomas de alarma: urgencia médica — se deriva de inmediato (máxima prioridad).
    (
        ClinicalCategory.SINTOMA_ALARMA,
        re.compile(
            r"dolor(?:es)?\s+(?:de\s+|en\s+el\s+)?pecho|(?:me\s+)?duele\s+(?:el\s+)?pecho"
            r"|no\s+puedo\s+respirar|dificultad\s+(?:para|al)\s+respirar|me\s+ahogo"
            r"|me\s+(?:desmay|desvanec)|p[eé]rdida\s+del?\s+conocimiento|me\s+desmay[eé]"
            r"|convulsi|sangrado\s+(?:abundante|que\s+no\s+para)|vomito\s+sangre|sangre\s+en"
            r"|parali(?:sis|zad)|no\s+siento\s+(?:el|la|medio)"
            r"|visi[oó]n\s+borrosa\s+(?:repentina|de\s+repente)|labios\s+morados",
            re.IGNORECASE,
        ),
    ),
    # Interacciones: "¿puedo tomar X con Y?", "¿se puede combinar/mezclar?".
    (
        ClinicalCategory.INTERACCION,
        re.compile(
            r"\binteracci[oó]n|\binteract[uú]a"
            r"|(?:puedo|se\s+puede|podr[ií]a|es\s+seguro)\s+" + _V_INGESTA +
            r"\b.{0,40}?\b(?:con\s+(?:el|la|mi|un|otro|la\s+)?\w|junto\s+con|al\s+mismo\s+tiempo\s+que|y\s+(?:el|la)\s+\w)"
            r"|(?:combinar|mezclar|juntar)\w*\s+.{0,30}?\bcon\b"
            r"|(?:es\s+seguro|puedo|se\s+puede|hay\s+(?:alg[uú]n\s+)?problema(?:\s+en)?)\s+.{0,25}?"
            r"(?:combinar|mezclar|junt|tomar)\w*\s+.{0,25}?\bcon\b"
            r"|se\s+pueden?\s+(?:combinar|mezclar|tomar\s+junt)",
            re.IGNORECASE,
        ),
    ),
    # Dosis: "¿qué dosis?", "¿cuánto me tomo?", "¿cada cuántas horas?", "sobredosis".
    (
        ClinicalCategory.DOSIS,
        re.compile(
            r"\bsobredosis\b|qu[eé]\s+dosis|cu[aá]l\s+es\s+la\s+dosis|la\s+dosis\s+(?:de|para|correcta|adecuada|recomendada)"
            r"|cada\s+cu[aá]nt[oa]s?\s+(?:horas?|d[ií]as?|tomo|debo|me\s+tomo|puedo)"
            r"|cu[aá]nt[oa]s?\s+(?:pastillas?|tabletas?|c[aá]psulas?|gotas?|ml|mg|veces?|cucharad)"
            r"\s*.{0,25}?\b(?:debo|tengo\s+que|me\s+tomo|tomo|al\s+d[ií]a|por\s+d[ií]a|puedo\s+" + _V_INGESTA + r")"
            r"|cu[aá]nt[oa]\s+(?:debo|tengo\s+que|me)\s+" + _V_INGESTA,
            re.IGNORECASE,
        ),
    ),
    # Embarazo / lactancia: mención del estado + marco de consumo/seguridad.
    (
        ClinicalCategory.EMBARAZO_LACTANCIA,
        re.compile(
            r"(?:embaraz|gestaci[oó]n|gestante|lactancia|lactando|amamant|dando\s+(?:pecho|de\s+(?:lactar|mamar)|teta))"
            r".{0,50}?(?:" + _V_INGESTA + r"|puedo|seguro|afecta|da[nñ]a|peligros|contraindicad|riesgo|bebe|beb[eé])"
            r"|(?:" + _V_INGESTA + r"|puedo|seguro|afecta|peligros|riesgo)"
            r".{0,50}?(?:embaraz|gestante|lactancia|lactando|amamant|dando\s+pecho)",
            re.IGNORECASE,
        ),
    ),
    # Pediatría: medicación para bebé/niño + marco de dosis/consumo (no "pañales para mi bebé").
    (
        ClinicalCategory.PEDIATRIA,
        re.compile(
            r"(?:mi\s+(?:beb[eé]|hij[oa]|ni[nñ][oa]|chamit[oa]|peque[nñ][oa])|"
            r"(?:para|a)\s+(?:un|mi|el|la)\s+(?:beb[eé]|ni[nñ][oa]|lactante|reci[eé]n\s+nacido)|"
            r"reci[eé]n\s+nacido|un\s+lactante|mi\s+peque)"
            r".{0,50}?(?:" + _V_INGESTA + r"|dosis|dar(?:le)?|puede\s+" + _V_INGESTA +
            r"|cu[aá]nt|le\s+doy|darle|se\s+le\s+da|fiebre|dolor|gripe|tos|infecci)"
            r"|(?:dosis|" + _V_INGESTA + r")\s+.{0,30}?(?:para|en)\s+(?:beb[eé]|ni[nñ][oa]s?|lactante|reci[eé]n\s+nacido)"
            r"|qu[eé]\s+le\s+(?:doy|puedo\s+dar)\s+.{0,20}?(?:beb[eé]|ni[nñ][oa]|hij)",
            re.IGNORECASE,
        ),
    ),
    # Reacciones adversas: efecto secundario / alergia / "me cayó mal".
    (
        ClinicalCategory.REACCION_ADVERSA,
        re.compile(
            r"efecto[s]?\s+(?:secundario|adverso|no\s+deseado)"
            r"|reacci[oó]n\s+(?:al[eé]rgica|adversa|secundaria)"
            r"|me\s+(?:cay[oó]|hizo|est[aá]\s+haciendo|sent[ií]|puse|pongo)\s+(?:mal|da[nñ]o|pesad)"
            r"|me\s+dio\s+(?:al[eé]rgia|alergia|rash|urticaria|sue[nñ]o|n[aá]usea|v[oó]mito|diarrea|mareo)"
            r"|me\s+(?:brot[eé]|salieron?|aparecieron?)\s+.{0,25}?(?:ronchas|granos|alergia|rojo|sarpullido|urticaria)"
            r"|desde\s+que\s+(?:tomo|tom[eé]|empec[eé]).{0,40}?(?:me\s+siento|tengo|me\s+da|me\s+duele)"
            r"|me\s+produce\s+(?:al[eé]rgia|sue[nñ]o|mareo|n[aá]usea)",
            re.IGNORECASE,
        ),
    ),
    # Contraindicaciones: "¿puedo tomar X si tengo [condición]?", "¿es malo para el hígado?".
    (
        ClinicalCategory.CONTRAINDICACION,
        re.compile(
            r"contraindicaci|est[aá]\s+contraindicad"
            r"|(?:puedo|se\s+puede|es\s+(?:malo|seguro|peligros)|hay\s+problema)\s+.{0,40}?"
            r"(?:si\s+(?:tengo|soy|padezco)|teniendo|con\s+(?:mi|la)\s+(?:presi[oó]n|az[uú]car|diabet|hipertens|gastritis|[uú]lcera))"
            r"|es\s+malo\s+(?:para|si)\s+(?:el|mi|la)\s+(?:h[ií]gado|ri[nñ][oó]n|coraz[oó]n|presi[oó]n|est[oó]mago)"
            r"|(?:soy|tengo)\s+(?:hipertens|diab[eé]tic|al[eé]rgic\w*\s+a|gastritis|[uú]lcera)\b.{0,40}?(?:puedo|" + _V_INGESTA + r")",
            re.IGNORECASE,
        ),
    ),
    # Diagnóstico: "¿qué tengo?", "¿será que tengo...?", "me diagnosticaron", "qué me tomo para".
    (
        ClinicalCategory.DIAGNOSTICO,
        re.compile(
            r"qu[eé]\s+(?:tengo|ser[aá]\s+que\s+tengo|me\s+estar[aá]\s+pasando)"
            r"|ser[aá]\s+que\s+(?:tengo|es|ser[aá])|ser[aá]n?\s+s[ií]ntomas"
            r"|tengo\s+(?:estos\s+)?s[ií]ntomas|estos\s+s[ií]ntomas"
            r"|me\s+diagnosticaron|me\s+detectaron"
            r"|esto\s+(?:es|ser[aá])\s+(?:normal|grave|peligroso|de\s+cuidado)"
            r"|qu[eé]\s+me\s+(?:tomo|puedo\s+tomar|recomiendas?\s+(?:tomar|para))\s+para\s+(?:el|la|mi|este|esta|estos|estas)\s+\w"
            r"|qu[eé]\s+(?:es\s+bueno|sirve|recomiendas?)\s+para\s+(?:el|la|mi)\s+(?:dolor|fiebre|gripe|tos|infecci|diarrea|gastritis|ansiedad|insomnio|presi)",
            re.IGNORECASE,
        ),
    ),
    # Sustitución individualizada: "¿puedo cambiar X por Y?", "¿cuál es mejor para mí?".
    # OJO: "¿tienen el genérico de X?" es COMERCIAL (sugerir_generico), NO clínico.
    (
        ClinicalCategory.SUSTITUCION_INDIVIDUALIZADA,
        re.compile(
            r"(?:puedo|podr[ií]a|conviene)\s+(?:cambiar|sustituir|reemplazar|cambiarme)\b.{0,40}?\bpor\b"
            r"|qu[eé]\s+(?:me\s+)?(?:recomiendas?|sugieres?|conviene)\s+(?:tomar\s+)?(?:en\s+vez|en\s+lugar|mejor\s+que|en\s+lugar\s+de)"
            r"|cu[aá]l\s+(?:es\s+)?(?:mejor|me\s+conviene|me\s+recomiendas?)\s+(?:para\s+m[ií]|en\s+mi\s+caso)"
            r"|cu[aá]l\s+(?:me\s+)?conviene\s+m[aá]s\s+(?:para\s+m[ií]|en\s+mi\s+caso)",
            re.IGNORECASE,
        ),
    ),
)


def clasificar(texto: str | None) -> ClinicalCategory | None:
    """Clasifica el texto del CLIENTE; devuelve la categoría clínica o `None`.

    Función PURA y determinista. Devuelve la primera categoría coincidente en orden de
    prioridad (síntoma de alarma primero). `None` significa "no es consulta clínica de
    consejo" → el flujo comercial sigue intacto (fail-safe: solo deriva ante señal clara).
    """
    if not texto:
        return None
    t = texto.strip()
    if not t:
        return None
    for categoria, patron in _PATRONES:
        if patron.search(t):
            return categoria
    return None


def es_consulta_clinica(texto: str | None) -> bool:
    """Atajo booleano de `clasificar` para los guards/wiring."""
    return clasificar(texto) is not None


# ------------------------------------------------------------- máquina de estados ---


class ClinicalHandoffState(str, Enum):
    """Estados de la derivación clínica (UC-08 / PLAN §8 punto 4).

    DETECTADO y SOLICITADO los controla NEA (el clasificador detecta y la derivación se
    solicita al humano vía la puerta de handoff existente). ASIGNADO → ATENDIDO →
    RETOMADO/CERRADO dependen del **proceso humano real**, hoy PENDIENTE (PLAN §8 q6): se
    modela el grafo pero esas transiciones las dispara el proceso cuando exista.
    """

    DETECTADO = "detectado"      # el clasificador marcó la consulta como clínica
    SOLICITADO = "solicitado"    # persistido + handoff pedido al humano (estado ABIERTO)
    ASIGNADO = "asignado"        # un responsable tomó el caso (proceso humano)
    ATENDIDO = "atendido"        # el humano respondió al cliente
    RETOMADO = "retomado"        # el bot reanuda la conversación tras la atención
    CERRADO = "cerrado"          # caso cerrado (terminal)


# Estados en los que la derivación sigue ABIERTA: el bot NO debe reanudar consejo y un
# nuevo mensaje clínico NO crea otro handoff (anti-duplicado).
ESTADOS_ABIERTOS: frozenset[ClinicalHandoffState] = frozenset(
    {
        ClinicalHandoffState.DETECTADO,
        ClinicalHandoffState.SOLICITADO,
        ClinicalHandoffState.ASIGNADO,
        ClinicalHandoffState.ATENDIDO,
    }
)

# Grafo explícito de transiciones permitidas (ninguna fase hereda permisos por accidente).
_TRANSICIONES: dict[ClinicalHandoffState, frozenset[ClinicalHandoffState]] = {
    ClinicalHandoffState.DETECTADO: frozenset({ClinicalHandoffState.SOLICITADO}),
    ClinicalHandoffState.SOLICITADO: frozenset(
        {ClinicalHandoffState.ASIGNADO, ClinicalHandoffState.CERRADO}
    ),
    ClinicalHandoffState.ASIGNADO: frozenset(
        {ClinicalHandoffState.ATENDIDO, ClinicalHandoffState.CERRADO}
    ),
    ClinicalHandoffState.ATENDIDO: frozenset(
        {ClinicalHandoffState.RETOMADO, ClinicalHandoffState.CERRADO}
    ),
    # Tras retomar, una consulta clínica nueva puede volver a solicitar derivación.
    ClinicalHandoffState.RETOMADO: frozenset(
        {ClinicalHandoffState.SOLICITADO, ClinicalHandoffState.CERRADO}
    ),
    ClinicalHandoffState.CERRADO: frozenset(),
}


def transicion_permitida(
    origen: ClinicalHandoffState, destino: ClinicalHandoffState
) -> bool:
    """¿Se permite pasar de `origen` a `destino`? (función pura, grafo explícito)."""
    return destino in _TRANSICIONES.get(origen, frozenset())


# Motivo seguro del CRM al que se mapea la derivación clínica (decisión del propietario:
# NO añadir `clinico` a HANDOFF_REASONS todavía). `cliente` está en el catálogo cerrado
# del CRM (no da 422) y es el bucket más fiel: el cliente necesita a un humano. La
# categoría clínica NO se envía al CRM; vive solo en el estado/auditoría de NEA.
CRM_REASON_CLINICO = "cliente"


class ClinicalHandoffStatus(str, Enum):
    """Resultado tipado de una solicitud de derivación clínica."""

    SOLICITADO = "solicitado"        # derivación nueva creada y pedida al humano
    YA_ABIERTO = "ya_abierto"        # ya había una derivación abierta → no se duplica
    REQUIERE_ATENCION = "requiere_atencion"  # no procede (p. ej. sin categoría)


@dataclass(frozen=True)
class ClinicalHandoff:
    """Una derivación clínica (dato clínico MÍNIMO: categoría + correlación, sin texto)."""

    clinical_handoff_id: str
    conversation_id: int
    category: ClinicalCategory
    state: ClinicalHandoffState = ClinicalHandoffState.SOLICITADO

    @property
    def abierto(self) -> bool:
        return self.state in ESTADOS_ABIERTOS


@dataclass(frozen=True)
class ClinicalHandoffResult:
    """Resultado de `DerivarConsultaClinicaUseCase.ejecutar`."""

    status: ClinicalHandoffStatus
    clinical_handoff_id: str | None = None
    category: ClinicalCategory | None = None
    crm_reason: str = CRM_REASON_CLINICO
    limitaciones: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


def nuevo_clinical_handoff_id() -> str:
    """UUID4 para un caso de derivación nuevo (sirve de correlation_id del caso)."""
    return str(uuid.uuid4())


# Limitación estándar: el proceso humano real de derivación clínica no está definido
# (PLAN §8 q6). NEA deriva y espera al humano; no afirma atención ni resuelve la consulta.
LIMITACION_PROCESO_PENDIENTE = "proceso_humano_derivacion_clinica_pendiente"


@runtime_checkable
class ClinicalHandoffStore(Protocol):
    """Capacidad de **reservar atómicamente** una derivación clínica ABIERTA por conversación.

    Contrato de `reservar_derivacion` (idempotente / anti-duplicado — PLAN §8 punto 4):
      - Sin derivación abierta para la conversación: crea una nueva (estado SOLICITADO) y
        la devuelve.
      - Con una derivación YA ABIERTA (detectado/solicitado/asignado/atendido): devuelve la
        existente **sin crear otra** — un segundo mensaje clínico mientras se espera al
        humano no duplica el handoff.
    Garantía de unicidad: un índice único parcial `(conversation_id) WHERE abierto`
    (ver migración 017) hace que `INSERT … ON CONFLICT DO NOTHING RETURNING` sea atómico.
    """

    async def reservar_derivacion(
        self,
        *,
        clinical_handoff_id: str,
        conversation_id: int,
        category: ClinicalCategory,
    ) -> ClinicalHandoff:
        ...

    async def derivacion_abierta(
        self, conversation_id: int
    ) -> ClinicalHandoff | None:
        ...

    async def avanzar_estado(
        self, clinical_handoff_id: str, destino: ClinicalHandoffState
    ) -> ClinicalHandoff | None:
        ...


class EnMemoriaClinicalHandoffStore:
    """Implementación de referencia en memoria del `ClinicalHandoffStore`.

    Especifica la semántica que replica `PgStore` con el índice único parcial + `ON
    CONFLICT DO NOTHING RETURNING`. Permite probar anti-duplicado, atención y reanudación
    sin Postgres.
    """

    def __init__(self) -> None:
        self._por_id: dict[str, ClinicalHandoff] = {}

    async def reservar_derivacion(
        self,
        *,
        clinical_handoff_id: str,
        conversation_id: int,
        category: ClinicalCategory,
    ) -> ClinicalHandoff:
        abierto = await self.derivacion_abierta(conversation_id)
        if abierto is not None:
            # ON CONFLICT DO NOTHING: ya hay una derivación abierta → no se duplica.
            return abierto
        h = ClinicalHandoff(
            clinical_handoff_id=clinical_handoff_id,
            conversation_id=conversation_id,
            category=category,
            state=ClinicalHandoffState.SOLICITADO,
        )
        self._por_id[clinical_handoff_id] = h
        return h

    async def derivacion_abierta(
        self, conversation_id: int
    ) -> ClinicalHandoff | None:
        for h in self._por_id.values():
            if h.conversation_id == conversation_id and h.abierto:
                return h
        return None

    async def avanzar_estado(
        self, clinical_handoff_id: str, destino: ClinicalHandoffState
    ) -> ClinicalHandoff | None:
        actual = self._por_id.get(clinical_handoff_id)
        if actual is None:
            return None
        if not transicion_permitida(actual.state, destino):
            raise ValueError(
                f"transición no permitida: {actual.state.value} → {destino.value}"
            )
        avanzado = ClinicalHandoff(
            clinical_handoff_id=actual.clinical_handoff_id,
            conversation_id=actual.conversation_id,
            category=actual.category,
            state=destino,
        )
        self._por_id[clinical_handoff_id] = avanzado
        return avanzado


class DerivarConsultaClinicaUseCase:
    """UC-08: deriva una consulta clínica a un humano de forma idempotente y fail-closed.

    Hace cumplir, sin tocar el LLM ni autorizar nada: (1) solo deriva ante una categoría
    clínica detectada; (2) anti-duplicado — una sola derivación abierta por conversación;
    (3) registra el dato clínico MÍNIMO (categoría + correlación, sin texto); (4) mapea al
    motivo seguro del CRM (`cliente`) sin afirmar que el humano ya atendió (G13/ADR-0006).

    Este caso de uso NO envía el handoff al CRM: eso lo hace la puerta existente
    (`turn._safe_handoff` → `crm.post_handoff`). Aquí solo se reserva/consulta el estado
    durable para anti-duplicado y auditoría. Separar la decisión (derivar) del efecto
    externo (post_handoff) mantiene el caso de uso puro y testeable.
    """

    def __init__(self, store: ClinicalHandoffStore) -> None:
        self._store = store

    async def ejecutar(
        self,
        *,
        conversation_id: int,
        category: ClinicalCategory | None,
    ) -> ClinicalHandoffResult:
        if category is None:
            return ClinicalHandoffResult(
                status=ClinicalHandoffStatus.REQUIERE_ATENCION
            )
        previo = await self._store.derivacion_abierta(conversation_id)
        handoff = await self._store.reservar_derivacion(
            clinical_handoff_id=nuevo_clinical_handoff_id(),
            conversation_id=conversation_id,
            category=category,
        )
        ya_existia = previo is not None
        return ClinicalHandoffResult(
            status=(
                ClinicalHandoffStatus.YA_ABIERTO
                if ya_existia
                else ClinicalHandoffStatus.SOLICITADO
            ),
            clinical_handoff_id=handoff.clinical_handoff_id,
            category=handoff.category,
            crm_reason=CRM_REASON_CLINICO,
            limitaciones=(LIMITACION_PROCESO_PENDIENTE,),
        )
