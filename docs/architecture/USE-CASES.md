# NEA — Casos de uso (TO-BE)

> **Paso 3.** Casos de uso Customer prioritarios + consulta clínica/handoff. Cada
> uno con actor, precondiciones, permiso, efecto, resultados tipados/errores,
> auditoría y pruebas. Acompaña a [`TO-BE.md`](TO-BE.md),
> [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) y [`POLICY-MATRIX.md`](POLICY-MATRIX.md).
> **No modifica runtime.** Cada caso mapea a símbolos reales del AS-IS.

---

## 1. Índice

| ID | Caso de uso | Actor | Símbolo(s) AS-IS |
|---|---|---|---|
| UC-01 | Buscar catálogo | Customer | `tools._buscar_medicamento:2957`, `sugerir_generico:4200` |
| UC-02 | Consultar detalle / precio | Customer | `_buscar_medicamento` + `_formatear_lista_productos:2190` |
| UC-03 | Consultar disponibilidad | Customer | `get_products` `disponible` + G6 |
| UC-04 | Gestionar carrito | Customer | `_agregar_al_carrito:4257`, `actualizar_cantidad:4532` |
| UC-05 | Preparar pedido (resumen + entrega) | Customer | `_ver_carrito:4573`, `_resolver_paso_entrega:1328` |
| UC-06 | Confirmar pedido | Customer | `_finalizar_pedido:4682` |
| UC-07 | Seguimiento del pedido | Sistema → Customer | `followup.py` |
| UC-08 | Consulta clínica → handoff humano | Customer → Farmacéutico (🔸) | `_handoff:2643`, `_safe_handoff:3052` |

Resultados tipados comunes: ver [`TO-BE.md`](TO-BE.md) §4. Permisos: ver
[`POLICY-MATRIX.md`](POLICY-MATRIX.md).

---

## 2. Resultados tipados (contrato común)

`Encontrado` · `NoEncontrado` · `Ambiguo` · `NoAutorizado` · `Parcial` ·
`ProveedorNoDisponible` · `RequiereConfirmacion` · `Derivado`. Cada caso de uso
declara el subconjunto que puede devolver.

---

## UC-01 — Buscar catálogo

- **Actor:** Cliente. **Precondición:** `aiEnabled` + `windowOpen` + farmacia
  resuelta (`providerId`); identidad en allowlist (o lab).
- **Permiso:** **F15.1** `_autorizar_busqueda:2768` autoriza el **término** antes
  de tocar el catálogo. Fail-closed si la comprensión falla (`_rechazo_comprension`).
- **Comprensión previa (F15):** `intencion.entender` decide `hay_consulta`/`termino`
  (ver [`FLOW-CATALOG.md`](FLOW-CATALOG.md) §1).
- **Efecto:** `GET /api/bot/products?providerId`; alias (F14.4), typos, principio
  activo; `log_med_query` + detección de crónicos; set `last_products/last_term/last_options`.
- **Resultados:** `Encontrado` (lista ordenada por precio) · `NoEncontrado`
  (`med_not_found`, chat abierto, **sin** handoff) · `Ambiguo` (varias opciones /
  `aviso_forma`) · `NoAutorizado` (F15.1) · `ProveedorNoDisponible` (CRM/LLM caído).
- **Auditoría:** `intent_decisions` + `med_queries.intent_*`.
- **Pruebas AS-IS:** `test_f14_busqueda.py`, `test_f15_intencion.py` (incl. A9
  término no autorizado no reitera), `test_comprension_termino.py`.
- **Invariante:** ninguna búsqueda adicional ni no autorizada ([ADR-0003](ADR/ADR-0003-puerta-unica-autorizacion.md)).

---

## UC-02 — Consultar detalle / precio

- **Actor:** Cliente. **Precondición:** producto encontrado (UC-01).
- **Permiso:** mismo que UC-01 (no es acción privilegiada).
- **Efecto (lectura):** presenta precio USD/Bs del **catálogo**, formato VE
  (`_fmt_ve:1352`). **Nunca inventa precio.**
