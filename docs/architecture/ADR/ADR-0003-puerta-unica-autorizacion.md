# ADR-0003 — Una sola puerta de autorización de búsqueda (por término), no una segunda

- **Estado:** Aceptada (diseño)
- **Fecha:** 2026-11-10
- **Paso del plan:** 3
- **Relacionada con:** [`../TO-BE.md`](../TO-BE.md) §7; [`../POLICY-MATRIX.md`](../POLICY-MATRIX.md); PLAN §2.3, §5(paso 5), punto 11 del prompt

## Contexto

Hoy existe **exactamente una** puerta que autoriza tocar el catálogo:
`tools._autorizar_busqueda` (`app/tools.py:2768`). Es autorización **por término,
no por turno** (`intent_terminos_autorizados`, `app/tools.py:2385,2754`):

- término ya aprobado → se reutiliza sin llamada (`:2791`);
- mismo producto con otro formato → hereda la aprobación (`:2796`);
- evidencia fuerte (dosis/forma) abre solo la primera búsqueda del turno (`:2825-2832`);
- resto → llama `intencion.entender(candidato_obligatorio=True)` (F15.1, `:2840`);
- **fail-closed**: si la capa de comprensión no carga o falla, se deniega
  (`_rechazar_por_fallo_comprension:2880` → `_rechazo_comprension:2894`).

F15 (`intencion.entender`) es la **comprensión**; F15.1 (`candidato_obligatorio`)
juzga el **término candidato**; `_autorizar_busqueda` es la **puerta** que combina
ambos con la memoria de términos autorizados.

## Decisión

El diseño TO-BE **conserva `_autorizar_busqueda` como la única puerta de
autorización de búsqueda**. El futuro `Policy Engine` (`core/policies/`):

1. **No reemplaza ni duplica** la decisión por término; la **envuelve**: añade las
   dimensiones que hoy no son puerta explícita (actor × acción × recurso × farmacia),
   y **delega** la autorización de búsqueda de catálogo en `_autorizar_busqueda`.
2. Expone la decisión como resultado tipado (`Autorizado` / `NoAutorizado` /
   `RequiereAclaracion`) sin cambiar el comportamiento fail-closed.

No se crea una segunda puerta para medicamentos. Cualquier caso de uso que busque
catálogo pasa por la misma función.

## Alternativas consideradas

| Alternativa | Por qué se descarta |
|---|---|
| Un `Policy Engine` que reautorice búsqueda en paralelo a F15.1 | Doble puerta = dos fuentes de verdad, riesgo de divergencia y de autorizaciones dobles (PLAN §2.3 lo prohíbe explícitamente). |
| Mover la autorización al prompt del LLM | Viola [ADR-0002](ADR-0002-llm-sin-autoridad.md). |
| Autorizar por turno en vez de por término | Regresión: reintroduce búsquedas basura que F15.1 cerró (`test_f15_intencion.py`, A9). |

## Consecuencias

- **Positivas:** la frontera de autorización de catálogo sigue siendo un solo
  punto auditable y ya probado.
- **Negativas:** el Policy Engine queda "asimétrico": para búsqueda delega, para
  otras acciones (carrito, pedido, Staff) decide él mismo. Debe documentarse para
  no confundir a futuros implementadores.
- **Prueba de no-regresión:** paridad exacta de `_autorizar_busqueda` (cero
  llamadas no autorizadas, ninguna búsqueda adicional) al extraer el caso de uso
  de catálogo en el Paso 5.
