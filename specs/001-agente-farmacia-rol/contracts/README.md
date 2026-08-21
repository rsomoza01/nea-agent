# Contracts — Rol farmacéutico del agente Nea

El agente consume dos endpoints del CRM (`vocero-crm`) para obtener el catálogo y la info del proveedor. No habla con Firebase directamente.

## GET /api/bot/products (del CRM)

Consulta el catálogo de medicamentos del tenant.

**Auth**: `X-API-Key: <CRM_BOT_API_KEY>` (enviado por `CrmClient`).

**Query**: `q`, `providerId` (= env `PROVIDER_ID`), `limit`.

**Response 200**:
```json
{
  "products": [
    {
      "productId": "string",
      "producto": "Losartán",
      "generico": "Losartán Potásico",
      "presentacion": "Tabletas 50 mg",
      "laboratorio": "Genfar",
      "precio": 12.5,
      "disponible": true,
      "requiereReceta": false
    }
  ],
  "provider": { "providerId": "xxx", "nombre": "Farmacia X" }
}
```

## GET /api/bot/providers (CRM)

**Response 200**:
```json
{ "provider": { "providerId": "xxx", "nombre": "Farmacia", "direccion": "...", "horario": "...", "ciudad": "..." } }
```

## Métodos en CrmClient (nea-agent)

- `get_products(q, provider_id=None, limit=8) -> list[dict]` → `GET /api/bot/products?q=...&providerId=...`.
- `get_providers(provider_id) -> dict` → `GET /api/bot/providers?providerId=...`.

## Tools del agente

- `buscar_medicamento({nombre})` → `get_products` → disponibilidad + precio.
- `sugerir_generico({nombre})` → busca por nombre genérico.
- `precio_por_droguero({productId})` → si el tenant tiene varios providers (fuera de MVP en la práctica, un solo provider).
- `info_provider({})` → `get_providers` → dirección/horario.
- `agregar_al_carrito({opcion, cantidad})` → carrito local.
- `ver_resumen({})` → resumen del carrito.
- `finalizar_pedido({})` → registra en CRM (FR-9) y limpia carrito.
- `handoff({reason})` → escalar a humano.

## Errores (CrmClient)

- 404 → `None` (catálogo/provider no existe).
- 409 → `CrmConflict` (window_closed, etc.).
- 5xx/red → `CrmError`.
