# NEA — Roadmap de transición incremental (TO-BE)

> **Paso 3.** Plan de transición del AS-IS al TO-BE **conservando el monolito**:
> extracción incremental, cada paso un PR pequeño y reversible, con criterio de
> salida y paridad medida. Acompaña a [`TO-BE.md`](TO-BE.md). Alinea con el PLAN §4.
> **No modifica runtime.** El Paso 4 **requiere aprobación explícita** (prompt, PLAN §9).

---

## 1. Principios de transición

1. **Conservar el monolito** ([ADR-0001](ADR/ADR-0001-monolito-modular.md)); no mover
   todo de golpe; no crear carpetas vacías (PLAN §3.2).
2. **Compatibilidad por fachada/adaptador**: al extraer lógica, `turn.py`/`tools.py`
   quedan como fachadas que delegan, sin cambiar el comportamiento observable.
3. **Paridad medida**: sondas y fixtures existentes deben dar el mismo resultado;
   cero búsquedas adicionales, cero llamadas no autorizadas.
4. **No tocar protecciones** sin autorización específica con spec y probes
   (F14/F15/F15.1/G1–G13, concurrencia, idempotencia, aislamiento).
5. **Una rama por etapa**; partir del HEAD aprobado de `agent-qa` con SHA registrado;
   no mezclar con integración upstream ni features comerciales (PLAN §6).

---

## 2. Secuencia (del PLAN §4, con el diseño de este paso como insumo)

| Paso | Rama sugerida | Entregable | Criterio de salida | Bloquea |
|---:|---|---|---|---|
| 3 (este) | `docs/pharmacy-domain-design` | TO-BE, DOMAIN-MODEL, USE-CASES, POLICY-MATRIX, ADRs, este roadmap | cada caso de uso con actor, precondición, permiso, efecto, errores, auditoría y pruebas | Sí → Paso 4 |
| 4 | `refactor/application-contracts` | DTOs/resultados tipados; puertos mínimos (catálogo/repo/reloj) como adaptadores sobre `Store`/`CrmClient`/`OpenAiLlm`; tests de contrato | código nuevo invocable con deps explícitas; **252 tests siguen pasando**; sin tocar lógica de `turn`/`tools` | Sí |
| 5 | `refactor/pharmacy-catalog-usecase` | caso de uso de búsqueda que usa los mismos algoritmos; tool actual como adaptador | **paridad exacta** por fixtures/sondas; `_autorizar_busqueda` sigue siendo puerta única; cero búsqueda adicional | Sí |
| 6 | `refactor/turn-orchestrator` | orquestador pequeño; `turn.py` como fachada | paridad de tool-loop, concurrencia, backstops, orden de vetos; sin "god orchestrator" | Parcial |
| 7 | `feat/pharmacy-order-workflow` | máquina de estados preparar/confirmar; idempotency key; pruebas de idempotencia/concurrencia | un pedido no se confirma dos veces ni se comunica sin evidencia | Sí (pedidos avanzados) |
| 8 | `feat/clinical-handoff` | política clínica + estados de derivación | consultas sensibles llegan a humano; estado demostrable | Sí (antes de exponer clínico) |
| 9 | `feat/pharmacy-actor-policies` | matriz de roles + pruebas negativas de acceso cruzado | empleado no opera fuera de su farmacia/rol; cliente no invoca Staff | Sí (antes de Staff) |
| 10 | `refactor/infrastructure-boundaries` | contratos de adaptadores, métricas, resiliencia | fallos externos explícitos y recuperables; sin afirmaciones falsas | Transversal |
| 11 | `feat/pharmacy-staff-agent` | Staff Agent reutilizando dominio | Staff no duplica dominio ni hereda permisos Customer | No antes de Paso 9 |
| 12 | `release/pharmacy-architecture-v1` | suite completa + CRM sandbox + piloto | aprobación explícita del propietario | Sí (antes de producción) |

---

