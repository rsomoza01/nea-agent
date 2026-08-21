# Data Model — Rol farmacéutico del agente Nea

## Entity: bot_cart (tabla nueva en Postgres, migración 004)

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `id` | BIGSERIAL PK | Identificador del item de carrito |
| `conversation_id` | BIGINT FK → bot_conversation | Conversación |
| `producto` | TEXT NOT NULL | Nombre del medicamento |
| `presentacion` | TEXT | Presentación |
| `cantidad` | INT NOT NULL DEFAULT 1 | Cantidad |
| `precio_usd` | NUMERIC | Precio unitario USD |
| `precio_bs` | NUMERIC | Precio unitario Bs (opcional) |
| `product_id` | TEXT | Id del producto en el catálogo |
| `created_at` | TIMESTAMPTZ | Fecha de alta |
| `updated_at` | TIMESTAMPTZ | Fecha de actualización |

**Nota**: es una tabla por conversación; al finalizar el pedido (LISTO) se limpia.

## Providers / catálogo (desde CRM, no persistido localmente)

- `providerId`: del env `PROVIDER_ID`.
- El agente obtiene el catálogo por `CrmClient.get_products(q, provider_id)`.

## Relaciones

- `bot_cart.conversation_id` → `bot_conversation.id`.
- El carrito solo existe mientras la conversación está abierta; se limpia al finalizar pedido.

## Validación

- `cantidad` >= 1.
- El `product_id` debe provenir del catálogo consultado (no inventar).
- Al finalizar, el pedido se registra en el CRM y se limpia el carrito.
