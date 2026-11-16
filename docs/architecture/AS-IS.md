# NEA — Arquitectura AS-IS (cómo funciona hoy)

> **Paso 2 del plan de modernización.** Documento de auditoría: describe el
> comportamiento REAL del runtime en `HEAD` de `chore/architecture-baseline`
> (SHA `c80f90e`). No propone mover módulos ni refactorizar. Cada afirmación
> importante apunta a `ruta:símbolo` con líneas aproximadas.
>
> Fecha: 2026-11-10. Rama de trabajo: `docs/architecture-as-is`.
> Documentos hermanos: [`DEPENDENCY-MAP.md`](DEPENDENCY-MAP.md),
> [`TOOL-INVENTORY.md`](TOOL-INVENTORY.md), [`FLOW-CATALOG.md`](FLOW-CATALOG.md),
> [`FLOW-ORDERS.md`](FLOW-ORDERS.md).

---

## 0. Qué es NEA

NEA es un agente de WhatsApp para farmacias (y, en su chasis original, para
agendamiento de citas). Corre como una app **FastAPI** (`app/main.py`) detrás
de un CRM (`vocero-crm`): recibe eventos de Meta o del puente Evolution, los
procesa con un LLM con *tool-calling*, y responde por el CRM. El estado propio
del bot vive en Postgres; el catálogo, la agenda y el envío de mensajes viven
en el CRM.

Dos "roles" conviven en el mismo código:

- **Agenda** (chasis original): `propose_slots` / `book_session` /
  `reschedule_session` / `route_out`.
- **Farmacia** (spec 001, el negocio activo hoy): catálogo, carrito, pedido,
  OCR de recetas. Se activa cuando hay `providerId` (`app/turn.py:430-431`).

La selección de herramientas por rol está en
`app/tools.py:active_tool_schemas:961-975` (`CORE_TOOLS` + `FARMACIA_TOOLS` vs.
schema completo).

---

## 1. Flujo de entrada → envío (traza con símbolos)

### 1.1 Resumen en una línea

```
POST /webhook  →  relay (cola) + extract_inbound  →  dedup  →  Coalescer.add
   →  (debounce COALESCE_SECONDS)  →  handle_flush  →  conversation_lock  →  run_turn
   →  media  →  build_system_prompt + state_block  →  _tool_loop (LLM + tools + backstops in-loop)
   →  backstops post-loop  →  guardas G1–G13 (vetos)  →  _send  →  handoff/fase/followup
```

### 1.2 Diagrama de secuencia (Mermaid)

```mermaid
sequenceDiagram
    participant Meta as Meta / Evolution
    participant WH as webhook.py (/webhook, /chat)
    participant RelayQ as relay_queue (PG)
    participant CO as Coalescer
    participant TF as turn.handle_flush
    participant RT as turn.run_turn
    participant MED as media.py
    participant CRM as CrmClient
    participant LLM as OpenAiLlm
    participant TR as tools.ToolRuntime
    participant G as guards.py
    participant ST as Store (PG/Memory)

    Meta->>WH: POST /webhook (body crudo + firma)
    WH->>WH: verify_signature()  (401 si inválida)
    WH-->>Meta: 200 inmediato (trabajo en task de fondo)
    WH->>RelayQ: enqueue_relay(body, signature)  (relay PRIMERO)
    WH->>WH: extract_inbound(payload)
    WH->>ST: mark_processed(wa_message_id)  (dedup atómico)
    WH->>CO: coalescer.add(identity, msg)
    Note over CO: debounce COALESCE_SECONDS (8s); cada msg reinicia el timer
    CO->>TF: on_flush(identity, items)
    TF->>TF: conversation_lock(identity)  (serializa turnos)
    TF->>RT: run_turn(ctx, identity, items)
    RT->>CRM: get_context()  (aiEnabled, windowOpen, providerId)
    RT->>MED: describe_item()  (audio/imagen/pdf → texto + OCR)
    RT->>RT: build_system_prompt + _FARMACIA_BLOCK + state_block
    RT->>TR: _tool_loop(...)
    loop hasta MAX_TOOL_ROUNDS (5) o texto final
        TR->>LLM: complete(messages, tools)
        LLM-->>TR: tool_calls | content
        TR->>TR: _autorizar_busqueda() (F15) → execute(tool)
        TR->>CRM: get_products / bookings / ...
        TR->>ST: cart_add / update_conversation / log_med_query
    end
    RT->>RT: backstops post-loop (contradicción, formato, omisión, precio...)
    RT->>G: G1–G13 (vetos deterministas → plantilla constante)
    RT->>CRM: _send() → send_message (o pending_send si agota)
    RT->>CRM: post_handoff (si aplica, DESPUÉS del mensaje)
    RT->>ST: update_conversation (fase, followup, last_product...)
```

