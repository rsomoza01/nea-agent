"""Paquete 1 — bucle de herramientas: ningún turno de farmacia termina sin un
mensaje para el cliente.

Cierra D-A1 (coroutine sin await), D-A2 (agotar MAX_TOOL_ROUNDS sin texto),
D-A3 (re-fuerzo de búsqueda en bucle) y D-A4 (misma llamada repetida). Mide lo
que el cliente HABRÍA recibido (`ctx.lab_outbox`) con un LLM guionado y un
catálogo de mentira — sin modelo real ni Docker (coste 0).
"""
from __future__ import annotations

import asyncio

import pytest

from tests.harness_agente_p1 import correr, tc, txt

MAX_TOOL_ROUNDS = 5


def _run(nombre, guion, mensaje, errores=None):
    return asyncio.run(correr(nombre, guion, mensaje, errores=errores))


def _texto(res) -> str:
    return res["respuestas"][0] if res["respuestas"] else ""


# --- A1 — agotamiento con 6 búsquedas válidas distintas, nunca texto ---------
def test_a1_agotamiento_valido_hay_una_respuesta():
    guion = [tc(i, "buscar_medicamento", {"nombre": m}) for i, m in enumerate(
        ["losartan 50 mg", "metformina 850 mg", "ibuprofeno 400 mg",
         "omeprazol 20 mg", "amoxicilina 500 mg", "vitamina c"], 1)]
    res = _run("a1", guion, "busco losartan 50 mg")
    assert res["excepcion"] is None
    assert len(res["respuestas"]) == 1
    assert _texto(res).strip(), "el cliente se quedó sin respuesta (silencio)"
    assert "LOSARTAN" in _texto(res), "no menciona lo obtenido del catálogo"


# --- A2 — 6 búsquedas idénticas: corte por degeneración, con respuesta -------
def test_a2_llamadas_identicas_cortan_con_respuesta():
    guion = [tc(i, "buscar_medicamento", {"nombre": "losartan 50 mg"}) for i in range(1, 7)]
    res = _run("a2", guion, "busco losartan 50 mg")
    assert res["excepcion"] is None
    assert _texto(res).strip(), "repetir la misma llamada dejó al cliente en silencio"
    assert "LOSARTAN" in _texto(res)
    assert res["llamadas_llm"] < 6, "no cortó el bucle degenerado antes de agotar"


# --- A3 — 6 llamadas con arguments {}: respuesta, sin excepción, str ---------
def test_a3_argumentos_vacios_sin_excepcion(caplog):
    guion = [tc(i, "buscar_medicamento", {}) for i in range(1, 7)]
    with caplog.at_level("ERROR"):
        res = _run("a3", guion, "busco losartan 50 mg")
    assert res["excepcion"] is None, f"el turno reventó: {res['excepcion']}"
    assert _texto(res).strip()
    assert isinstance(_texto(res), str)
    # A3b — handle_flush no debe registrar 'reventó' (coroutine sin await).
    assert "reventó" not in caplog.text
    assert "coroutine" not in caplog.text


# --- A4 — 6 medicamentos, 1 búsqueda por ronda: nada obtenido se pierde ------
def test_a4_varios_medicamentos_no_se_pierde_lo_obtenido():
    meds = ["losartan 50 mg", "metformina 850 mg", "ibuprofeno 400 mg",
            "omeprazol 20 mg", "amoxicilina 500 mg", "vitamina c"]
    guion = [tc(i, "buscar_medicamento", {"nombre": m}) for i, m in enumerate(meds, 1)]
    res = _run("a4", guion,
               "necesito losartan, metformina, ibuprofeno, omeprazol, amoxicilina y vitamina c")
    assert res["excepcion"] is None
    texto = _texto(res)
    assert texto.strip()
    # Los 5 que el catálogo SÍ devolvió (MAX_TOOL_ROUNDS=5) están en el texto.
    for esperado in ("LOSARTAN", "METFORMINA", "IBUPROFENO", "OMEPRAZOL", "AMOXICILINA"):
        assert esperado in texto, f"{esperado} se perdió del respaldo"


