# RESULTADO — Paquete 3 · `fix/g13-afirmacion-sin-accion`

**Objetivo:** el agente no afirma una acción que no ocurrió.
**Rama:** `fix/g13-afirmacion-sin-accion` (nueva, desde `desarrollo`) · **Entorno:** `.venv-test`, sin modelo real, sin Docker (coste 0).
**Autorización de chasis:** este paquete toca `app/guards.py` (detector + plantilla) y un `elif` en la cadena de vetos de `app/turn.py`, según `docs/SPEC-G13-afirmacion-sin-accion.md` §6 y la orden de cierre.

Las sondas de `eval/` **no se modificaron**.

---

## Diseño implementado (spec G13)

| Punto | Archivo · símbolo | Qué hace |
|---|---|---|
| Detector léxico | `app/guards.py` · `familia_accion_afirmada` + `G13_PEDIDO_REGISTRADO` / `G13_AGREGADO_AL_PEDIDO` / `G13_APARTADO_O_AGENDADO` | Devuelve la familia ('pedido'/'agregado'/'apartado') o None. **Guarda de negación** (`G13_NEGACION`: no puedo/se puede/hay/quedó/logré/alcancé/pude). |
| Señal de estado (aditiva) | `app/tools.py` · `runtime.pedido_finalizado`, `runtime.carrito_agregado_turno` | `pedido_finalizado=True` solo si `_finalizar_pedido` devuelve ok; `carrito_agregado_turno=True` en `_agregar_al_carrito` (add real) **y** en el add del backstop (dedup de `backstop_added_skus`). |
| Disparo | `app/turn.py` · `elif` tras G12 + `_g13_accion_ocurrio` | Veta **solo** si hay frase de familia **Y** la señal del turno dice que la acción NO ocurrió. La frase sola nunca veta. |
| Plantilla | `app/guards.py` · `TPL_G13_ACCION` | Constante del spec; no afirma nada nuevo, ofrece dejar el pedido listo. Log: `guarda G13: afirmó una acción que no ocurrió …`. |

**Lo que NO hace:** no juzga escalado (es G11), ni promesas de futuro, ni precios/stock/composición (G5/G6/G10). `apartado/agendado`: esta farmacia no tiene herramienta de agenda → la señal siempre es `False`.

---

## Validación

### Nivel 1 — detector (determinista)

```
familia_accion_afirmada:
  "Listo, quedó registrado."                                   -> pedido      (1 positivo real)
  "Tu pedido quedó registrado." / "Finalicé tu pedido." /
  "Cerré el pedido." / "El pedido quedó confirmado."           -> pedido
  "Ya lo agregué a tu pedido."                                 -> agregado
  "Te lo aparté." / "Queda apartado a tu nombre." / "Te agendé"-> apartado
  "No puedo dejarte esas 2 cajas ni cerrar el pedido."         -> None (negación)
  "No puedo cerrar el pedido con el carrito vacío."            -> None (negación)
  "Ya avisé al equipo." / "Ya le pasé tu caso al equipo."      -> None (handoff real = G11)
  "¿Qué medicamento buscas?" / "Tengo LOSARTAN 50 mg..."       -> None
```

**0 falsos positivos.** La sonda read-only `eval/g13-afirmacion-sin-accion/detector_g13.py` sobre el corpus **camino-real** local (12 textos del agente) da **0 coincidencias** en las 3 familias → ningún texto real dispara en falso.

### Nivel 2 — decisión de veto (frase de familia **Y** señal en False)

```
"Listo, quedó registrado."  + pedido_finalizado=False  -> VETA
"Listo, quedó registrado."  + pedido_finalizado=True   -> NO veta
"Ya lo agregué a tu pedido."+ carrito_agregado=True     -> NO veta
"No puedo cerrar el pedido con el carrito vacío."       -> NO veta (negación)
```

### Nivel 3 — punta a punta (`comprador_decidido`, carrito vacío)

```
cliente : Sí, eso es todo, finaliza mi pedido
modelo  : Listo, quedó registrado. 👋          (carrito VACÍO, sin finalizar_pedido ok)
al cliente (DESPUÉS de G13): plantilla TPL_G13_ACCION — ya NO afirma el registro
```

### pytest — ANTES / DESPUÉS (literal)

```
# ANTES (desarrollo):  166 passed
# DESPUÉS (esta rama): 178 passed   (166 existentes + 12 nuevos: tests/test_g13_accion.py)
```

`python -m py_compile app/*.py` → OK.

---

## Riesgo introducido

- Es el único paquete que toca el **chasis** (G1-G12 intocables + un `elif` nuevo). El veto es aditivo: va al final de la cadena, con señal de estado obligatoria y guarda de negación; en turnos conversacionales sin frase de familia nunca actúa.
- `carrito_agregado_turno` se enciende en las dos vías de add (modelo y backstop) para no vetar una afirmación verdadera ("ya lo agregué") cuando el add lo hizo el backstop.

## Qué NO se ejecutó (y por qué)

- **Las 2 corridas del Laboratorio (`/api/lab/runs`):** exigen stack Docker + API y consumen modelo (prohibido por coste); su transcript (donde vive el positivo real "Listo, quedó registrado.") no está local. El detector se validó contra las frases **documentadas en el spec** + el corpus camino-real local (0 FP).
- **Simulador con modelo real (`--solo`):** mismo motivo; el nivel 3 determinista reproduce el escenario `comprador_decidido` con carrito vacío sin modelo.

## Discrepancias con la orden / sondas

- Ninguna. El detector `eval/g13-afirmacion-sin-accion/detector_g13.py` no se tocó; no expone el positivo real localmente porque el transcript del Laboratorio no está disponible sin Docker — se reporta, no se maquilla.