- **Resultados:** `Encontrado` · `Parcial` (falta Bs → se omite, no se inventa 0).
- **Evidencia:** precio del catálogo por producto (ver [`DOMAIN-MODEL.md`](DOMAIN-MODEL.md) §6).
- **Validación/veto:** G1 (precio sin dato), G1-ext (superlativo), G6 (dígitos/stock),
  backstop precio inventado (`turn.py:820-834`).
- **Pruebas AS-IS:** backstops de lista (vía harness P1/P2); G6 (`test_g6_lista_mostrada.py`).
- **🔸 PENDIENTE:** adjuntar **momento de consulta** al precio (PLAN §7).

---

## UC-03 — Consultar disponibilidad

- **Actor:** Cliente. **Precondición:** producto encontrado.
- **Efecto (lectura):** flag `disponible` del catálogo.
- **Resultados:** `Encontrado` (disponible/no) · `NoEncontrado`.
- **Validación/veto:** G6 (coherencia con lista mostrada); un resultado parcial no
  se presenta como confirmación total (PLAN §7).
- **🔸 PENDIENTE:** stock numérico y su momento (hoy solo booleano `disponible`).

---

## UC-04 — Gestionar carrito (agregar / actualizar cantidad)

- **Actor:** Cliente. **Precondición:** al menos un producto mostrado
  (`last_options`); estado `SinCarrito`/`ConItems`/`ResumenMostrado`.
- **Permiso:** acción Customer sobre su propio `conversation_id` (sin autorización
  de catálogo adicional; el precio se recupera del catálogo, no del LLM).
- **Efecto:** `cart_add` (upsert que **suma**) / `cart_set` (reemplaza). Resuelve
  "opción N"/SKU alucinado contra `last_options`. Reset `cart_summary_shown=False`
  si cambia tras resumen.
- **Idempotencia:** no dobla si el backstop ya agregó el SKU (intra-turno) ni entre
  turnos con la misma cantidad (`tools.py:4319,4351-4378`).
- **Resultados:** `Encontrado`/éxito (set `carrito_agregado_turno`, señal G13) ·
  `NoEncontrado` (`no_en_carrito` en actualizar) · `Parcial` (Bs recuperado o omitido).
- **Auditoría:** `bot_cart`, `bot_conversation`.
- **Validación/veto:** G13 (no afirmar "agregué" sin evidencia).
- **Pruebas AS-IS:** `test_f14_busqueda.py::TestF142OpcionNumerada`,
  `test_g13_accion.py`. **Hueco:** sin test unitario directo de `cart_set`.

---

## UC-05 — Preparar pedido (resumen + método de entrega)

- **Actor:** Cliente. **Precondición:** carrito con ítems; cliente pide
  resumen/"LISTO".
- **Efecto:** `ver_carrito` construye `cart_summary_text` determinista (ítems,
  subtotales USD/Bs, total, **formas de pago** del tenant, **método de entrega**);
  omite Bs si falta. `_resolver_paso_entrega` gestiona `delivery_pending`
  (`method`→`address`). Set `cart_summary_shown=True`.
- **Resultados:** `RequiereConfirmacion` (resumen mostrado, falta "SI") ·
  `Parcial` (Bs omitido) · transición a `PreguntaEntrega`/`PideDireccion`.
- **Invariante:** **el resumen no sale sin método de entrega** (`turn.py:1146-1158`);
  `run_turn` **reemplaza** el texto del LLM por `cart_summary_text` salvo que además
  agregue (`_tambien_pide_agregar`).
- **Auditoría:** `bot_conversation` (`delivery_*`, `cart_summary_shown`).
- **Pruebas AS-IS:** **hueco** — entrega y `ver_carrito` sin test unitario
  (`FLOW-ORDERS.md` §9). Se ejercita por harness.

---

## UC-06 — Confirmar pedido

