# NEA — Inventario de herramientas (AS-IS)

> Las 13 herramientas expuestas al LLM, su rol, efectos secundarios, fuentes de
> datos y validaciones. Acompaña a [`AS-IS.md`](AS-IS.md). SHA `c80f90e`.
>
> Schemas: `app/tools.py:TOOL_SCHEMAS:986-1264`. Despacho:
> `app/tools.py:ToolRuntime.execute:2435-2471`. Selección por rol:
> `active_tool_schemas:961-975` (`CORE_TOOLS`, `FARMACIA_TOOLS`, resto = agenda).

---

## 1. Tabla maestra

| Herramienta | Handler (`tools.py`) | Rol | Efecto secundario | Fuente de datos | Validación / guardas |
|---|---|---|---|---|---|
| `update_ficha` | `_update_ficha:2473` | transversal | `PUT /api/bot/ficha` (merge) | args del LLM | filtra `None`; sin campos → no-op |
| `propose_slots` | `_propose_slots:2481` | legado/agenda | `replace_offered_slots` (PG) | `GET /api/bot/availability` | sin slots → `sin_disponibilidad`; guarda hasta `MAX_OFFERED=12` |
| `book_session` | `_book_session:2553` | legado/agenda | `create_booking` CRM + `clear_offered_slots` + `put_ficha` | slots ofrecidos (PG) | `_resolve_offered:2508` valida epoch exacto; `SlotTaken`→re-ofrece |
| `reschedule_session` | `_reschedule_session:2592` | legado/agenda | `reschedule_booking` CRM + `clear_offered_slots` | slots ofrecidos (PG) | igual que book; `no_booking`→`sin_cita` |
| `route_out` | `_route_out:2630` | legado/agenda | `put_ficha` (`calificado=False`) | perfil | marca `routed_out` |
| `handoff` | `_handoff:2643` | transversal | **solo marca** `handoff_reason` (se ejecuta tras el mensaje) | args | cancelable si carrito activo o fue "no disponible" (`_cancelar_handoff_por_no_disponible:2652`) |
| `buscar_medicamento` | `_buscar_medicamento:2957` | Customer (farmacia) | `log_med_query` + crónicos; set `last_products/last_term/...` | `GET /api/bot/products?providerId` | **F15** `_autorizar_busqueda:2768`; relevancia; alias; typos; fail-safe |
| `sugerir_generico` | `_sugerir_generico:4200` | Customer (farmacia) | igual a buscar | `GET /api/bot/products` | misma autorización/relevancia |
| `info_provider` | `_info_provider:4228` | Customer (farmacia) | cachea `paymen_type`/`provider_hours` | `GET /api/bot/providers` | — |
| `agregar_al_carrito` | `_agregar_al_carrito:4257` | Customer (farmacia) | `cart_add` (PG, upsert) + reset `cart_summary_shown` | `last_options` + catálogo (recupera Bs) | cantidad≥1; resuelve "opción N"→SKU real; idempotencia intra/inter-turno |
| `actualizar_cantidad` | `_actualizar_cantidad:4532` | Customer (farmacia) | `cart_set` (PG, reemplaza) | — | cantidad≥1; `no_en_carrito` si no existe |
| `ver_carrito` | `_ver_carrito:4573` | Customer (farmacia) | `update_conversation(cart_summary_shown=True)` | `cart_items` (PG) + `paymenType` CRM | omite Bs si falta (no inventa 0); set `cart_summary_text` |
| `finalizar_pedido` | `_finalizar_pedido:4682` | Customer (farmacia) | `put_ficha` (nota) + `cart_clear` + `cart_closed=True` + limpia entrega | `cart_items` (PG) | `carrito_vacio` si vacío; set `pedido_finalizado` (G13) |

Constantes de reparto de agenda: `MAX_OFFERED=12`, `OFFER_PER_DAY=3`,
`OFFER_DAYS=5` (`app/tools.py:981-984`).

---

## 2. Rol farmacia vs. agenda

- **`CORE_TOOLS`** (transversales, siempre presentes): `update_ficha`, `handoff`.
- **`FARMACIA_TOOLS`**: `buscar_medicamento`, `sugerir_generico`, `info_provider`,
  `agregar_al_carrito`, `ver_carrito`, `actualizar_cantidad`, `finalizar_pedido`.
