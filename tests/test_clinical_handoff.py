"""Pruebas del módulo de seguridad clínica y derivación humana (Paso 8, UC-08).

Cubre, de forma determinista y sin red:
  - clasificador conservador: PRECISIÓN (positivos clínicos por categoría) y, sobre todo,
    que NO dispara en consultas comerciales (no debilita F14/F15/F15.1 ni G5/G6);
  - máquina de estados: transiciones permitidas y prohibidas;
  - caso de uso idempotente: derivación nueva vs. doble solicitud (anti-duplicado);
  - mapeo al motivo seguro del CRM (no se añade `clinico` al catálogo);
  - atención y reanudación (avance de estado por el proceso humano).
"""
from __future__ import annotations

import asyncio

import pytest

from app.application import clinical_handoff as ch
from app.crm import HANDOFF_REASONS, canonical_handoff_reason


# --------------------------------------------------------------- clasificador ---

CASOS_POSITIVOS = {
    ch.ClinicalCategory.SINTOMA_ALARMA: [
        "me duele el pecho y el brazo izquierdo",
        "no puedo respirar bien desde anoche",
    ],
    ch.ClinicalCategory.INTERACCION: [
        "¿Puedo tomar losartán junto con ibuprofeno?",
        "¿se pueden combinar el omeprazol con el clopidogrel?",
    ],
    ch.ClinicalCategory.DOSIS: [
        "¿Qué dosis de amoxicilina debo tomar?",
        "¿cada cuántas horas me tomo el paracetamol?",
    ],
    ch.ClinicalCategory.EMBARAZO_LACTANCIA: [
        "Estoy embarazada, ¿puedo tomar loratadina?",
        "estoy lactando, ¿me afecta este antibiótico?",
    ],
    ch.ClinicalCategory.PEDIATRIA: [
        "¿qué le doy a mi bebé para la fiebre?",
        "mi hijo de 2 años puede tomar este jarabe?",
    ],
    ch.ClinicalCategory.REACCION_ADVERSA: [
        "desde que tomo el enalapril me da mucha tos",
        "me dio alergia el antibiótico",
    ],
    ch.ClinicalCategory.CONTRAINDICACION: [
        "¿puedo tomar esto si tengo gastritis?",
        "¿es malo para el hígado?",
    ],
    ch.ClinicalCategory.DIAGNOSTICO: [
        "¿qué me tomo para esta gripe?",
        "me diagnosticaron hipertensión",
    ],
    ch.ClinicalCategory.SUSTITUCION_INDIVIDUALIZADA: [
        "¿puedo cambiar el enalapril por losartán?",
        "¿cuál es mejor para mí, el ibuprofeno o el naproxeno?",
    ],
}

# Consultas COMERCIALES normales: el clasificador NO debe derivarlas (fail-safe: solo
# deriva ante señal clínica clara). Un falso positivo aquí rompería el flujo de catálogo.
CASOS_NEGATIVOS = [
    "¿Tienen paracetamol?",
    "¿Cuánto cuesta la amoxicilina?",
    "precio del losartán 50 mg",
    "¿hay ibuprofeno disponible?",
    "quiero 2 cajas de losartán",
    "¿tienen el genérico de Daflon?",
    "necesito pañales para mi bebé",
    "buenas, ¿me das el precio de la vitamina C?",
    "¿para qué sirve el omeprazol?",
    "¿tienen jarabe para la tos de niños?",
    "¿me puedes apartar 3 cajas?",
    "¿tienen algo para el dolor de cabeza?",
    "quiero comprar enalapril y losartán",
    "hola, ¿están abiertos?",
    "¿cuántas cajas me llevo?",
    "LISTO, confirmo el pedido",
    "",
    None,
]


@pytest.mark.parametrize(
    "categoria,textos",
    [(c, ts) for c, ts in CASOS_POSITIVOS.items()],
)
def test_clasificador_detecta_categoria(categoria, textos):
    for t in textos:
        assert ch.clasificar(t) is categoria, f"{t!r} debió clasificar como {categoria}"


