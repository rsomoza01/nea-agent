# Implementation Plan: Rol farmacéutico y herramientas de catálogo para el agente Nea

**Branch**: `001-agente-farmacia-rol` | **Date**: 2026-08-20 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-agente-farmacia-rol/spec.md`

## Summary

`nea-agent` pasa de agente de agendamiento de citas a **farmacéutico virtual**. Este plan (lado agente) implementa:

1. **Rol farmacéutico**: reorientar el system prompt (`app/prompt.py`) de "agendar cita" a "atender consultas de disponibilidad y precio de medicamentos". Desactivar tools de agenda.
2. **Herramientas de catálogo**: `buscar_medicamento`, `sugerir_generico`, `precio_por_droguero` (si el tenant tiene varios providers), `info_provider`. Consultan el catálogo vía el CRM (`/api/bot/products`, `/api/bot/providers`).
3. **Carrito de pedido** (FR-8): selección por número, cantidades, resumen con "LISTO", persistido en Postgres propio.
4. **Finalización de pedido** (FR-9): registrar en el CRM y notificar a humano.
5. **OCR de recetas** (FR-5): procesar foto de receta con el LLM configurado (OpenRouter con visión).
6. **Reglas de negocio de Gentefarma**: cargar como conocimiento (Markdown) — intención, carrito, OCR, mensajes, precio USD/Bs con tasa BCV, sin fee.

## Technical Context

**Language/Version**: Python 3.11, FastAPI

**Primary Dependencies**: `httpx` (CrmClient), `openai` (LLM), `asyncpg` (Postgres), `pydantic-settings`

**Storage**: PostgreSQL propio de `nea-agent` (tablas `bot_conversation`, `bot_message`, nueva tabla carrito)

**Testing**: `pytest` (con `respx` para mockear el CRM)

**Target Platform**: Railway (contenedor uvicorn, puerto 8000)

**Project Type**: Web service (FastAPI)

**Performance Goals**: Respuesta de consulta de medicamento en < 2s (query al CRM/Firebase).

**Constraints**: No inventar precios (fuente = catálogo del tenant vía CRM). Aislamiento por `providerId`. Degradación silenciosa (Constitución III de nea).

**Scale/Scope**: Una instancia por tenant; cada una con `PROVIDER_ID` en env.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Status | Notas |
|------|--------|-------|
| **I. No inventar** | ✅ | El agente consulta el catálogo por tool; nunca inventa precios. |
| **II. Aislamiento por tenant** | ✅ | `PROVIDER_ID` en env de cada instancia. |
| **III. Degradación silenciosa** | ✅ | Un fallo del CRM/LLM → turno silencioso + log, o escala. |
| **IV. Verificación** | ✅ | Tests `pytest` + verificación E2E. |
| **V. Spec antes de código** | ✅ | Ciclo completo. |

## Project Structure

### Documentation (this feature)

```text
specs/001-agente-farmacia-rol/
├── plan.md              # Este archivo
├── research.md
├── data-model.md
├── quickstart.md
└── contracts/README.md
```

### Source Code (repository root)

```text
app/
├── prompt.py            # MODIFICAR: chasis farmacéutico + contexto
├── tools.py             # MODIFICAR: añadir tools de catálogo/carrito, desactivar agenda
├── crm.py               # MODIFICAR: métodos get_products/get_providers
├── config.py            # MODIFICAR: PROVIDER_ID
├── db.py                # MODIFICAR: tabla carrito (migración 004)
└── llm.py               # MODIFICAR: soporte visión para OCR
migrations/
└── 004_cart.sql         # NUEVO: tabla carrito
docs/
└── gentefarma-reglas.md # NUEVO: reglas de negocio en Markdown
tests/
├── test_tools_farma.py  # NUEVO
└── test_prompt_farma.py # NUEVO
```

**Structure Decision**: single FastAPI app, patrón existente (app/ + tests/ + migrations/).