### 1.3 Paso a paso con citas

1. **Webhook de entrada.** `app/webhook.py:receive:290-303` responde `200`
   inmediato y lanza `_process` en una task de fondo (referencias vivas en
   `_bg_tasks`, `app/webhook.py:32`). La firma `x-hub-signature-256` se verifica
   en `verify_signature:35-42` (sin `META_APP_SECRET` se acepta — modo dev).

2. **Relay primero.** `_process` encola el body crudo en `relay_queue` ANTES de
   parsear (`app/webhook.py:306-312`); así el CRM recibe el payload pase lo que
   pase. Despierta al `RelayWorker` con `ctx.relay_wake.set()`.

3. **Extracción / normalización.** `extract_inbound:77-163` parsea tolerante el
   payload de Meta: salta `smb_message_echoes` (coexistence), `reaction`,
   `unsupported` con errores (fantasma de Meta, re-entrega), y construye
   `InboundMessage`. La identidad se canonicaliza en el origen
   (`canonical_identity`, `app/config.py:12-22`: `521…`→`52…` para México).
   `_extract_text:45-59` saca texto de `text`/`button`/`interactive`.

4. **Dedup.** `ctx.store.mark_processed(wa_message_id)` es un `INSERT … ON
   CONFLICT DO NOTHING` atómico (`app/db.py:99-108`, tabla `processed_message`).
   Fresco → sigue; repetido → se descarta (`app/webhook.py:335-339`).

5. **Coalescing.** `ctx.coalescer.add(identity, msg)` (`app/webhook.py:343`).
   `Coalescer` (`app/coalesce.py:18-54`) acumula por identidad y reinicia un
   timer de `COALESCE_SECONDS` (default 8 s, `app/config.py:103`) en cada
   mensaje; al vencer dispara `_fire` → `on_flush`. El callback nunca propaga
   excepciones (`app/coalesce.py:42-46`).

6. **Typing temprano.** `_early_typing` (`app/webhook.py:354-376`) marca
   leído + "escribiendo…" ~0,5 s después, respetando allowlist y `stalled_at`.

7. **handle_flush.** `on_flush` está cableado a `handle_flush`
   (`app/main.py:_wire_coalescer:43-47`, `functools.partial`).
   `handle_flush` (`app/turn.py:245-251`) toma el `conversation_lock` y llama
   `run_turn`; cualquier excepción muere en silencio con log.

8. **conversation_lock.** `app/turn.py:210-242`. Un `asyncio.Lock` por
   identidad (en `ctx.turn_locks`) serializa los turnos de una conversación:
   el mensaje tardío espera y re-lee contexto fresco. Refcount en
   `ctx.turn_lock_users` evita reciclar el candado bajo un turno en espera.

