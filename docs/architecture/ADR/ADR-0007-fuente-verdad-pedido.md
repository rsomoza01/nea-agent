# ADR-0007 — Fuente de verdad del pedido y separación preparar/confirmar

- **Estado:** Aceptada en diseño preliminar (revisión 2026-11-12; decisiones del
  propietario ESTIMADAS, no definitivas — ver §Revisión). Parte D desbloqueada.
- **Fecha:** 2026-11-10 · **Revisión:** 2026-11-12 (Paso 7)
- **Paso del plan:** 3 (prepara Paso 7) / 7 (ejecución incremental)
- **Relacionada con:** [`../TO-BE.md`](../TO-BE.md) §5; [`../DOMAIN-MODEL.md`](../DOMAIN-MODEL.md) §4; [`../FLOW-ORDERS.md`](../FLOW-ORDERS.md); PLAN §7 "Pedidos", §8 preguntas 1, 2, 8

## Revisión 2026-11-12 (decisiones ESTIMADAS del propietario, Paso 7)

El propietario respondió las preguntas abiertas 1/2/8 de forma **estimada (no
definitiva)**. Se registran como base de diseño; cualquiera puede cambiar sin reabrir
el núcleo ya probado:

1. **(P1) API de pedidos en el CRM:** no se asume que exista; **NEA aporta la entidad de
   pedido** como mejora propia.
2. **(P2) Persistencia:** si NEA la construye, **vive en NEA** (nueva tabla
   `order_attempt`, no en el CRM).
3. **(P8) Fuente de verdad de precio/disponibilidad — con refinamiento del arquitecto:**
   el propietario indicó "la base de datos disponible / la DB real de producción". Se
   **refina**: la DB de NEA (Postgres) **no** contiene el catálogo de productos; el
   precio/stock provienen del **catálogo del CRM** (`crm.get_products`,
   DOMAIN-MODEL §4.1), del que `bot_cart` guarda una **instantánea** al agregar. Mantener
   una tabla de precios propia en NEA crearía una **segunda fuente que se desincroniza**
   y rompería "precio siempre del catálogo". Decisión: la **entidad de pedido** se
   persiste en NEA (P2), pero la **fuente de verdad de precio/disponibilidad sigue siendo
   el catálogo**, **revalidado al confirmar** releyéndolo y comparándolo con la
   instantánea del carrito (vía `cart_fingerprint`). En producción ese catálogo puede
   estar respaldado por la DB real detrás del CRM — consistente con la intención del
   propietario, pero accedido por la ruta de catálogo existente, no por una consulta
   paralela.

**Parte D (reserva atómica/concurrencia) — DESBLOQUEADA.** Con P2 ("persistir en NEA")
el mecanismo definitivo queda decidido: **mismo idioma que `Store.mark_processed`**
(`db.py:99`) — `INSERT … ON CONFLICT (order_attempt_id) DO NOTHING RETURNING`, atómico e
idempotente por id; si `RETURNING` viene vacío, el intento ya existía y se devuelve el
guardado. Validado contra Postgres desechable (ver
[`../PR-feat-pharmacy-order-workflow.md`](../PR-feat-pharmacy-order-workflow.md) §DB).
Implementado como puerto `OrderAttemptStore` + referencia en memoria
(`app/application/order_workflow.py`), aún **no cableado** al hot path (decisiones
estimadas). La migración `016_order_attempt.sql` queda como **propuesta validada**, no
aplicada a `migrations/`, hasta que las decisiones sean definitivas.

## Contexto

Hoy:
- El carrito vive en Postgres (`bot_cart`, `migrations/004`), con upsert atómico
  que **suma** cantidad (`db.cart_add:382-397`) y ventana de sesión
  `CART_SESSION_HOURS=12h`.
- "Finalizar pedido" **no crea una entidad pedido** en el CRM: registra una **nota
  de ficha** best-effort (`put_ficha` con `resultado=pedido`,
  `tools._finalizar_pedido:4700-4705`) y limpia el carrito (`cart_closed=True`).
- El estado del pedido se espeja en tres lugares (PG, `ToolRuntime`, `Conversation`
  en memoria) — DEPENDENCY-MAP R8.

**Decisiones abiertas (PLAN §8):** (1) ¿el CRM ofrece API de pedidos o solo
ficha/catálogo? (2) ¿existe pedido persistido en el CRM o solo carrito en NEA?
(8) ¿qué sistema es fuente de verdad de precio/disponibilidad?

## Decisión

Parte **diseñable ya** (no depende de la pregunta abierta):

