# Spec F13 — Ajustes post-prueba real (negativa de "¿otro medicamento?" + sugerencias directas)

Repo: `D:\Proyectos\GenteFarma\agentNEA` (rama `agent-qa`). Cambios aditivos, sin romper vetos existentes (G1-G12).

## Hallazgo 1 — El agente no respeta el "No" del paciente

**Síntoma real:** usuario pide 1 caja de X, agrega al carrito, el agente pregunta
"¿Otro medicamento?", el usuario responde "NO", y el agente responde con la
plantilla G6: *"No tengo información confirmada sobre este medicamento… ¿Cuál
es el medicamento que buscas?"* — ignorando la negativa.

**Causa raíz:** en `app/turn.py` (bloque de vetos, ~línea 721) G6 sustituye el
texto final por `TPL_VERIFICACION_CATALOGO` cuando `not last_products and not
cart_summary_text` y el texto contiene un dato tipo precio/stock. En el turno
de la negativa el LLM suele redactar algo que menciona precio del pedido; como
en ese instante no hay `last_products` ni resumen en runtime, G6 pisa la
respuesta con una plantilla que PREGUNTA de nuevo por un medicamento.

**Fix requerido:**
1. Detectar turno de negativa con carrito activo: `user_text` casado por
   `_quiere_ver_resumen()` con `tiene_carrito=True` (ya existe en turn.py:1633,
   acepta "no", "no gracias" sueltos).
2. En ese caso NO debe aplicarse G6 (ni G5, ni G1-ext) al texto final. Si el
   texto final terminó vetado, reemplazar por el flujo de resumen
   (ver_carrito / resumen del pedido), NO por `TPL_VERIFICACION_CATALOGO`.
3. La respuesta esperada del agente tras el "No" es: confirmar que no agrega
   nada más y mostrar el resumen del pedido (o el cierre amable si el carrito
   quedó vacío). Jamás volver a preguntar "¿cuál medicamento buscas?".

## Hallazgo 2 — Pide confirmación antes de sugerir equivalentes

**Síntoma real:** el paciente pregunta por un medicamento que no está como tal;
el agente pregunta "¿quieres que busque un equivalente?" en vez de mostrar
directamente las opciones relacionadas.

**Causa raíz:** la búsqueda por principio activo ya es automática
(`tools.py` ~1248-1281, devuelve las alternativas con instrucción de
presentarlas). Pero `app/prompt.py` línea 75 dice "Si el catálogo no encuentra
el medicamento, dilo con honestidad y **ofrece buscar una alternativa**", lo
que induce al LLM a pedir permiso. Además el texto intermedio del LLM
("¿te busco el equivalente?") puede terminar vetado y caer en plantillas que
refuerzan la pregunta.

**Fix requerido:**
1. `prompt.py` línea 75: cambiar la redacción para que cuando el medicamento
   exacto no esté, el agente MUESTRE DIRECTAMENTE las alternativas que las
   herramientas le devolvieron (principio activo o genérico), SIN pedir
   confirmación para buscar. Buscar primero, hablar después se mantiene.
2. Verificar que ningún guard castigue esa respuesta directa:
   `consulted_catalog=True` en el turno ya excluye G10a/G10b — confirmarlo.
3. Si el fallback por principio activo devuelve 0, mantener el mensaje honesto
   de "no disponible" (sin inventar), que es el comportamiento validado.

## Validación (3 niveles + Laboratorio)

1. `python -m py_compile app/turn.py app/prompt.py app/tools.py` en host.
2. Import dentro del contenedor: `docker exec vocerocrm-nea-1 python -c "from app import turn"`.
3. Recrear `nea` con `--build`.
4. Laboratorio: 2 corridas, estado fresco (TRUNCATE de nea-db con
   autorización previa del usuario), juez LongCat. Casos a vigilar:
   cierre de pedido con "no" (hallazgo 1) y medicamento no encontrado con
   alternativas (hallazgo 2). Delta < 8 vs baseline para PASS.

## Despliegue

Commits a `agent-qa` (voceroCRM y agentNEA según toque) → push a
`gentefarma/*` → merge fast-forward a local `desarrollo` → push a
`rsomoza01/*/desarrollo` (solo con autorización explícita).
