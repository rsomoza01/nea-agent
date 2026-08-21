# Research — Rol farmacéutico del agente Nea

**Decision**: El agente consulta el catálogo de medicamentos del tenant a través del CRM (`/api/bot/products`, `/api/bot/providers`), no directamente a Firebase. El `providerId` vive en env (`PROVIDER_ID`).

**Rationale**:
- El CRM ya expone el gateway `/api/bot/*` y tiene la service account de Firebase. El agente no necesita credenciales de Firebase; consulta al CRM (que ya filtra por `providerId`).
- El carrito se persiste en el Postgres propio de `nea-agent` (nueva tabla), no en el CRM.
- El OCR de recetas usa el LLM configurado (OpenRouter) con un modelo con visión, no OpenAI.

**Alternatives considered**:
- Consultar Firebase directamente desde nea-agent → rechazado (duplicaría credenciales y saltaría el aislamiento del CRM).
- Persistir carrito en el CRM → rechazado (el estado del pedido es local al agente; el CRM solo recibe el pedido finalizado).

## OCR de recetas

- Cuando llega una foto, se descarga el binario (vía `CrmClient.get_media` ya existente).
- Se envía la imagen al LLM configurado (OpenRouter) con un modelo con visión para extraer los medicamentos.
- Los medicamentos extraídos se pasan a `buscar_medicamento` (multi-medicina).
- Sanitización: normalizar `retadar`→`retard`, sales, formas farmacéuticas (reglas Gentefarma).

## Carrito (FR-8)

- Nueva tabla `bot_cart` en Postgres, por conversación.
- Tools: `agregar_al_carrito` (opción + cantidad), `ver_resumen`, `finalizar_pedido`.
- `LISTO` → resumen + finalizar (registrar en CRM, FR-9).

## Reglas de negocio (Gentefarma)

- Cargar `docs/gentefarma-reglas.md` como knowledge base (BRIEF_PATH) o KB del CRM.
- Incluye: intención, carrito, OCR, mensajes, precio USD/Bs con tasa BCV, sin fee.