9. **run_turn.** `app/turn.py:265-1259`. Es el corazón. En orden:
   - Gate 1 allowlist (`:288-293`); se salta en modo lab (`ctx.lab_outbox`).
   - Comando `/reset` para testers (`:300-304` → `_run_reset:1261`).
   - Gate 1.5 `stalled_at` (cierre por falta de rumbo + cooldown 24 h,
     `:310-321`, `STALL_COOLDOWN:190`).
   - Gate 2 contexto CRM (`_fetch_context:330` → `:334-363`): exige
     `aiEnabled` y `windowOpen`. Reactiva solo si el handoff previo fue
     `medicamento_no_disponible` (`:345-357`).
   - Persiste `crm_conversation_id`, `last_inbound_at` (`:365-370`).
   - Procesa multimedia (`media.describe_item`, `:382-410`).
   - Quita el vocativo inicial (`_sin_vocativo`, `:416`) y persiste el mensaje
     del usuario (`:417-419`).
   - Arma el system prompt (`build_system_prompt` + `_FARMACIA_BLOCK` +
     `_build_state_block`, `:432-472`).
   - Limpia marcadores del sistema del historial salvo el turno en curso
     (`_texto_cliente_sin_marcadores`, `:502-522`).
   - Inyecta alertas deterministas: hostilidad (`hostile_streak≥3`, `:526-528`),
     G3 canal sin media (`:533-540`), candado de cierre (`sin_rumbo`, `:545-553`).
   - Backstops PRE-LLM: petición de envío (`:602-637`), "¿solo por separado?"
     (`:642-659`).
   - Lanza `_tool_loop` con heartbeat de typing (`:665-693`).

10. **_tool_loop.** `app/turn.py:1473-3005`. Es el bucle de tool-calling y la
    mayor concentración de reglas deterministas:
    - **Paso de entrega** resuelto ANTES del LLM (`_resolver_paso_entrega`,
      `:1328-1470`; el loop lo invoca al inicio): único punto por el que pasan
      todos los turnos.
    - **Handoff por petición** del cliente, determinista y "a la primera"
      (`:1509-1572`, `_pide_hablar_con_humano`).
    - **Guard foto** (`:1572`).
    - Rondas LLM (`MAX_TOOL_ROUNDS=5`, `app/turn.py:83`): por cada tool-call
      llama `runtime.execute(name, args)` (`app/tools.py:2435-2471`).
    - Backstops in-loop que FUERZAN herramientas cuando el modelo no las llama:
      horario (`info_provider`, `:1907`), resumen (`ver_carrito`, `:1946`),
      finalizar (`finalizar_pedido`, `:1967`), flujo de receta multi-medicamento
      (`:2444` y helpers `_medicamentos_de_ocr:6364`).
    - `_respaldo_sin_texto` (`:4268-4294`) cuando el catálogo falla.

11. **Backstops post-loop** (en `run_turn`, `:717-938`): reemplazan el texto del
    LLM por la lista real del catálogo cuando contradice/desordena/omite/inventa:
    contradicción (`:721-735`), lista desordenada (`:742-756`), formato no
    canónico (`:769-784`), omisión (`:799-814`), precio inventado (`:820-834`),
    handoff injustificado (`:845-861`), avisos de dosis/forma (`:871-885`),
    bloque de carrito (`:892-912`), resumen determinista (`:929-938`).

12. **Guardas G1–G13 (vetos).** `app/turn.py:940-1098` sobre `app/guards.py`.
    Cadena `if/elif` que, al final del turno, reemplaza el texto por plantilla
    constante y/o fuerza handoff. Ver §3.

13. **Envío.** `_send` (`app/turn.py:3006-3049`): normaliza a dialecto WhatsApp
    (`a_formato_whatsapp`), parte mensajes >4000 chars (`_partir_mensaje_largo`),
    reintenta `SEND_ATTEMPTS=4` veces; si agota, encola en `pending_send`
    (jamás descarta — incidente 2026-08-03). En modo lab escribe en
    `ctx.lab_outbox` (`:3020-3022`).

14. **Handoff / fase / seguimiento.** Tras enviar: handoff se ejecuta DESPUÉS
    del mensaje (`_safe_handoff`, `:1194-1195`) salvo que haya carrito activo
    (`:1188-1193`). Luego persiste fase, `last_product/last_term/last_options`,
    y agenda `followup_due_at` solo si hay pedido abierto no cerrado
    (`:1197-1249`).

---

## 2. Ciclo de vida y dependencias de los objetos centrales

