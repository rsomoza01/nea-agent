# Tasks: Rol farmacéutico y herramientas de catálogo para el agente Nea

**Input**: Design documents from `/specs/001-agente-farmacia-rol/`

**Prerequisites**: plan.md, spec.md, data-model.md, contracts/, research.md, quickstart.md

**Tests**: Se incluyen tests unit de tools y prompt (patrón existente con `respx`).

**Organization**: Tasks grouped by user story.

## Format: `[ID] [P?] [Story] Description`

## Path Conventions

- Single FastAPI project: `app/`, `tests/`, `migrations/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Reglas de negocio de Gentefarma en Markdown + config `PROVIDER_ID`.

- [X] T001 Crear `docs/gentefarma-reglas.md` con las reglas de negocio de Gentefarma (intención, carrito, OCR, mensajes, precio USD/Bs con tasa BCV, sin fee) a partir de `server.js`/`SDD.md`.
- [X] T002 [P] Añadir `provider_id` a `app/config.py` (Settings: `provider_id: str = ""`).

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: Sin el acceso al catálogo vía CRM y la tabla de carrito, ninguna story funciona.

- [X] T003 Añadir métodos al CRM client en `app/crm.py`: `get_products(q, provider_id, limit)` → `GET /api/bot/products`, `get_providers(provider_id)` → `GET /api/bot/providers` (con manejo de errores tipado).
- [ ] T004 [P] Crear migración `migrations/004_cart.sql` — tabla `bot_cart` (conversation_id FK, producto, presentación, cantidad, precio_usd, precio_bs, product_id).
- [ ] T005 [P] Añadir métodos de carrito a `app/db.py`: `add_cart_item`, `get_cart`, `clear_cart`.
- [X] T006 Cargar las reglas de negocio: `docs/gentefarma-reglas.md` creado (BRIEF_PATH/KB se configura en el deploy del tenant).

**Checkpoint**: Fundación lista.

---

## Phase 3: User Story 1 — Consulta de disponibilidad y precio (Priority: P1) 🎯 MVP

**Goal**: El cliente pregunta por un medicamento y el agente responde disponibilidad y precio.

**Independent Test**: mensaje "¿tienen losartán 50 mg?" → el agente consulta `get_products` y responde.

### Tests for User Story 1

- [X] T007 [P] [US1] Test unit de `get_products` en `tests/test_tools_farma.py` (mock CRM con `respx`)

### Implementation for User Story 1

- [X] T008 [US1] Añadir tool `buscar_medicamento` (y schema) en `app/tools.py` que llame `get_products(provider_id)` y devuelva disponibilidad + precio.
- [X] T009 [US1] Reorientar el system prompt en `app/prompt.py`: de "agendar cita" a "farmacéutico" (identidad, consulta de disponibilidad/precio, NO inventar precios, escalar si fuera del catálogo).
- [X] T010 [US1] Desactivar tools de agenda (`propose_slots`, `book_session`, `reschedule_session`) del `TOOL_SCHEMAS` o de la ejecución en este perfil. (vía `active_tool_schemas(farmacia=True)`)

**Checkpoint**: US1 funcional — el agente responde disponibilidad/precio.

---

## Phase 4: User Story 2 — Genérico y precio USD/Bs (Priority: P2)

**Goal**: El agente ofrece el genérico y muestra precio USD y Bs (con tasa BCV, sin fee).

### Implementation for User Story 2

- [X] T011 [P] [US2] Añadir tool `sugerir_generico` en `app/tools.py` que llame a `get_products` con `q` sobre genérico.
- [ ] T012 [US2] Formatear precio USD y Bs (conversión por tasa BCV) en el render de respuesta del agente, sin cargo comercial.

**Checkpoint**: US1 + US2.

---

## Phase 5: User Story 3 — Receta por foto (OCR) (Priority: P2)

**Goal**: El agente procesa una foto de receta y responde disponibilidad/precio de cada medicamento.

**Requiere**: modelo LLM con visión (OpenRouter).

### Implementation for User Story 3

- [ ] **P** [US3] En `app/media.py` o `app/llm.py`, añadir extracción de medicamentos de imagen (OCR) usando el LLM configurado con visión.
- [ ] T014 [US3] Integrar el OCR en el flujo de turno: al detectar imagen de receta, extraer medicamentos y llamar `buscar_medicamento` (multi-medicina) en `app/turn.py`.

**Checkpoint**: US3 — el agente responde varios medicamentos de una receta.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T015 [P] Cargar `docs/gentefarma-reglas.md` como knowledge base del agente (BRIEF_PATH) y verificar que se inyecta en el prompt.
- [ ] T016 [P] Añadir logging de consultas de catálogo y de carrito (observabilidad).
- [ ] T017 Correr `pytest` (todos los tests existentes + nuevos).
- [ ] T018 Correr `quickstart.md` validación E2E (consulta, ausente, carrito, OCR, precio sin fee).

---

## Dependencies & Execution Order

### Phase Dependencies

- Setup (P1): sin dependencias.
- Foundational (P2): depende de Setup — BLOQUEA.
- US1 (P3): MVP.
- US2 (P4): tras US1.
- US3 (P5): tras US1.
- Polish (P6): tras todas.

### Parallel Opportunities

- Setup (T001, T002) paralelo.
- Fundacional (T003-T005) parcialmente paralelo.
- Tras Fundacional, US1 es MVP; US2 y US3 paralelos si hay equipo.

---

## Implementation Strategy

### MVP First (US1)

1. Setup + Fundacional (T001-T006)
2. US1 (T007-T010) → **VALIDA con test**
3. Deploy/demo MVP (consulta disponibilidad/precio)

### Incremental Delivery

1. Foundation → consulta básica.
2. +US2 → genérico/precio USD-Bs.
3. +US3 → OCR receta.

---

## Notes

- [P] = archivos distintos, sin dependencias.
- [Story] = traza a user story.
- Commit después de cada tarea o grupo lógico.
- El carrito (FR-8) y finalización (FR-9) son parte de US1/US2 del flujo de pedido; se añaden como tareas de Polish/integration si el dueño decide incluirlos en el MVP.