## 3. Qué habilita este Paso 3 para el Paso 4 (siguiente)

El Paso 4 (contratos de aplicación, **aditivo**) puede empezar con:

- **Resultados tipados** definidos en [`TO-BE.md`](TO-BE.md) §4 y
  [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §5 (mapeados a los dicts actuales).
- **Contrato de intención** formalizado desde `Entendimiento` (ampliación aditiva).
- **Puertos mínimos** sobre `Store`/`CrmClient`/`OpenAiLlm`, sin migración de datos
  ([ADR-0005](ADR/ADR-0005-puertos-sin-migracion.md)).
- **Casos de uso** con contrato de entrada/salida/errores ([`USE-CASES.md`](USE-CASES.md)).

Lo que el Paso 4 **no** debe hacer: mover la lógica compleja de `turn.py`/`tools.py`
(eso es Paso 5/6) ni tocar protecciones.

---

## 4. Riesgos de la transición y mitigación

| Riesgo (AS-IS/DEPENDENCY-MAP) | Mitigación en el roadmap |
|---|---|
| R1 God-module `turn.py` | extracción por responsabilidad única, funciones puras primero (Paso 6) |
| R3 ciclo latente `tools`↔`intencion` | no reempaquetar hasta que el puerto de intención esté definido (Paso 4) |
| R6 `MemoryStore`≠`PgStore` (analytics) | cerrar paridad al formalizar puerto (Paso 9/10) |
| R7 dos rutas de ingesta | misma política de actor/recurso en el Policy Engine (Paso 9) |
| R8 estado de pedido espejado | idempotency key + lectura fresca al confirmar (Paso 7) |
| Huecos de test (entrega, workers, finalizar) | añadir tests **antes o junto** a cada extracción (PLAN §6 regla 6) |

---

## 5. Gate mínimo por PR (PLAN §6)

- `python -m py_compile app/*.py`
- suite `pytest` completa
- tests nuevos del módulo
- sondas F14/F15/F15.1 y guardas afectadas
- QA de loop/handoff/pedido según alcance
- `pip check` si cambian dependencias
- informe `docs/architecture/PR-<rama>-VALIDATION.md`

Los casos no ejecutados se reportan **BLOCKED** o **NOT RUN**, nunca PASS.

> **Sobre la línea base de pruebas (no es cobertura completa).** Los **252 tests
> que pasan** ([`BASELINE.md`](BASELINE.md)) son la **línea base medida** en el
> Paso 1 bajo las limitaciones de ese entorno, **no una garantía de cobertura
> total** del comportamiento. La matriz [`BASELINE-TEST-MATRIX.md`](BASELINE-TEST-MATRIX.md)
> documenta explícitamente pruebas **BLOCKED** (filas 11–13: F14 conversacional
> contra CRM real, integración Docker/DB desechable, F14 simulador — requieren CRM
> o Docker no disponibles en local) y **NOT RUN** (fila 14: corpus F15 de 50
> frases, fuera del alcance del Paso 1). Esas pruebas **siguen pendientes**: cada
> PR debe reportarlas como BLOCKED/NOT RUN si no se ejecutan, **sin convertirlas en
> PASS**, y debe habilitar las que su alcance desbloquee (p. ej. CRM sandbox o DB
> desechable) antes de dar por cubierta la funcionalidad correspondiente.

---

## 6. Decisiones pendientes que condicionan el roadmap

| Pendiente (PLAN §8) | Paso bloqueado |
|---|---|
| q1/q2/q8 fuente de verdad de pedido/precio/stock | Paso 7 (modelo transaccional completo) |
| q3 identidad de empleado | Paso 9/11 (Staff real) |
| q6 proceso clínico humano | Paso 8 (derivación) |
| q7 agenda heredada necesaria | retiro/renombrado de flujos legados |
| q10 retención de datos | auditoría/observabilidad (Paso 10) |

Ninguna se resuelve en este paso; se arrastran como **🔸 PENDIENTE**.