| Objeto | Definición | Construcción (prod) | Construcción (tests) | Vida |
|---|---|---|---|---|
| `AppContext` | `app/state.py:715-737` (dataclass) | lifespan de FastAPI (`app/main.py:64-86`) | inyectado por los tests con fakes | Toda la app |
| `Settings` | `app/config.py:25-168` (pydantic-settings) | `Settings()` lee `.env`/entorno | valores explícitos | Toda la app |
| `Store` (protocolo) | `app/state.py:181-327` | `PgStore` | `MemoryStore` (`app/state.py:333-709`) | Toda la app |
| `PgStore` | `app/db.py:73-879` | `connect()` + `migrate()` al arranque | — | Pool asyncpg (min1/max5) |
| `CrmClient` | `app/crm.py:102-355` | `CrmClient(base_url, api_key)` httpx | fake / respx | Toda la app |
| `OpenAiLlm` | `app/llm.py:53-486` | con modelos chat/transcribe/vision | fake con `.complete()` | Toda la app |
| `ProfileProvider` | `app/profile.py:102-170` | con CRM + brief + TTL 300 s | `None` → perfil mínimo | Caché por `conversation_id` |
| `Coalescer` | `app/coalesce.py:18-54` | `_wire_coalescer` | manejado a mano | Toda la app |
| `ToolRuntime` | `app/tools.py:2221-2403` | **UNO POR TURNO** en `run_turn:564` | parcial en sondas | Un turno |

**Lifespan de producción** (`app/main.py:50-110`): conecta Postgres, aplica
migraciones idempotentes (`store.migrate:89-96`), carga alias
(`cargar_desde_store`), construye `CrmClient`/`OpenAiLlm`/`ProfileProvider`,
arma el `AppContext`, cablea el `Coalescer`, y arranca **tres workers**
(`:90-97`). Al cerrar: cancela workers, cierra coalescer, CRM y store.

**Inyección en tests**: `create_app(ctx)` acepta un `AppContext` pre-armado
(`app/main.py:50`, `app/main.py:113-115`); el lifespan detecta `own_resources =
app.state.ctx is None` y no construye recursos propios.

`ToolRuntime` es efímero por turno: acumula TODO el estado observable del turno
(`handoff_reason`, `last_products`, `cart_summary_text`, veredicto F15, flags de
backstop…) en ~60 atributos documentados en `__init__` (`app/tools.py:2224-2403`).
`run_turn` lee esos atributos para decidir backstops y persistencia.

---

## 3. Guardas deterministas (anti-alucinación)

`app/guards.py` (638 líneas) es CÓDIGO puro: cada guarda **detecta** con regex y
**reemplaza** por plantilla constante. La detección vive en `guards.py`; la
CONDICIÓN DE ESTADO y la aplicación viven en `run_turn` (`app/turn.py:950-1098`).