@pytest.mark.parametrize("texto", CASOS_NEGATIVOS)
def test_clasificador_no_dispara_en_comercial(texto):
    assert ch.clasificar(texto) is None, f"{texto!r} NO debe derivarse (falso positivo)"


def test_es_consulta_clinica_coincide_con_clasificar():
    assert ch.es_consulta_clinica("¿qué dosis me tomo?") is True
    assert ch.es_consulta_clinica("¿tienen paracetamol?") is False


# ---------------------------------------------------------- máquina de estados ---

def test_transiciones_permitidas():
    S = ch.ClinicalHandoffState
    assert ch.transicion_permitida(S.DETECTADO, S.SOLICITADO)
    assert ch.transicion_permitida(S.SOLICITADO, S.ASIGNADO)
    assert ch.transicion_permitida(S.ASIGNADO, S.ATENDIDO)
    assert ch.transicion_permitida(S.ATENDIDO, S.RETOMADO)
    assert ch.transicion_permitida(S.RETOMADO, S.SOLICITADO)  # nueva consulta reabre
    # Cierre posible desde cualquier estado abierto.
    assert ch.transicion_permitida(S.SOLICITADO, S.CERRADO)
    assert ch.transicion_permitida(S.ATENDIDO, S.CERRADO)


def test_transiciones_prohibidas():
    S = ch.ClinicalHandoffState
    assert not ch.transicion_permitida(S.DETECTADO, S.ATENDIDO)   # no se salta solicitado
    assert not ch.transicion_permitida(S.SOLICITADO, S.ATENDIDO)  # no se salta asignado
    assert not ch.transicion_permitida(S.CERRADO, S.SOLICITADO)   # terminal
    assert not ch.transicion_permitida(S.ATENDIDO, S.SOLICITADO)  # no retrocede sin retomar


def test_estados_abiertos_no_incluye_terminales():
    S = ch.ClinicalHandoffState
    assert S.CERRADO not in ch.ESTADOS_ABIERTOS
    assert S.RETOMADO not in ch.ESTADOS_ABIERTOS  # ya hubo atención; no bloquea otra consulta
    assert S.SOLICITADO in ch.ESTADOS_ABIERTOS


# --------------------------------------------------------------- mapeo CRM ---

def test_motivo_clinico_mapea_a_motivo_seguro_existente():
    # Decisión del propietario: NO añadir `clinico` al catálogo del CRM.
    assert "clinico" not in HANDOFF_REASONS
    assert ch.CRM_REASON_CLINICO in HANDOFF_REASONS
    # Un reason clínico se normaliza a un motivo del catálogo (no da 422, no se pierde).
    assert canonical_handoff_reason("clinico") == ch.CRM_REASON_CLINICO
    assert canonical_handoff_reason("clínico urgente") == ch.CRM_REASON_CLINICO


# ------------------------------------------------------------ caso de uso ---

def _store_memoria():
    from app.state import MemoryStore

    return MemoryStore()


def test_derivacion_nueva_crea_handoff_solicitado():
    store = _store_memoria()
    uc = ch.DerivarConsultaClinicaUseCase(store)
    res = asyncio.run(
        uc.ejecutar(conversation_id=1, category=ch.ClinicalCategory.DOSIS)
    )
    assert res.status is ch.ClinicalHandoffStatus.SOLICITADO
    assert res.category is ch.ClinicalCategory.DOSIS
    assert res.crm_reason == ch.CRM_REASON_CLINICO
    assert ch.LIMITACION_PROCESO_PENDIENTE in res.limitaciones
    abierto = asyncio.run(store.derivacion_abierta(1))
    assert abierto is not None and abierto.state is ch.ClinicalHandoffState.SOLICITADO