- **Actor:** Cliente. **Precondición:** `ResumenMostrado`/`PendienteConfirmacion`,
  carrito **no vacío**, método de entrega resuelto, **confirmación explícita**.
- **Permiso:** acción Customer; confirmación explícita obligatoria
  ([ADR-0007](ADR/ADR-0007-fuente-verdad-pedido.md)).
- **Efecto:** `put_ficha` (nota `resultado=pedido`, best-effort) + `cart_clear` +
  `cart_closed=True` + limpia entrega. Set `pedido_finalizado` (señal G13).
- **Resultados:** éxito (`Cerrado`) · `NoEncontrado`/error (`carrito_vacio` si
  vacío) · `ProveedorNoDisponible` (CRM caído → no afirmar registro).
- **Idempotencia:** **🔸 PENDIENTE** — añadir idempotency key de pedido (hoy no la
  hay; el backstop evita doble `finalizar`).
- **Validación/veto:** G13 (no afirmar pedido finalizado sin señal real; cubierto
  e2e con carrito vacío).
- **Auditoría:** nota de ficha en CRM; `bot_conversation`.
- **Pruebas AS-IS:** `test_g13_accion.py`. **Hueco:** sin test directo de limpieza
  de estado post-finalizar.

---

## UC-07 — Seguimiento del pedido

- **Actor:** Sistema (`FollowupWorker`) → Cliente. **Precondición:** pedido abierto
  `cart_closed=False`, agendado en `run_turn` a `FOLLOWUP_HOURS=4h`.
- **Efecto:** **un** empujón (`claim_followup` atómico), respetando franja horaria
  (9–19), días (L–S), IA activa y ventana abierta.
- **Resultados:** enviado · omitido (cliente cerró / sin carrito / pausado).
- **Validación/veto:** backstop anti-invención (`_medicamentos_inventados:46`);
  prohíbe terminar en pregunta e inventar el pedido.
- **Invariante:** es para el **pedido** no cerrado, nunca un "¿sigues ahí?".
- **Pruebas AS-IS:** **hueco** — sin test unitario del worker.

---

## UC-08 — Consulta clínica → handoff humano

- **Actor:** Cliente → **Farmacéutico (🔸 rol responsable PENDIENTE)**.
- **Precondición:** el mensaje parece consulta clínica (diagnóstico, tratamiento
  personalizado, interacción, cambio de dosis, embarazo/lactancia, pediatría,
  reacción adversa, síntoma de alarma).
- **Permiso / política:** **fail-closed** — el bot **no** da recomendación clínica
  autónoma; deriva ([ADR-0006](ADR/ADR-0006-handoff-clinico-fail-closed.md), PLAN §7).
- **Efecto (AS-IS):** `handoff` marca `handoff_reason`; `_safe_handoff` lo ejecuta
  **después** del mensaje. Determinista en hostilidad/G4/G9/G11. Cancelable si hay
  carrito activo (`turn.py:1188-1193`). Reactivación solo si fue
  `medicamento_no_disponible`.
- **Resultados:** `Derivado`.
- **Estados de derivación (🔸 PENDIENTE, bloquea Paso 8):** detectado → solicitado →
  asignado → atendido → retomado/cerrado; motivo `clinico` en `HANDOFF_REASONS`;
  proceso humano real (PLAN §8 q6). No se diseña contra supuestos.
- **Auditoría:** motivo mínimo necesario; **no** volcar datos clínicos excesivos
  (PLAN §7, [ADR-0006](ADR/ADR-0006-handoff-clinico-fail-closed.md)).
- **Pruebas AS-IS:** guardas G4/G9/G11 (`guards.py`), hostilidad.

---

## 3. Casos legados (agenda) — fuera de alcance Customer farmacia

`propose_slots`/`book_session`/`reschedule_session`/`route_out`
(`tools.py:2481-2641`) se **retiran** cuando `farmacia=True`
(`active_tool_schemas:969-975`). **🔸 PENDIENTE:** qué flujos de agenda siguen
siendo necesarios (PLAN §8 q7). No se rediseñan aquí.