| ID | Qué veta | Detector | Plantilla |
|---|---|---|---|
| G1 | Precio citado sin respaldo | `pide_precio_sin_dato:324` | `TPL_SIN_PRECIO:30` |
| G1-ext | Superlativo de precio sin respaldo | `cita_superlativo_precio:121` | `TPL_SIN_PRECIO` |
| G2 | Política fuera de KB (devolución, garantía…) | `intencion_fuera_kb:330` | `TPL_FUERA_KB:49` + handoff |
| G3 | Afirmar media que no llegó | `afirma_recibir_media:340` | `TPL_SIN_MEDIA:79` |
| G4 | Acusación de fraude | `acusacion_fraude:345` | `TPL_CIERRE_G4:99` + handoff |
| G5 | Composición/genérico sin evidencia por producto | `afirma_composicion:185` + `hay_evidencia_composicion:206` (fail-closed) | `TPL_SIN_COMPOSICION:171` |
| G6 | Precio con dígitos / stock / principio activo directo | `afirma_dato_catalogo:272` | `TPL_VERIFICACION_CATALOGO[_CON_LISTA]:254/266` |
| G7 | Eco del mensaje del cliente | `es_echo:302` (Jaccard >70% bidireccional) | `TPL_NO_ECHO:288` |
| G8 | Placeholder `[inserta…]` o nombre de tool | `tiene_placeholder:381` | `TPL_BUSQUEDA_HONESTA:375` |
| G9 | Cliente pide humano sin handoff | `pide_humano:430` | `TPL_CIERRE_ESCALADO:423` + handoff |
| G10a | Clase terapéutica / uso sin catálogo | `afirma_composicion_farmacologica:485` | `TPL_G10_COMPOSICION:473` |
| G10b | Promesa de búsqueda sin acción | `promete_busqueda_sin_accion:490` | `TPL_G10_PROMESA:479` |
| G11 | Promesa de escalado sin handoff | `promete_escalado:531` | `TPL_G11_ESCALADO:525` + handoff |
| G12 | Tool-call JSON cruda filtrada | `contiene_toolcall_json:558` | `TPL_G12_TOOLCALL:552` |
| G13 | Afirmar acción que no ocurrió | `familia_accion_afirmada:622` + `_g13_accion_ocurrio` (`app/turn.py:254-262`) | `TPL_G13_ACCION:615` |

Hostilidad (`app/hostility.py`) y cierre sin rumbo (`app/stall.py`) son
detectores de CONTEO entre turnos (deliberadamente fuera del LLM): el conteo es
determinista, el LLM solo redacta; `run_turn` garantiza el handoff al tercer
strike (`:697-698`) aunque el modelo no lo llame.

---

## 4. `app/turn.py` agrupado por responsabilidad

`turn.py` tiene 8.328 líneas; la gran mayoría son **helpers deterministas
puros** (regex/normalización) que alimentan los backstops. Agrupación:

### 4.1 Orquestación / IO (efectos de red y DB)
- `handle_flush:245`, `run_turn:265`, `_tool_loop:1473`, `_send:3006`,
  `_safe_handoff:3052`, `_fetch_context:1285`, `_run_reset:1261`,
  `conversation_lock:210`, `_typing_heartbeat:122`,
  `_preguntar_metodo_entrega:1306`, `_resolver_paso_entrega:1328`,
  `_fallback_farmacia:4242`, `_respaldo_sin_texto:4268`,
  `_resolver_alternativas_de_receta:6315`, `_medicamentos_de_ocr:6364`.

### 4.2 Reglas de dominio (deciden, sin IO)
- Entrega/pedido: `_es_peticion_de_envio:3862`, `_eleccion_entrega:3981`,
  `_quiere_ver_resumen:3718`, `_quiere_finalizar:3742`, `_pide_el_total:4005`,
  `_es_resumen_de_pedido:4038`, `_parece_direccion:4049`,
  `_es_confirmacion_resumen:4189`, `_tambien_pide_agregar:3693`.
- Opciones/cantidades: `_extraer_eleccion_opcion:3542`,
  `_extraer_eleccion_multiple:3396`, `_menciona_opcion_numerada:3523`,
  `_es_respuesta_cantidad:3589`, `_extraer_cantidad:3614`,
  `_pregunta_es_cantidad:3569`, `filtrar_opciones_mostradas:4110`.
- Clasificación de mensaje: `_parece_nombre_producto:5256`,
  `_parece_consulta_medicamento:5310`, `_es_despedida_o_handoff:5434`,
  `_lead_esta_cerrando:4804`, `_es_reclamo_o_cortesia:7772`,
  `_pide_hablar_con_humano:3948`, `_quiere_info_horario:3652`.
- Receta/OCR: `_parsear_medicamentos_receta:8096`, `_son_varios_envases:6153`,
  `_terminos_por_envase:6106`, `_medicamentos_enumerados:6899`,
  `_parece_lista_medicamentos:7836`, `_receta_ilegible:7478`.

