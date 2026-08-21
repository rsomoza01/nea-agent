# Quickstart — Validación del rol farmacéutico del agente Nea

> Guía de validación E2E del agente farmacéutico.

## Prerequisitos

- CRM `vocero-crm` desplegado con `providerId` configurado y `/api/bot/products` funcionando.
- `nea-agent` con env:
  - `CRM_BASE_URL` (apunta al CRM)
  - `CRM_BOT_API_KEY` (BOT_API_KEY del CRM)
  - `PROVIDER_ID` (providerId del tenant)
  - `OPENAI_BASE_URL` (OpenRouter) y `OPENAI_MODEL` con visión (para OCR)
- Postgres de nea-agent con migración 004 (carrito).

## Setup

```bash
pip install -r requirements.txt
# correr migraciones (el bot las aplica al arrancar)
uvicorn app.main:app --port 8000
```

## Validación

### 1. Consulta de disponibilidad y precio

Enviar por WhatsApp al número del agente: `"¿tienen losartán 50 mg?"`.

**Esperado**: el agente consulta `/api/bot/products` y responde disponibilidad + precio (USD y Bs).

### 2. Medicamento fuera de catálogo

`"¿tienen [medicamento inexistente]?"`.

**Esperado**: el agente responde honestamente que no está o escala (no inventa precio).

### 3. Carrito

`"quiero 2 cajas de la opción 3"` → agrega al carrito. `"LISTO"` → resumen + finaliza pedido (registra en CRM).

### 4. OCR de receta

Enviar foto de receta → el agente extrae los medicamentos y responde disponibilidad/precio de cada uno.

### 5. Precio USD/Bs sin fee

Verificar que el precio mostrado es el base del catálogo convertido a Bs con tasa BCV, sin cargo comercial.

## Resultado esperado (Success Criteria)

- **SC-001/002**: consulta responde disponibilidad/precio; ausente → no alucina.
- **SC-003**: consulta solo el `providerId` del tenant.
- **SC-004**: no inventa precios.
