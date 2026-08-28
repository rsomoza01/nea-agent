"""Detector determinista de hostilidad sostenida (spec 002, US2-6 / AC-18).

Por qué existe: la regla de negocio "al tercer mensaje hostil → handoff" exige
CONTAR entre turnos, y eso es justo lo que un LLM hace de forma no confiable
(en el bench salió flaky: a veces contaba, a veces seguía vendiendo). El
conteo es determinista aquí; el LLM solo pone la redacción del cierre. El
handoff al tercer strike lo GARANTIZA turn.py aunque el modelo no llame la
herramienta.

El léxico es deliberadamente conservador: agresión DIRIGIDA y desprecio
(insultos, mentadas, acusaciones de fraude), no coloquialismos. "no mames" o
"qué pedo" solos NO cuentan — así habla México. El costo de un falso
positivo es bajo de todas formas: handoff = alerta al dueño, que puede
reactivar la IA con un clic.
"""
from __future__ import annotations

import re

HOSTILE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"vete (mucho )?a la (verga|chingada|mierda|fregada)",
        r"chinga (tu|su) madre|chinguen a su madre|vale (verga|madre)",
        r"\bpendej[oa]s?\b|\bimb[eé]cil(es)?\b|\best[uú]pid[oa]s?\b|\bidiotas?\b",
        r"\bestafador(es)?\b|\bestafas?\b|\bfraudes?\b|\brater[oa]s?\b|\bratas?\b|\brob[oa]s?\b",
        r"\bchafas?\b|\bbasura\b|\bporquer[ií]a\b|\bmierdas?\b|\bculer[oa]s?\b",
        r"p[ií]nches?\s+\w+",
        r"puro humo|pura mentira|puros? cuentos?|es una farsa",
    )
)

# Alerta que se inyecta como mensaje de sistema en el turno del tercer strike.
ALERT = (
    "ALERTA DEL SISTEMA (esto NO lo escribió el lead): detectado el TERCER "
    "mensaje hostil seguido. En ESTE turno tu respuesta es únicamente UNA "
    "línea digna de cierre — sin invitación, sin pitch, sin pregunta — y "
    "llamas la herramienta handoff con razón \"hostilidad\". La conversación "
    "pasa al dueño del negocio para que él decida (responder, ignorar o bloquear)."
)


def is_hostile(text: str) -> bool:
    return any(p.search(text) for p in HOSTILE_PATTERNS)


def hostile_streak(user_texts: list[str]) -> int:
    """Mensajes hostiles CONSECUTIVOS al final de la conversación del lead.

    Un mensaje no-hostil corta la racha (el lead que se calmó vuelve a
    empezar de cero — la regla es por hostilidad SOSTENIDA).
    """
    streak = 0
    for text in reversed(user_texts):
        if is_hostile(text or ""):
            streak += 1
        else:
            break
    return streak