### 4.3 Presentación (construyen el texto hacia el cliente)
- `_build_state_block:5330`, `_lista_con_aviso:7195`,
  `_formatear_receta:8168`, `_titulo_de_lista:7560`, `_avisos_de_lista:7293`,
  `_respuesta_por_separado:4905`, `_respuesta_no_es_ese_farmaco:5102`,
  `_aviso_no_lo_tengo:7154`, `MENSAJE_SUGERIDO_CARRITO:76`.

### 4.4 Guardas/limpieza de salida (post-proceso del texto del LLM)
- `_strip_internal_markup:4679`, `_quitar_ofrecimiento_consulta:4722`,
  `_quitar_repregunta_tras_cierre:5110`, `_quitar_repeticiones:5018`,
  `_con_confirmacion_carrito:4925`, `_sin_equivalencia_inventada:7100`,
  `_quitar_invito_carrito:5169`, `_quitar_pie_carrito_duplicado:5192`,
  `_niega_disponibilidad:5453`, `_lista_desordenada:5487`,
  `_formato_no_canonico:5527`, `_cita_precio_inventado:5621`,
  `_menciona_producto:5646`, `_montos_respaldados:7047`.

### 4.5 Helpers puros (normalización, parsing, sin estado)
- `_sin_tildes:5813`, `_norm_simple:7023`, `_partir_mensaje_largo:153`,
  `_extraer_dosis`/`_extraer_termino_medicamento:8241`,
  `_texto_cliente_sin_marcadores:5691`, `_texto_ocr_completo:5753`,
  `_limpiar_transcripcion:5822`, `_agent_tz:200`, `_MatchInvertido:4412`.

> **Observación de altitud (no acción):** `turn.py` concentra orquestación,
> dominio, presentación y ~90 helpers puros en un solo módulo. Es el mayor punto
> de acoplamiento del sistema (ver [`DEPENDENCY-MAP.md`](DEPENDENCY-MAP.md) §4).

---

## 5. Workers y módulos transversales

| Worker | Archivo | Intervalo | Qué hace |
|---|---|---|---|
| RelayWorker | `app/relay.py:21-86` | wake + barrido 5 s | Reenvía body crudo al CRM con backoff (24 h máx, cap 15 min) |
| FollowupWorker | `app/followup.py:158-347` | 60 s | UN empujón del PEDIDO no cerrado, con ventana horaria/días y backstop anti-invención (`_medicamentos_inventados:46`) |
| SenderWorker | `app/sender.py:22-96` | 30 s | Reintenta `pending_send`; 409→abandona, 24 h→handoff error |

- **Mensajes**: `app/sender.py`, `_send`/`_partir_mensaje_largo` en `turn.py`,
  dialecto WhatsApp en `app/wtext.py:a_formato_whatsapp:44`.
- **Estado**: `app/state.py` (modelos + protocolo + MemoryStore), `app/db.py`
  (PgStore), migraciones en `migrations/`.
- **Follow-up**: `app/followup.py` + columnas `followup_*` de `bot_conversation`.
- **Relay**: `app/relay.py` + `relay_queue`.
- **Media**: `app/media.py` (audio/imagen/pdf/sticker/location/contacts/video),
  OCR y clasificación de legibilidad en `app/llm.py:238-360`.
- **Observabilidad**: `app/observability.py` (Langfuse lazy no-op; traza por
  turno vía contextvar `_current_trace:25`; generaciones LLM en
  `OpenAiLlm._record_generation:426`).
- **Comprensión (F15)**: `app/intencion.py` — capa que decide si el mensaje trae
  consulta de medicamento ANTES de buscar.
- **Relevancia de catálogo**: `app/relevancia.py` (1.226 líneas de matching
  difuso); detalle en [`FLOW-CATALOG.md`](FLOW-CATALOG.md).

---

## 6. Puntos de entrada y clasificación por rol

### 6.1 Puntos de entrada HTTP (`app/webhook.py`, `app/main.py`)