1. **Separar `preparar pedido` de `confirmar pedido`** como estados explícitos
   (hoy están mezclados en el paso de resumen + confirmación "SI").
2. **Confirmación explícita obligatoria** antes de registrar algo irreversible
   (hoy ya existe vía `_es_confirmacion_resumen`, se formaliza como transición).
3. **El estado comunicado al cliente debe proceder del sistema fuente**; mientras
   no exista entidad de pedido en el CRM, NEA **no** afirma estados que no puede
   demostrar (se sostiene con G13).
4. **Identificador único por intento de pedido (`order_attempt_id`).** Cada
   *intento lógico de confirmación* se identifica con un `order_attempt_id` único
   (p. ej. UUID), **persistido** antes de llamar al sistema fuente. El resultado del
   sistema fuente se liga a ese id para reconciliar y no afirmar un registro que no
   se pueda demostrar (G13). Se distinguen tres escenarios y lo que queda abierto:

   **A. Reintentos del mismo intento.**
   - Un intento lógico genera **un único** `order_attempt_id`.
   - Los reintentos por **timeout, doble webhook o reenvío** del mismo intento
     **reutilizan** ese mismo id.
   - **No** se genera un id nuevo por cada solicitud HTTP ni por cada entrega del
     webhook; el id se ata al intento lógico, no al transporte.

   **B. Doble tap y solicitudes concurrentes.**
   - Dos solicitudes **concurrentes** que corresponden a la **misma** confirmación
     lógica **no** deben crear dos intentos distintos.
   - La **creación/reserva** del intento debe ser **atómica**, y debe existir un
     **mecanismo de transición o reserva** que permita **recuperar el intento ya
     creado** cuando llegue la solicitud duplicada (en vez de crear otro).
   - `INSERT … ON CONFLICT (order_attempt_id) DO NOTHING` solo protege cuando ambas
     solicitudes **comparten el mismo id**; **no** resuelve por sí solo el caso de
     dos ids distintos que representan la misma confirmación lógica.
   - Por tanto **no se afirma que la idempotencia esté completamente resuelta**
     hasta definir **cómo se reconoce y reutiliza el intento lógico ante
     concurrencia** (correlación/reserva). Ver parte PENDIENTE (D).

   **C. Pedidos legítimos distintos.**
   - Una **nueva confirmación intencional** del cliente debe poder generar un
     **nuevo** `order_attempt_id`, **aunque** el carrito tenga exactamente los
     mismos productos y cantidades.
   - El **hash del carrito no es el identificador único** del pedido (puede servir
     como dato de contexto/control de cambios, nunca como clave de unicidad).

   **D. 🔸 PENDIENTE (decisión de diseño del Paso 7).**
   - No se cierra aquí el **mecanismo técnico definitivo** de persistencia y
     coordinación del intento (reserva atómica, correlación de concurrencia,
     ventana de reutilización): depende de la **arquitectura transaccional del
     Paso 7** y de la fuente de verdad del pedido.
   - Se documenta explícitamente el **detalle de concurrencia** (cómo se reconoce y
     reutiliza el intento lógico ante solicitudes concurrentes) como **decisión de
     diseño pendiente del Paso 7**; no se inventa ni se cierra ahora.

Parte **🔸 PENDIENTE (bloquea el modelo transaccional completo del Paso 7):** si el
pedido debe persistirse como entidad en el CRM, con estados
`confirmado/aceptado/preparado/entregado/cancelado`, y cuál es la fuente de verdad
de precio/disponibilidad al confirmar. **No se diseña la máquina de estados
extendida contra supuestos.**

## Alternativas consideradas

| Alternativa | Por qué se descarta / aplaza |
|---|---|
| Modelar ya el pedido completo con estados del CRM | Depende de preguntas abiertas 1/2/8; diseñar ahora es diseñar contra supuestos (PLAN §8). |
| Mantener "finalizar = nota de ficha" como estado final | Aceptable **temporalmente**; no permite seguimiento real de estado de pedido. Se marca como límite actual. |
| Idempotencia por mensaje únicamente (dedup actual) | El dedup de `processed_message` cubre el webhook, no la confirmación de pedido; se necesita clave a nivel de pedido. |

## Consecuencias

- **Positivas:** la separación preparar/confirmar y la idempotencia se pueden
  especificar y probar sin tocar el CRM.
- **Negativas:** hasta cerrar las preguntas 1/2/8, el "estado de pedido" real es
  solo carrito + nota; el roadmap del Paso 7 queda parcialmente bloqueado.