def test_doble_solicitud_no_duplica_handoff():
    # Anti-duplicado (PLAN §8 punto 4): un segundo mensaje clínico mientras se espera
    # al humano NO crea otro handoff; devuelve el existente como YA_ABIERTO.
    store = _store_memoria()
    uc = ch.DerivarConsultaClinicaUseCase(store)

    async def _flujo():
        r1 = await uc.ejecutar(conversation_id=7, category=ch.ClinicalCategory.INTERACCION)
        r2 = await uc.ejecutar(conversation_id=7, category=ch.ClinicalCategory.DIAGNOSTICO)
        return r1, r2

    r1, r2 = asyncio.run(_flujo())
    assert r1.status is ch.ClinicalHandoffStatus.SOLICITADO
    assert r2.status is ch.ClinicalHandoffStatus.YA_ABIERTO
    # Mismo caso: la categoría/clave de la PRIMERA derivación es la que queda.
    assert r2.clinical_handoff_id == r1.clinical_handoff_id
    assert r2.category is ch.ClinicalCategory.INTERACCION
    assert len(store.clinical_handoffs) == 1


def test_conversaciones_distintas_tienen_handoffs_distintos():
    store = _store_memoria()
    uc = ch.DerivarConsultaClinicaUseCase(store)

    async def _flujo():
        a = await uc.ejecutar(conversation_id=1, category=ch.ClinicalCategory.DOSIS)
        b = await uc.ejecutar(conversation_id=2, category=ch.ClinicalCategory.DOSIS)
        return a, b

    a, b = asyncio.run(_flujo())
    assert a.status is b.status is ch.ClinicalHandoffStatus.SOLICITADO
    assert a.clinical_handoff_id != b.clinical_handoff_id


def test_categoria_none_no_deriva():
    store = _store_memoria()
    uc = ch.DerivarConsultaClinicaUseCase(store)
    res = asyncio.run(uc.ejecutar(conversation_id=1, category=None))
    assert res.status is ch.ClinicalHandoffStatus.REQUIERE_ATENCION
    assert store.clinical_handoffs == {}


def test_atencion_y_reanudacion_de_conversacion():
    # Simula el proceso humano: solicitado → asignado → atendido → retomado. Tras
    # retomar, el caso ya NO está abierto, así que una consulta clínica nueva crea
    # otra derivación (reanudación correcta de la conversación).
    store = _store_memoria()
    uc = ch.DerivarConsultaClinicaUseCase(store)
    S = ch.ClinicalHandoffState

    async def _flujo():
        r1 = await uc.ejecutar(conversation_id=9, category=ch.ClinicalCategory.PEDIATRIA)
        hid = r1.clinical_handoff_id
        await store.avanzar_estado_derivacion(hid, S.ASIGNADO)
        await store.avanzar_estado_derivacion(hid, S.ATENDIDO)
        retomado = await store.avanzar_estado_derivacion(hid, S.RETOMADO)
        # Ya no hay derivación abierta tras retomar.
        sin_abierta = await store.derivacion_abierta(9)
        # Una consulta clínica nueva abre otra derivación.
        r2 = await uc.ejecutar(conversation_id=9, category=ch.ClinicalCategory.DOSIS)
        return retomado, sin_abierta, r2

    retomado, sin_abierta, r2 = asyncio.run(_flujo())
    assert retomado.state is S.RETOMADO
    assert sin_abierta is None
    assert r2.status is ch.ClinicalHandoffStatus.SOLICITADO
    assert r2.category is ch.ClinicalCategory.DOSIS


def test_transicion_invalida_en_store_levanta():
    store = _store_memoria()
    uc = ch.DerivarConsultaClinicaUseCase(store)
    S = ch.ClinicalHandoffState

    async def _flujo():
        r1 = await uc.ejecutar(conversation_id=3, category=ch.ClinicalCategory.DOSIS)
        # solicitado → atendido NO es válido (se salta asignado).
        await store.avanzar_estado_derivacion(r1.clinical_handoff_id, S.ATENDIDO)

    with pytest.raises(ValueError):
        asyncio.run(_flujo())


def test_store_memoria_satisface_el_puerto():
    # La MemoryStore real debe exponer los métodos que el caso de uso necesita.
    store = _store_memoria()
    assert hasattr(store, "reservar_derivacion")
    assert hasattr(store, "derivacion_abierta")
