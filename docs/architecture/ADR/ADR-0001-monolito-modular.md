# ADR-0001 — Monolito modular, no microservicios

- **Estado:** Aceptada (diseño; no autoriza runtime)
- **Fecha:** 2026-11-10
- **Paso del plan:** 3 (diseño TO-BE)
- **Rama:** `docs/pharmacy-domain-design`
- **Relacionada con:** [`../TO-BE.md`](../TO-BE.md) §1, PLAN §1

## Contexto

NEA es hoy una app FastAPI con el dominio de farmacia concentrado en dos módulos
grandes (`app/turn.py` ≈8.300 LOC, `app/tools.py` ≈4.000 LOC) y un conjunto de
piezas ya bien definidas (`intencion.py`, `guards.py`, `state.py` con `Store` como
`Protocol`, `crm.py`, `llm.py`, workers). Ver [`../AS-IS.md`](../AS-IS.md) y
[`../DEPENDENCY-MAP.md`](../DEPENDENCY-MAP.md). El acoplamiento alto está
documentado (R1–R9 del mapa) pero **ningún ciclo rompe el arranque hoy**.

El objetivo de negocio es especializar el producto en farmacia sin detener la
operación ni la integración upstream pendiente.

## Decisión

Evolucionar hacia un **monolito modular con capas ligeras** dentro del mismo
proceso FastAPI/Python/PostgreSQL asíncrono. Se conservan: FastAPI, el proveedor
CRM actual (`CrmClient`), la interfaz LLM (`OpenAiLlm`), Postgres y los workers.

## Alternativas consideradas

| Alternativa | Por qué se descarta |
|---|---|
| Microservicios (catálogo, pedidos, clínico separados) | Introduce red, despliegue y consistencia distribuida sin evidencia de necesidad; el equipo y la base de código no lo justifican. |
| Reescritura total ("big bang") | Riesgo inaceptable sobre protecciones medidas (F14/F15/F15.1/G1–G13) y 252 tests. Imposible mantener paridad. |
| Framework autónomo de agentes | Daría autoridad de ejecución al LLM, en contra de [ADR-0002](ADR-0002-llm-sin-autoridad.md). |
| Dejar el monolito como está | No resuelve el God-module `turn.py` ni la frontera de seguridad repartida. |

## Consecuencias

- **Positivas:** riesgo bajo por extracción incremental; se reutilizan piezas
  probadas; cada PR es pequeño y reversible (PLAN §4, §6).
- **Negativas / coste:** la modularidad es por convención y disciplina de
  imports, no por frontera de proceso; requiere ADRs y revisión para no recrear
  un nuevo "service locator" en `application/`.
- **Seguimiento:** la estructura `app/{api,core,application,domain,infrastructure,safety}`
  es destino conceptual; no se crean carpetas vacías (PLAN §3.2).