| Endpoint | Símbolo | Rol | Notas |
|---|---|---|---|
| `GET /webhook` | `webhook.verify:166` | transversal | verificación Meta (hub.challenge) |
| `POST /webhook` | `webhook.receive:290` | Customer | canal Meta; 200 inmediato + task |
| `POST /chat` | `webhook.chat:180` | Customer / Staff-lab | canal Evolution (`send=true`, debounce) + Laboratorio del CRM (`send=false`, turno inmediato, `lab_outbox`) |
| `GET /health` | `main.health:118` | transversal | ping DB |
| `GET /analytics/top-meds` | `webhook.analytics_top_meds:384` | **Staff** | dashboard; `X-API-Key` |
| `GET /analytics/chronic-patients` | `webhook.analytics_chronic_patients:426` | **Staff** | pacientes crónicos; `X-API-Key` |

> **Entrada alternativa clave:** `POST /chat` es un segundo camino de ingesta
> (Evolution/Lab) que entra directo al coalescer (`send=true`) o a `handle_flush`
> (`send=false`), saltándose el relay y la verificación de firma de Meta — usa
> `X-API-Key` en su lugar (`app/webhook.py:195-197`). Es un punto de acoplamiento
> y un riesgo a tener presente (ver §7).

### 6.2 Clasificación de capacidades (resumen; detalle en los flujos)

| Capacidad | Clase | Evidencia |
|---|---|---|
| Consulta/precio/disponibilidad de medicamentos | Customer | `tools._buscar_medicamento:2957` |
| Carrito y pedido (agregar/resumen/finalizar) | Customer | `tools._agregar_al_carrito/_ver_carrito/_finalizar_pedido` |
| Entrega (delivery/retiro) | Customer | `turn._resolver_paso_entrega:1328` |
| OCR/recetas | Customer | `media._image:213`, `turn._medicamentos_de_ocr:6364` |
| Handoff a humano | transversal | `tools._handoff:2643` |
| update_ficha | transversal | `tools._update_ficha:2473` |
| Dashboard top-meds | **Staff** | `webhook.analytics_top_meds:384` |
| Pacientes crónicos | **Staff** | `webhook.analytics_chronic_patients:426`, `db.chronic_patients:766` |
| Agenda (propose/book/reschedule/route_out) | **legado/agenda** | `tools.*:2481-2641`; retiradas en rol farmacia |
| Consent de pacientes crónicos | **Staff — PENDIENTE** | `store.marcar_consent` existe pero no hay endpoint que lo exponga (ver §7) |

---

## 7. Riesgos de acoplamiento, ciclos y entradas alternativas

1. **`turn.py` como centro de gravedad (acoplamiento alto).** Importa 20+
   símbolos de `tools.py` y 3 de `relevancia.py` (`app/turn.py:27-70`), y
   `tools.py`/`followup.py`/`sender.py` importan de vuelta de `turn.py`. Es el
   módulo que todo toca.

2. **Ciclo lógico `turn ↔ tools`.** `turn` importa de `tools` a nivel de módulo
   (`:43-70`); `tools` NO importa `turn` a nivel de módulo, pero sí comparte
   constantes duplicadas (`_PALABRAS_FUNCIONALES` existe en `tools.py:258` y hay
   dos copias/variantes más, comentado en `turn.py:3196`). Acoplamiento por
   datos compartidos.

3. **Ciclos evitados con imports tardíos (señal de dependencia circular latente):**
   - `sender.py:56` importa `turn.WA_MAX_CHARS` / `_partir_mensaje_largo` dentro
     de un método (evita ciclo `turn`→`sender` inexistente pero `followup`→`turn`
     sí existe a nivel módulo: `followup.py:21-22`).
   - `llm.py:205,265` importa `media.MAX_IMAGE_BYTES` tardío (evita ciclo
     `media`→(usa `ctx.llm`) y `llm`→`media`).
   - `tools.py:2834` importa `intencion.entender` tardío dentro de
     `_autorizar_busqueda` (fail-closed si no carga).
   - `followup.py:19` importa `prompt`; `turn.py:24` importa `intencion`.

