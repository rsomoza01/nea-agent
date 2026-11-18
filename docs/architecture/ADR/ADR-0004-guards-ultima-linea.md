# ADR-0004 — Las guardas G1–G13 son la última línea de defensa, no un autorizador

- **Estado:** Aceptada (diseño)
- **Fecha:** 2026-11-10
- **Paso del plan:** 3
- **Relacionada con:** [`../TO-BE.md`](../TO-BE.md) §6; [`../AS-IS.md`](../AS-IS.md) §3; PLAN §2 (fila `guards.py`), §2.3

## Contexto

`app/guards.py` (638 LOC) **detecta** con regex y **reemplaza** por plantilla
constante; la condición de estado y la aplicación viven en `run_turn`
(`app/turn.py:950-1098`). G13 además verifica que una acción afirmada haya
ocurrido de verdad (`_g13_accion_ocurrio`, `app/turn.py:254-262`). Los backstops
de `run_turn` (`:717-938`) reemplazan el texto del LLM por la lista real del
catálogo. Todo esto corre **al final del turno**, después de ejecutar las
herramientas.

Riesgo de diseño: tentación de convertir las guardas en el lugar donde se "decide"
qué puede pasar (un autorizador tardío).

## Decisión

En el TO-BE, la capa `safety/response_guards/` **mantiene su rol de veto de salida
(fail-safe de redacción)**: verifica que la respuesta sea fiel a la evidencia
producida por el caso de uso. **No** concede permisos ni habilita acciones; la
autorización ocurre antes, en `core/policies/` (ver [ADR-0003](ADR-0003-puerta-unica-autorizacion.md)).

G13 se modela como **validación de post-condición** (¿la acción afirmada tiene
evidencia de que ocurrió?), alimentada por las señales del caso de uso
(`carrito_agregado_turno`, `pedido_finalizado`), no como una puerta de entrada.

## Alternativas consideradas

| Alternativa | Por qué se descarta |
|---|---|
| Usar las guardas como autorizador (mover política a `guards.py`) | Mezcla "puedo hacerlo" con "lo redacté bien"; rompe la separación del PLAN §1 y duplica F15.1. |
| Eliminar guardas al introducir validación de dominio | Son defensa en profundidad contra el LLM; la validación de dominio y el veto de salida son capas distintas y ambas se conservan. |

## Consecuencias

- **Positivas:** separación clara "autorizar (antes) vs. validar resultado (en
  dominio) vs. vetar redacción (al final)". Cada capa es testeable aislada.
- **Negativas:** tres puntos de control que deben coordinarse; el orden de los
  vetos (cadena `if/elif` en `run_turn`) debe preservarse exactamente al extraer
  (PLAN §5 paso 6).
- **Invariante:** ningún veto habilita una acción; solo puede reemplazar texto y/o
  forzar handoff.