- **Agenda (legado)**: `propose_slots`, `book_session`, `reschedule_session`,
  `route_out` — **retiradas** cuando `farmacia=True`
  (`active_tool_schemas:969-975`).

`farmacia` se decide en `turn.py:430-431` por presencia de `providerId` en el
contexto CRM (o `settings.provider_id`).

---

## 3. Permisos y autorización

- **Autorización de búsqueda (F15/F15.1).** Toda llamada a `buscar_medicamento`
  pasa por `_autorizar_busqueda:2768` ANTES de consultar el catálogo. La
  autorización es **por término**, no por turno (`intent_terminos_autorizados`):
  - término ya aprobado → se reutiliza sin llamada (`:2791`);
  - mismo producto con otro formato → hereda la aprobación (`:2796`);
  - evidencia fuerte (dosis/forma) abre solo la PRIMERA búsqueda del turno
    (`:2825-2832`);
  - resto → llama `intencion.entender(candidato_obligatorio=True)` (`:2840`);
  - fail-closed: si la capa no carga o falla, **se deniega**
    (`_rechazar_por_fallo_comprension:2880`).
- **API Key.** `/chat` y `/analytics/*` exigen `X-API-Key == CRM_BOT_API_KEY`
  (`webhook.py:195-197,395-397,436-439`).
- **Allowlist.** `run_turn` corta respuesta (no relay) si la identidad no está
  en `ALLOWED_WA_IDS` (`turn.py:288-293`); se salta en modo lab.
- **Tester-only.** `/reset` solo para `TESTER_WA_IDS` (`turn.py:300-304`).

---

## 4. Efectos secundarios por destino

| Destino | Herramientas que escriben |
|---|---|
| CRM `put_ficha` | `update_ficha`, `book_session`, `route_out`, `finalizar_pedido` |
| CRM bookings | `book_session`, `reschedule_session` |
| CRM handoff | `handoff` (diferido, vía `_safe_handoff`) |
| PG `bot_cart` | `agregar_al_carrito`, `actualizar_cantidad`, `ver_carrito`(lee), `finalizar_pedido`(clear) |
| PG `offered_slots` | `propose_slots`, `book_session`, `reschedule_session` |
| PG `bot_conversation` | `agregar_al_carrito`, `ver_carrito`, `finalizar_pedido`, entrega |
| PG `med_queries` | `buscar_medicamento`, `sugerir_generico` (`_log_med_query:2701`) |
| PG `intent_decisions` | `buscar_medicamento` vía `registrar_decision_intencion:2677` |
| PG `patient_profiles` | `_log_med_query` → `registrar_consulta_cronica` |

---

## 5. Validaciones y "nunca inventa"

- **Precios siempre del catálogo**: `agregar_al_carrito` sobrescribe los precios
  del LLM con los de `last_options`/catálogo (`tools.py:4297-4301,4400-4482`);
  si falta Bs, lo recupera por productId→nombre→relevancia estricta única, y si
  es ambiguo lo **omite** (no inventa 0).
- **SKU real**: resuelve "opción N" o id alucinado contra `last_options` y el
  catálogo, para deduplicar (`tools.py:4282-4312,4469-4477`).
- **Idempotencia del carrito**: no dobla cantidades si el backstop ya agregó el
  SKU este turno (`backstop_added_skus:4319`) ni entre turnos si la cantidad
  coincide (`:4351-4378`).
- **Slots server-side**: solo lo ofrecido es reservable, validado por epoch
  exacto (`_resolve_offered:2523-2542`).
- **Términos basura**: `_log_med_query` descarta términos no plausibles
  (`_termino_busqueda_plausible:45`) para no manchar analytics.

---

## 6. Resultados que el LLM debe seguir (contrato de instrucciones)

Cada handler devuelve un dict con `ok` + a veces `instrucciones` que guían al
modelo (p. ej. `ver_carrito` ordena citar `resumen_para_el_cliente` literal,
`tools.py:4674-4679`; `agregar_al_carrito` ordena confirmar en una línea y
preguntar SI/NO, `:4525-4529`). Estos contratos son la interfaz LLM↔runtime y
los backstops de `turn.py` los hacen cumplir de forma determinista.