4. **Entrada alternativa sin firma de Meta.** `POST /chat` (`send=true`) encola
   directo al coalescer sin pasar por `verify_signature` ni `enqueue_relay`;
   depende solo de `X-API-Key`. Dos rutas de ingesta con contratos distintos
   para el mismo `handle_flush`.

5. **Dos "fuentes de verdad" para el mismo estado.** El carrito vive en PG
   (`bot_cart`) pero varios flags de pedido se copian al `ToolRuntime` (espejo
   por turno) y a la `Conversation` en memoria; `finalizar_pedido` y los
   backstops releen la conversación "fresca" de la DB para no usar copias viejas
   (`app/tools.py:4718-4723`, `app/turn.py:1237-1240`). Es un patrón consciente,
   pero frágil.

6. **Capacidad Staff sin endpoint.** `store.marcar_consent` (`app/db.py:831`) y
   `registrar_consulta_cronica` existen, pero solo `chronic_patients`/`top_meds`
   están expuestos por `webhook.py`. El consent (Fase 3) queda **pendiente**.

7. **MemoryStore divergente del PgStore.** El fake de tests
   (`app/state.py:333-709`) implementa `top_med_queries`/`chronic_patients` con
   firmas MÁS SIMPLES que `PgStore` (p. ej. sin `q`/paginación): los tests que
   usan MemoryStore no cubren la ruta real de analytics. Riesgo de deriva.

---

## 8. Contraste con los tests existentes

El suite (`tests/`, 252 tests que pasan — ver [`BASELINE.md`](BASELINE.md))
cubre bien las piezas DETERMINISTAS y poco la orquestación end-to-end:

| Área AS-IS | Test que la respalda | Cobertura |
|---|---|---|
| Tool-loop / respaldo de agotamiento | `test_tool_loop_respaldo.py` (A1–A9) | Alta (incluye F15.1 "término no autorizado no reitera", A9) |
| Guarda G5 (composición, fail-closed) | `test_g5_evidencia.py` (B1–B9) | Alta |
| Guarda G13 (acción no ocurrida) | `test_g13_accion.py` | Alta (incl. e2e carrito vacío) |
| Capa F15 (comprensión) | `test_f15_intencion.py` (camino rápido, contrato JSON, capa, auditoría, autorización por término) | Alta |
| Extracción de término | `test_comprension_termino.py` | Alta |
| Catálogo F14 (apelativos, opción numerada, alias, typos, multi-med) | `test_f14_busqueda.py` | Media-alta |
| Equivalencia inventada + aviso de forma | `test_equivalencia_y_forma.py` | Media |
| OCR/PDF | `test_media_pdf.py` | Media (solo extracción PDF) |
| Hints de proveedor LLM | `test_llm_provider_hints.py` | Alta |

**Huecos de test observados** (no acción, solo registro):
- No hay test de `run_turn` completo con webhook→send (los harness P1/P2/P3 no
  tienen `__main__` y salen con 0 bytes — ver [`BASELINE.md`](BASELINE.md)).
- `_resolver_paso_entrega` (entrega) y los workers (relay/followup/sender) no
  tienen tests unitarios en `tests/`.
- Analytics (`top_med_queries`, `chronic_patients` del `PgStore`) no se testea
  contra DB real en el suite (solo MemoryStore con firma distinta).
- `MemoryStore` no garantiza paridad con `PgStore` para analytics/paginación.

---

## 9. Reglas duras del runtime (constitución implícita)

- **200 en <1 s siempre** en el webhook; trabajo asíncrono (`webhook.receive`).
- **El turno jamás tumba el loop**: todo termina en silencio + log
  (`coalesce._fire:42-46`, `handle_flush:250`, `run_turn` degradación).
- **Nunca se descarta una respuesta generada**: `pending_send` (`_send:3044`).
- **Datos de producto solo del catálogo**: guardas G1/G5/G6/G10 + backstops.
- **Migraciones idempotentes** re-corridas en cada arranque (`db.migrate:89-96`).
- **Aislamiento por tenant**: `providerId` del contexto CRM, perfil por
  `conversation_id` (`profile.get:121`), catálogo por `providerId`.
