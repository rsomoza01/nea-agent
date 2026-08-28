"""Detector determinista de conversación que no va a ningún lado (candado de cierre).

Por qué existe: hay hilos que se quedan dando vueltas — el lead contesta
"ok", "va", un emoji, o sigue platicando sin soltar nada del negocio — y Nea
seguía preguntando indefinidamente. Eso quema tokens, satura el inbox del
dueño y, sobre todo, se lee a necesitado: un negocio serio no persigue.

El conteo va aquí y no en el prompt por la misma razón que `hostility.py`:
contar entre turnos es justo lo que un LLM hace de forma no confiable. El
LLM solo pone la redacción del cierre; que el cierre OCURRA (y que después
haya silencio) lo garantiza `turn.py`.

Es deliberadamente conservador. Cerrarle a un lead vivo cuesta mucho más que
aguantarle un turno de más, así que los dos disparadores exigen evidencia
acumulada, no un mal mensaje suelto.
"""
from __future__ import annotations

import re

# Mensajes que no aportan nada: acuses, muletillas, risas, emojis sueltos.
# Ojo con lo que NO está aquí: "no", "no me interesa", "ahorita no" — un "no"
# es una respuesta clarísima y merece la salida elegante del prompt, no el
# candado. Y cualquier cosa larga se asume con contenido.
# Ojo con lo que tampoco está: los saludos. Un "hola" es una jugada legítima
# de conversación, y contándolo como vacío se podía matar un hilo en el tercer
# mensaje — demasiado pronto para un lead que apenas está entrando en calor.
_RELLENO = re.compile(
    r"^(ok(ay)?|oka|va|sale|ah|ajá|aja|mmm?|hmm?|eh|este|ya|bueno|"
    r"gracias|grax|(?:ja|je|ji|ha){2,}|lol|:v|👍|👌|🙏|😂|🤣|🙂|😅"
    r")[\s.!¡?¿,😀-🿿]*$",
    re.I,
)
# Solo emojis/puntuación: tampoco aporta.
_SIN_LETRAS = re.compile(r"^[^\w]+$", re.UNICODE)

MAX_CARACTERES_VACIO = 24  # arriba de esto asumimos que dijo algo

# Mensajes de relleno seguidos del lead que disparan el cierre.
RACHA_VACIA = 3
# Mensajes del lead sin que la conversación llegue nunca a agendar/DIY/handoff.
MAX_MENSAJES_SIN_AVANCE = 14

ALERTA = (
    "ALERTA DEL SISTEMA (esto NO lo escribió el lead): esta conversación ya no "
    "avanza. En ESTE turno tu respuesta es únicamente UNA línea cálida de "
    "cierre que deja la puerta abierta — sin pregunta, sin pitch, sin "
    "invitación a la cita, sin links. Algo del estilo de \"te dejo por aquí, "
    "cuando quieras retomarlo me escribes y seguimos\". Despídete con dignidad: "
    "no ruegues, no resumas la conversación y no ofrezcas nada más."
)


def es_relleno(text: str) -> bool:
    """¿El mensaje del lead no aporta absolutamente nada?"""
    limpio = (text or "").strip()
    if not limpio:
        return True
    if len(limpio) > MAX_CARACTERES_VACIO:
        return False
    return bool(_RELLENO.match(limpio) or _SIN_LETRAS.match(limpio))


def racha_vacia(user_texts: list[str]) -> int:
    """Mensajes de relleno CONSECUTIVOS al final del hilo del lead.

    Uno con contenido corta la racha: el lead que por fin soltó algo vuelve a
    empezar de cero.
    """
    racha = 0
    for text in reversed(user_texts):
        if es_relleno(text):
            racha += 1
        else:
            break
    return racha


def sin_rumbo(user_texts: list[str], fase: str) -> bool:
    """¿Toca cerrar amable y dejar de responder?

    Dos caminos, ambos con evidencia acumulada:
    - el lead lleva `RACHA_VACIA` mensajes seguidos sin decir nada, o
    - la conversación se estiró más de `MAX_MENSAJES_SIN_AVANCE` mensajes sin
      salir nunca del descubrimiento (ni agendando, ni DIY, ni handoff).
    """
    if fase in ("agendando", "cerrada"):
        return False  # ya hay rumbo: no es asunto del candado
    if racha_vacia(user_texts) >= RACHA_VACIA:
        return True
    return len(user_texts) >= MAX_MENSAJES_SIN_AVANCE