# --- A5 — búsqueda + catálogo caído: no se inventa precio/stock -------------
def test_a5_catalogo_caido_limitacion_honesta_sin_inventar():
    guion = [tc(i, "buscar_medicamento", {"nombre": "losartan 50 mg"}) for i in range(1, 7)]
    res = _run("a5", guion, "busco losartan 50 mg", errores={"losartan"})
    assert res["excepcion"] is None
    texto = _texto(res)
    assert texto.strip(), "catálogo caído dejó al cliente en silencio"
    # No se inventa precio ni stock.
    assert "$" not in texto
    assert "Bs " not in texto
    assert "no tengo información confirmada" in texto.lower()


# --- A6 — 2 búsquedas OK + 1 fallida: lo respaldado se conserva, lo fallido
#         NO se fabrica (el catálogo cayó para ese término). ------------------
def test_a6_fallo_parcial_conserva_lo_respaldado():
    guion = [
        tc(1, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        tc(2, "buscar_medicamento", {"nombre": "metformina 850 mg"}),
        tc(3, "buscar_medicamento", {"nombre": "ibuprofeno 400 mg"}),
    ]  # las rondas 4-5 repiten la 3ª (ibuprofeno con catálogo caído) → corte
    res = _run("a6", guion, "necesito losartan 50 mg, metformina 850 mg e ibuprofeno 400 mg",
               errores={"ibuprofeno"})
    assert res["excepcion"] is None
    texto = _texto(res)
    # Lo respaldado por el catálogo se conserva.
    assert "LOSARTAN" in texto and "METFORMINA" in texto, "se perdió lo respaldado"
    # Lo fallido NO se fabrica: ibuprofeno (catálogo caído) no aparece como una
    # línea de producto con precio inventado (en el título puede aparecer el eco).
    lineas_producto = [l for l in texto.splitlines() if l.strip().startswith("💊")]
    assert not any("IBUPROFENO" in l for l in lineas_producto), "se fabricó el medicamento fallido"


# --- A7 — texto válido en la última ronda: se acepta, no se sustituye -------
def test_a7_texto_en_ultima_ronda_se_acepta():
    guion = [
        tc(1, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        tc(2, "buscar_medicamento", {"nombre": "losartan 100 mg"}),
        tc(3, "buscar_medicamento", {"nombre": "metformina 850 mg"}),
        tc(4, "buscar_medicamento", {"nombre": "ibuprofeno 400 mg"}),
        txt("Tengo LOSARTAN 50 mg y 100 mg. ¿Cuál te sirvo?"),
    ]
    res = _run("a7", guion, "busco losartan")
    assert res["excepcion"] is None
    assert "¿Cuál te sirvo?" in _texto(res), "se sustituyó el texto válido del modelo por respaldo"


# --- A8 — flujo normal: sin interferencia del mecanismo de protección -------
def test_a8_flujo_normal_sin_interferencia():
    guion = [
        tc(1, "buscar_medicamento", {"nombre": "losartan 50 mg"}),
        txt("Tengo LOSARTAN 50 mg x 30 tabletas. ¿Te la agrego?"),
    ]
    res = _run("a8", guion, "busco losartan 50 mg")
    assert res["excepcion"] is None
    assert "¿Te la agrego?" in _texto(res)


# --- A9 — D-A3: término no autorizado + texto: el backstop fuerza UNA vez ----
def test_a9_termino_no_autorizado_no_reitera_la_ronda():
    guion = [txt("¿Qué medicamento necesitas exactamente?")]
    res = _run("a9", guion, "cual es el principio activo del daflon?")
    assert res["excepcion"] is None
    assert _texto(res).strip(), "término no autorizado dejó al cliente en silencio"
    # No se re-forzó cada ronda hasta agotar: el turno salió con respuesta pronto.
    assert res["llamadas_llm"] < MAX_TOOL_ROUNDS
