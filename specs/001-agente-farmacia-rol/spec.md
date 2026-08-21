# Feature Specification: Rol farmacéutico y herramientas de catálogo para el agente Nea

**Feature Branch**: `001-agente-farmacia-rol`

**Created**: 2026-08-20

**Status**: Draft

**Carril**: Ciclo completo (obligatorio: cambia el system prompt del agente y las herramientas, y consume un contrato del CRM `/api/bot/products`)

**Input**: Transformar `nea-agent` en un farmacéutico virtual por cada cliente (tenant), que responda consultas de disponibilidad y mejor precio de medicamentos consultando el catálogo del negocio en Firebase (vía el CRM), sin inventar precios, y con el conocimiento de las reglas de negocio del bot Gentefarma.

---

## Resumen

`nea-agent` pasa de ser un agente de agendamiento de citas a un **farmacéutico virtual**. Su rol (system prompt) se reorienta para atender consultas de disponibilidad y precio de medicamentos. Se le añaden herramientas que consultan el catálogo del negocio (por el gateway `/api/bot/products` del CRM), y se carga el conocimiento de las reglas de negocio de Gentefarma (Markdown). Cada instancia atiende a UNA farmacia (tenant) con su `providerId`.

## Clarifications

### Session 2026-08-20

- Q: ¿Qué modelo usar para el OCR de recetas? → A: El LLM configurado (OpenRouter) con un modelo con visión (no depende de OpenAI).
- Q: ¿El agente gestiona un carrito de pedido? → A: Sí, carrito completo (selección, cantidades, resumen, LISTO) replicando Gentefarma.
- Q: ¿Dónde se persiste el carrito? → A: En la base de datos propia de `nea-agent` (Postgres).
- Q: ¿Cómo se finaliza el pedido? → A: Se registra en el CRM (conversación/lead + nota) y se notifica a un humano.
- Q: ¿Frescura del catálogo de precios? → A: En tiempo real, sin caché (o < 1 min).

---

## User Scenarios & Testing

### User Story 1 - Consulta de disponibilidad y precio (P1)

Un cliente pregunta por un medicamento (ej. "¿tienen losartán 50 mg?"). El agente identifica la intención, consulta el catálogo del tenant y responde disponibilidad y precio (USD y Bs).

**Why this priority**: Es el flujo principal de valor del agente.

**Independent Test**: Enviar una consulta de un medicamento presente en el catálogo y verificar que el agente responde disponibilidad y precio.

**Acceptance Scenarios**:
1. **Given** el catálogo tiene "losartán 50 mg", **When** el cliente pregunta por él, **Then** el agente responde disponibilidad y precio.
2. **Given** el medicamento no está en el catálogo, **When** el cliente pregunta, **Then** el agente responde honestamente que no está o escala.

### User Story 2 — Precio con genérico (P2)

**Why this priority**: segundo flujo de valor frecuente.

**Independent Test**: consulta de precio de un medicamento con genérico → el agente ofrece el genérico con su precio.

**Acceptance Scenarios**:
1. **Given** el medicamento tiene genérico, **When** el cliente pregunta el precio, **Then** el agente responde el precio y ofrece el genérico.

### User Story 3 — Receta por foto (OCR) (P2)

**Why this priority**: flujo valioso en farmacias.

**Acceptance Scenarios**:
1. **Given** el cliente envía foto de receta, **When** el agente la procesa (OCR), **Then** extrae y responde disponibilidad/precio de cada medicamento.
2. **Given** la receta tiene un medicamento fuera del catálogo, **When** el agente responde, **Then** lo dice con honestidad.

---

## Requirements

### Functional Requirements

- **FR-1**: El agente MUST responder consultas de disponibilidad y precio consultando el catálogo del tenant (por el CRM `/api/bot/products`), NO inventando precios.
- **FR-2**: El agente MUST tener herramientas: `buscar_medicamento`, `sugerir_generico`, `precio_por_droguero` (si el tenant tiene varios providers), `info_provider`. El catálogo se consulta en tiempo real (sin caché o caché < 1 min) para que el precio mostrado sea actual.
- **FR-3**: El agente MUST cargar el `providerId` de su tenant desde variable de entorno (`PROVIDER_ID`) y usarlo en las consultas al catálogo.
- **FR-4**: El agente MUST responder con las reglas de negocio de Gentefarma (intención, carrito, OCR, mensajes, precio USD/Bs con tasa BCV, sin fee).
- **FR-8**: El agente MUST gestionar un carrito de pedido completo, replicando las reglas de Gentefarma: seleccionar opciones del catálogo (por número), indicar cantidades ("quiero 2 cajas de la opción 3"), ver resumen con "LISTO", y acumular productos con sus cantidades y precios. El carrito se persiste en la base de datos propia de `nea-agent` (Postgres) como parte del estado de conversación.
- **FR-9**: Al confirmar el pedido (cliente dice "LISTO"), el agente MUST registrar el pedido en el CRM (vía `/api/bot/*`, p. ej. nota/conversación del lead) y notificar a un humano para que lo procese.
- **FR-5**: El agente MUST procesar recetas por foto (OCR) en el MVP, usando el proveedor LLM configurado (OpenRouter) con un modelo con capacidad de visión (no depende de OpenAI).
- **FR-6**: El agente MUST escalar a humano (`handoff`) ante duda médica seria, receta nueva, o medicamento fuera de catálogo.
- **FR-7**: El system prompt del agente MUST reorientarse de "agendador de citas" a "farmacéutico" (desactivar tools de agenda: propose_slots, book_session, reschedule_session).

### Key Entities

- **providerId**: id del tenant en Firebase.
- **Producto**: medicamento con nombre, presentación, laboratorio, precio, disponibilidad.
- **Provider**: farmacia con info (dirección, horario, ciudad).
- **Reglas de negocio (Gentefarma)**: conocimiento en Markdown.

## Success Criteria

### Measurable Outcomes

- **SC-001**: El cliente obtiene disponibilidad y precio de un medicamento del catálogo en la misma conversación.
- **SC-002**: El agente responde honestamente "no disponible" o escala a humano cuando el medicamento no está en el catálogo — 100% de los casos.
- **SC-003**: El agente consulta solo el catálogo de su `providerId` (aislamiento de tenant).
- **SC-004**: El agente no inventa precios (anti-alucinación).

## Assumptions

- El `nea-agent` se despliega por tenant (un instancia por farmacia), con `PROVIDER_ID` en env.
- El CRM (`vocero-crm`) expone `/api/bot/products` (spec 005-agente-farma-firebase).
- El precio se muestra en USD y Bs (con tasa BCV), sin fee.
- Las reglas de negocio de Gentefarma se cargan como knowledge base del agente (Markdown).
