# Spec G13 — Veto de «afirmar una acción que no ocurrió» (propuesta)

**Estado:** propuesto y **medido**, sin implementar. `app/guards.py` y la cadena de vetos de
`app/turn.py` no se tocan hasta que lo autorices (regla del proyecto: guardas G1-G12 intocables;
un veto nuevo es un cambio aditivo, pero va en el chasis).

**Qué problema resuelve:** el juez marca `alucinacion` cuando el agente **afirma una acción que no
ocurrió**. Caso medido hoy en el Laboratorio (imagen F15.1, `run_2j677cvumjmm7an8pqn2`),
`comprador_decidido`:

```
cliente : Sí, eso es todo, finaliza mi pedido
agente  : Listo, quedó registrado.  👋        ← carrito VACÍO y sin llamada a finalizar_pedido
```

En el log de esa corrida **no hay una sola línea de `finalizar_pedido`**: el modelo confirmó un
pedido que nunca existió. Es un rojo **visible al cliente** (el peor tipo) y estaba ya en la lista de
candidatos (G13, anotado desde F4/F9 como «afirmación sin tool call»).

## 1. Medición previa (obligatoria para un veto: sin falsos positivos no vale)

Sonda read-only: `eval/g13-afirmacion-sin-accion/detector_g13.py` sobre **66 textos del agente**
(las 2 corridas del Laboratorio + las respuestas del camino real):

| Familia | Coincidencias | ¿La acción había ocurrido? | Decisión |
|---|---|---|---|
| `escalado_ya_avisado` («ya avisé al equipo», «ya le pasé tu caso») | **2** (ambas en `pide_humano`) | **SÍ**: `POST /api/bot/handoff` 200 con reason «pidió humano» | **NO entra en G13** — 2 de 2 falsos positivos, y la promesa *sin* handoff ya la cubre **G11** |
| `pedido_registrado` («quedó registrado», «finalicé tu pedido», «cerré el pedido») | **2** | 1 es una **negación** legítima («no puedo dejarte esas 2 cajas ni cerrar el pedido») → falso positivo léxico; 1 es el positivo real («Listo, quedó registrado.») | **Entra**, con guarda de negación y señal de estado |
| `agregado_al_pedido` («ya lo agregué a tu pedido») | 0 | — | Entra (misma forma, coste cero) |
| `apartado_o_agendado` («te lo aparté», «te agendé») | 0 | — | Entra (esta farmacia no usa agenda: señal siempre False) |

Con el diseño mínimo (familias 2-4, guarda de negación, señal de estado): **1 positivo real y 0
falsos positivos** en los 66 textos medidos.

## 2. Diseño

1. **Detector léxico** (regex, en `guards.py`, junto a los demás): familias
   `pedido_registrado` / `agregado_al_pedido` / `apartado_o_agendado`.
   **Guarda de negación**: no dispara si el texto niega el hecho
   (`no puedo`, `no se puede`, `no hay`, `no quedó`, `no logré`, `no alcancé`).
2. **Señal de estado por familia** (en `tools.py`, aditiva, un flag por acción):
   - `runtime.pedido_finalizado` → `True` solo cuando `_finalizar_pedido` devuelve `ok`;
   - `runtime.carrito_agregado_turno` → `True` en `_agregar_al_carrito` y en el add del backstop;
   - apartado/agendado → la herramienta de agenda (no usada en esta farmacia).
3. **Condición de disparo**: frase de la familia **Y** señal en `False`. La frase sola **nunca**
   dispara (eso es lo que produce los falsos positivos de la tabla).
4. **Colocación**: tras G12, misma forma que el veto G12 (`elif` al final de la cadena), con
   **plantilla constante** en `guards.py` (`TPL_G13_ACCION`), sin inventar datos:
   *«No puedo confirmar que eso quedara registrado: en este chat no hay un pedido cerrado. Si
   quieres, lo dejamos listo ahora — dime el medicamento y cuántas cajas.»*
5. **Auditoría**: `logger.warning("guarda G13: afirmó una acción que no ocurrió …")`, igual que G4.

**Lo que NO hace:** no juzga el escalado (es G11), no juzga promesas de futuro («voy a revisarlo»),
no toca precios/stock/disponibilidad (G5/G6/G10) ni la composición (G5).

## 3. Validación (3 niveles)

1. **Unitario (determinista, sin modelo)**: el detector corre sobre los textos reales de las 2
   corridas + negaciones + casos legítimos. Objetivo: **1 positivo, 0 falsos positivos**; y las 2
   frases de `pide_humano` (handoff real) **no** disparan.
2. **Conversación**: escenario `comprador_decidido` con el arnés del simulador (`--solo`), mismo
   mensaje y mismo carrito vacío: el texto final debe dejar de afirmar el registro.
3. **Laboratorio A/B**: la corrida pareada pre-F15.1 vs F15.1 ya está en marcha (`eval/lab-ab-f151.sh`;
   resultados en `eval/lab-ab-f151-*.json`) y sirve de control del caso `comprador_decidido`.

## 4. Riesgos

| Riesgo | Contención |
|---|---|
| Falso positivo que borra una respuesta legítima («lo dejé listo» en otro sentido) | Señal de estado obligatoria + guarda de negación + plantilla que no afirma nada nuevo |
| El modelo dice la verdad y el flag no se enteró (p. ej. add hecho por el backstop) | El flag se enciende en **las dos** vías de add (modelo y backstop) |
| Efecto colateral en turnos sin carrito | El veto solo actúa si la frase de la familia aparece; en turnos conversacionales no hay frase |

## 5. Coste / beneficio

- **Beneficio**: elimina un rojo visible al cliente por corrida del Laboratorio (1 de los 3 rojos de
  hoy), de la misma familia que el «quedó registrado». No añade llamadas al modelo.
- **Coste**: dos líneas en la cadena de vetos + un flag por acción en el runtime.

## 6. Para implementar hace falta tu OK

Toca `app/guards.py` (plantilla + detector) y `app/turn.py` (un `elif` tras G12), que son **el
chasis** que hasta ahora se mantuvo intacto. Si lo autorizas: implemento el veto, añado sus tests
deterministas, paso el escenario por el simulador y lo valido con la corrida del Laboratorio ya en
marcha. Si prefieres no tocarlo, la alternativa sin chasis es una regla en el **resultado de
`finalizar_pedido`** (no en el prompt base), que es donde el agente sabe si el pedido existe.
