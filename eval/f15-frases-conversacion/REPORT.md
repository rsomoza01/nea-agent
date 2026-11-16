# F15 — Capa de comprensión del acto de conversación (reporte del cambio)

**Qué es:** el cambio que impide que el agente busque en el catálogo antes de entender qué le
están diciendo. Responde a las quejas del cliente (oct-2026) y a la directriz *«no quiero atarlo a
una regla específica por palabras: que diferencie primero las conversaciones y luego entienda cuál
es el medicamento»*.

**Estado:** implementado, medido y commiteado en `desarrollo` (`88bfd86`, `2b5cdfa` y el retiro del camino rápido por verbo).
Pendiente: veredicto del diagnóstico independiente (sección 6).

---

## 1. Las quejas y su causa raíz

| Queja (verbatim) | Qué hacía el agente |
|---|---|
| «No puede ser Indu.» (habla del laboratorio) | Contestó **«⚠️ No tengo SER INDU»** |
| «Vivo en Ciudad Bolivar.» (dónde vive) | **13 consultas al catálogo**: `vivo ciudad bolivar` → transposiciones → `boliva`, `boliv`, `boli`, `bol` |
| «Solo una marca?» (pregunta por el atributo) | Buscó el término `marca` |
| «Por ahroa no» (negativa temporal) | Buscó el término `ahroa` |

**Causa raíz medida** (no teórica): la decisión de buscar la tomaba un heurístico de **forma**
(`_parece_consulta_medicamento` → `_parece_nombre_producto`): «sin `?`, ≤4 palabras, ninguna de
saludo y una palabra de ≥5 letras ⇒ buscar». «Vivo en Ciudad Bolivar.» cumple las cuatro. Y la
bandera que lo habilitaba, `hay_senal_de_farmaco`, devolvía **True en 4 de las 5 frases basura**
(`tokens_farmaco` es, en la práctica, «cualquier palabra de ≥4 letras»: `indu` contaba como fármaco).

**Reproducido end-to-end con el modelo de producción** (`deepseek/deepseek-v4.1-flash`):
`eval/f15-frases-conversacion/antes-deepseek-v4.1-flash.{json,md}` — incluido el «No tengo SER INDU»
literal, y las 13 consultas de «Ciudad Bolivar» (el cliente no las ve, pero contaminan
`med_queries`, de donde F14.4 deriva alias y correcciones).

## 2. Qué se implementó

**`app/intencion.py`** — la capa. Clasifica el acto de conversación (`saludo`, `cortesia`,
`dato_personal`, `atributo_producto`, `dilatoria`, `reclamo`, `consulta_medicamento`, `otro`) y
decide `hay_consulta` + `termino`, con **tres caminos** para no pagar una llamada donde hoy se
acierta:

| Camino | Cuándo | Coste |
|---|---|---|
| `nombre` | nombre pelado corto («shampo dreene», «vivacina», «atamel 500») | 0 (determinista) |
| `capa` | **todo lo demás**, verbo de consulta incluido (frases, datos personales, atributos, dudas) | 1 llamada |
| `fail-safe` | timeout, proveedor caído, JSON inválido, capa apagada | 0 (conducta anterior) |

**Un camino rápido por VERBO existió y se retiró** (medido, no por gusto): «tienen», «hay»,
«necesito», «precio»… aparecen también en frases de charla, así que decidían búsquedas falsas —
«Solo una marca o hay varias?» → `marca varias`, «Por ahora no necesito nada» → `ahora`,
«tienen delivery?» → `delivery`, «aceptan zelle?» → zelle. El verbo no es evidencia de fármaco:
es la misma clase de error que la capa existe para evitar, así que ahora lo juzga la capa con el
contexto completo. Lo destapó una sonda que usó el **extractor real**; la primera medición
pasaba `termino_candidato=None`, con lo que el camino rápido nunca disparaba y el fallo quedaba
invisible (lección: medir el camino que de verdad corre).

**Integración en `app/turn.py`** — la capa es el **decisor** del backstop (antes lo era el
heurístico de forma, que sobrevive solo en el camino de fail-safe). No se gasta llamada si el LLM
ya consultó el catálogo.

**Configuración** — `INTENT_LAYER` (default `true`), `INTENT_MODEL` / `INTENT_API_KEY` /
`INTENT_BASE_URL` (proveedor propio opcional, mismo patrón que sus `VISION_*`: vacío = modelo del
chat), `INTENT_TIMEOUT` (12 s).

**Auditoría** — migración `014_med_queries_intent.sql`: `med_queries` gana `intent_fuente`,
`intent_acto`, `intent_motivo`, `intent_confianza`. Si la búsqueda la pidió el **modelo por su
cuenta** (tool call, no el backstop), la fila se marca `llm` — así toda búsqueda es atribuible y se
puede medir **quién** buscó basura.

**Restricciones respetadas:** `app/guards.py` sin un solo cambio, cadena de vetos intacta, cambios
aditivos, nada tocado fuera del repo.

## 3. Evidencia

| Nivel | Qué | Resultado |
|---|---|---|
| Compilación | `py_compile app/*.py` | OK (25 módulos) |
| Tests | `pytest tests -q` | **147 en verde** (28 nuevos de la capa) |
| Corpus real | 50 frases (30 que no deben buscar / 20 consultas legítimas) con el **flujo real** (extractor + camino rápido + capa) y `deepseek/deepseek-v4.1-flash` | **49 correctas · 0 falsos positivos · 0 falsos negativos · 1 inconcluyente** (JSON inválido del modelo, que el fail-closed maneja) |
| Robustez | el mismo corpus con `z-ai/glm-4.7` (otra familia) | **33 correctas · 0 falsos positivos · 0 falsos negativos · 17 inconcluyentes** (todos `timeout`: glm tarda 20-30 s por llamada) |
| Contenedor | `docker compose build nea` + arranque | migración `014` aplicada, health OK |
| End-to-end | escenarios 12/13 (código real, modelo real, catálogo controlado) | **cero consultas al catálogo** para las frases queja |

Artefactos: `corpus-deepseek_deepseek-v4.1-flash.json`, `corpus-z-ai_glm-4.7.json`,
`antes-deepseek-v4.1-flash.{json,md}` (el «antes»), `despues.md` (el «después»), `probe_capa.py`
(sonda determinista), `medir_corpus.py`, `diagnostico_sonnet.py`.

**Lo que el corpus destapó y se corrigió en el camino:** la primera medición dio 82.5% por dos
motivos distintos — (a) truncamiento de mi propio medidor (`max_tokens=300`, artefacto de medición,
no de la capa) y (b) **consultas por categoría o síntoma** («busco algo para el dolor de cabeza»,
«hay crema para las manchas?») que la capa clasificaba como charla. Añadidas al prompt como
consulta con término de categoría: 92.5% → **100%**. Ese hallazgo no estaba en el spec.

## 4. Cómo se comporta ahora (conversación real, escenario 13)

> **Cliente:** tienen macrodantina? → *(lista MACRODANTINA)*
> **Cliente:** No puede ser Indu.
> **Agente:** «No tengo información confirmada sobre ese medicamento en este momento… déjame
> buscarlo en el catálogo. ¿Cuál es…» *(sin «No tengo SER INDU», sin buscar)*
> **Cliente:** Solo una marca? → responde sobre la marca del producto y el genérico, sin buscar
> **Cliente:** Por ahroa no → «Entendido, sin problema…» *(sin buscar)*

## 5. Límites declarados

1. **El camino rápido no es una lista negra, pero existe.** Lo que decide es la capa; el único
   atajo sin LLM es el nombre pelado corto **con evidencia** (dosis, forma farmacéutica o token
   largo). `_PALABRAS_CONVERSACION` es una lista corta que sirve para **desviar** a la capa, no
   para decidir; aun así es lo único heurístico que queda, y el revisor externo pedía retirarlo
   del todo (requeriría respaldo de catálogo/contexto).
2. **El modelo del chat ya no puede saltarse la capa.** La autorización central
   (`_autorizar_busqueda` en `app/tools.py`) intercepta TODA búsqueda, incluida la que pide el
   modelo por su cuenta: sin evidencia fuerte decide la capa y, sin autorización, no se busca — se
   pide aclaración. Verificado con `probe_autorizacion.py` (§6).
3. **Coste.** Una llamada extra por turno **solo** en el camino `capa`. Medido: ~2 s por llamada;
   en el corpus, las consultas con verbo o nombre pelado no pagan nada.
4. **Oscilación del modelo.** Como en F14, las corridas sin `temperature=0` varían; por eso cada
   caso se mide con corpus (40 frases) y no con una sola conversación.
5. **El Laboratorio local no puede medir esto**: sin credenciales de Firebase el catálogo responde
   vacío y el Laboratorio mide estilo (±14 entre corridas idénticas), no precisión de búsqueda.

## 6. Revisión externa del cambio y mejoras aplicadas

Pediste que un revisor externo juzgara si el cambio quedó bien hecho. Se envió el código de la
capa, el diff focalizado, la integración en el turno, los tests, la evidencia del corpus y los
escenarios, con 8 preguntas concretas. **Sonnet 5.5/5/4.6 devolvieron `429 model temporarily at
capacity upstream`** (el proxy lo confirmó; no era el payload: 24k caracteres pasan). La cadena de
revisores cayó a **`openai/gpt-6-astra-pro`**, que es el que firmó el informe — está dicho en el
encabezado de `diagnostico-sonnet-codigo.md`.

**Veredicto: «condicional: dirección correcta, implementación insuficiente para declarar resuelto
F15. Nota: 5/10»**, con 6 cambios prioritarios. Qué se hizo con cada uno:

| # | Su crítica | Estado |
|---|---|---|
| 1 | «No controla herramientas: si solo consulta `entender()` desde el backstop, **el agujero es real**»; ante incertidumbre, aclarar en vez de recuperar el heurístico | **Aplicado.** `_autorizar_busqueda` es la puerta única: TODA búsqueda (incluida la que pide el modelo) pasa por ella; sin evidencia fuerte decide la capa, y sin autorización **no se busca**: se pide aclaración. Ver la prueba directa abajo. |
| 2 | `_veredicto_desde_json` aceptaba `{"motivo":"x"}`, `"hay_consulta":"quizás"`, consulta sin término | **Aplicado.** Esquema estricto: bool real, acto válido, confianza numérica finita en [0,1], término obligatorio en consulta y coherencia entre campos; lo inválido es **no concluyente**. |
| 3 | Construcción del cliente y timeout fuera del `try`; caché con credenciales declaradas en vez de efectivas; sin circuito de corte | **Aplicado.** Timeout validado (no numérico/0/negativo → default), cliente dentro del `try`, clave de caché con la configuración EFECTIVA y **circuito de corte** tras 3 fallos seguidos (120 s). |
| 4 | `camino_rapido` acepta por forma/lista; «`delivery` o `transferencia` cumplen sus condiciones» | **Aplicado en lo medible.** El atajo por VERBO se retiró (medido) y el de nombre pelado ahora exige **evidencia** (dosis, forma farmacéutica o token largo); `delivery` está en la lista de conversación y va a la capa. |
| 5 | `_contexto` insuficiente: sin historial, opciones ni pregunta pendiente | **Aplicado (parcial).** La capa recibe estado compacto: últimos turnos, último término y última lista mostrada. **Pendiente**: un solo `termino` no representa «losartán y metformina» (multi-consulta). |
| 6 | Las columnas en `med_queries` no registran los rechazos; hacen falta eventos por decisión | **Aplicado.** Migración **015 `intent_decisions`**: una fila por decisión (busque o no) con fuente, acto, veredicto, término, candidato, motivo y confianza. |

**Prueba directa del agujero nº1** (`eval/f15-frases-conversacion/probe_autorizacion.py`, en el
contenedor, con el modelo real): se llama `buscar_medicamento` **como lo hacía el modelo del chat**,
con los términos que quedaron en el log de producción:

```
OK  'me robaron el celular ayer'   -> buscar_medicamento('ladrones')          | buscó=False (acto=otro)
OK  'usted es una estafa...'       -> buscar_medicamento('estafa')            | buscó=False (acto=reclamo)
OK  'por ahora no necesito nada'   -> buscar_medicamento('ahora')             | buscó=False (acto=dilatoria)
OK  'vivo en ciudad bolivar'       -> buscar_medicamento('vivo ciudad bolivar')| buscó=False (acto=dato_personal)
OK  'tienen macrodantina?'         -> buscar_medicamento('macrodantina')      | buscó=True  (consulta_medicamento)
OK  'tienen atamel forte?'         -> buscar_medicamento('atamel forte')      | buscó=True  (consulta_medicamento)
```

**Consecuencia de aplicar el fail-closed (y hay que decirlo):** si el proveedor de la capa va lento
o falla, el agente **pregunta en vez de buscar**. Medido con `z-ai/glm-4.7`: sus llamadas tardan
20-30 s y superaban el timeout de la medición, así que el corpus cayó a 33/50 **por latencia del
proveedor, no por la lógica** (todos los fallos eran `fail-safe: timeout`). Es el precio de no
buscar a ciegas; el circuito de corte evita pagar el timeout en cada turno, y `INTENT_LAYER=false`
devuelve el comportamiento anterior si molesta.

**Pendientes declarados** (no los doy por hechos): multi-consulta en un solo mensaje; cierre/límite
de la caché de clientes (la app es de vida larga, no se cierra); corpus con repeticiones
independientes; tests con conteo real de invocaciones por caso; y volver a correr el revisor con
Sonnet cuando el upstream se descongestione (o pasarle los anexos completos, que en modo compacto
no vio: eso fue parte de su queja).

## 7. Entrega: los 4 casos reales antes/después y el Laboratorio

### 7.1 Los 4 casos reales, con el agente corriendo de verdad

Artefacto completo: `escenarios-final.md` (12 escenarios; 225 consultas al catálogo en toda la
batería). Antes = lo documentado en el diagnóstico; después = la corrida final con el código de
F15 (modelo real `deepseek/deepseek-v4.1-flash`, catálogo controlado).

| Caso | Antes | Después |
|---|---|---|
| **1 · apelativo + nitrofurantoina + vitamina C** | término `amor nitrofurantoina 50mg vitamina`; `amor` validaba **TE AMO PRESERV** | lista **nitrofurantoina (50 y 100) + VITAMINA C**; TE AMO PRESERV nunca aparece. *Residual*: «dame 2 cajas de la nitro» sigue sin resolverse como referencia al producto anterior («No tengo 2 NITRO») |
| **2 · `vivacina`** | devolvía **VITAMINA K y VITAMINA C** (fuzzy de 2 ediciones) | **no ofrece nada**: «no lo tengo en el catálogo» y sostiene el no ante la repregunta; no inventa precio |
| **3 · «opción N» en mensaje mixto** | no resolvía la opción y buscaba `macrodantina querer vitamina opción` | **resuelve la opción 2** (agrega VITAMINA K del listado) y aclara «la Vitamina C es la opción 1, dime si te la cambio» |
| **4 · dos bucales + typo (`solunoval`)** | un solo término concatenado + «No tengo SOLUNOVAL BUCAL» y luego la lista | dos búsquedas (SOLUNOVAR y PINVEX) con precios. *Residual*: sigue el aviso contradictorio «No tengo SOLUNOVAL **en solución bucal**. Lo tengo en: solución oral» mientras lista un producto que **se llama** SOLUCION BUCAL — es la plantilla `aviso_forma` del árbol del cliente, no la capa |

### 7.2 Laboratorio del CRM (juez LongCat vía proxy Nous :11500)

Estado fresco **sin TRUNCATE**: cada corrida usa la DB desechable (`nea-db-qa-j`, volumen propio);
el volumen `nea_data` del cliente no se monta ni se toca.

| Corrida | Cuándo | runId | Score | Delta | Veredicto |
|---|---|---|---|---|---|
| A (referencia, árbol F14) | 2026-10-29 23:43 | `run_wmzooniddn4njcbzoi19` | **100** | — | 11/11 verdes |
| B (estado SUCIO, descartada) | 2026-11-03 04:43 | `run_0u9ohnewj90zgf9oq5bk` | 82 | — | 8 verdes, 2 amarillo, 1 rojo — **no es el código: es estado reusado** |
| C (estado FRESCO, código actual) | 2026-11-03 05:2x | `run_jiou5w8bcwxp5tjhnuea` | **82** | **18** vs A | 9 verdes, 2 rojos — **los dos rojos son del código nuevo del cliente** (§7.3 y §7.4) |

**Veredicto honesto del delta:** la corrida limpia da **82** contra **100** de la referencia ⇒ delta
**18**, por encima del umbral 8 del spec. **No es el cambio F14/F15**: los 9 casos restantes están
todos en verde, y los 2 rojos están atribuidos a los commits nuevos del cliente — uno probado con
dos imágenes (§7.3) y el otro en su ruta de OCR/foto (§7.4). Además la banda de oscilación medida
entre corridas idénticas es ±14, así que un delta de 18 sin atribución no se puede leer como
regresión propia.

**Lección medida (queda en la skill):** reusar la DB entre corridas del Laboratorio produce rojos y
amarillos FANTASMA — las personas del Lab usan wa_identities fijas, así que una conversación previa
deja el chat en handoff y la corrida nueva arranca con el cliente ya «atendido» («Hola» → «Listo, ya
avisé al equipo»). El estado fresco se logra recreando la DB **desechable** (nunca el volumen
`nea_data` del cliente).

Histórico de la misma sesión (mismo código, para leer la oscilación): `run_e7vfzuhkmf8t59mibgh8`
= 100, `run_7k61nvr5kfdp2tqc8l8y` = 86 (delta −14), `run_o4oscqnb6bl0r8amwwez` = 100 (delta +14) —
la banda de ±14 entre corridas idénticas que ya se documentó en F14. **El Laboratorio mide
comportamiento, no precisión de búsqueda**: sin credenciales de Firebase su catálogo responde
vacío (por eso los casos dicen «no tengo losartán/daflon»). La precisión se mide con el corpus y
las conversaciones simuladas (§3 y §7.1).

### 7.3 El rojo `fuera_de_kb` del Laboratorio: es del commit del cliente, no de F14/F15

El Lab marcó **rojo** la persona `fuera_de_kb` con hallazgo `alucinacion`:

> cliente: «¿hacen entrega a domicilio los domingos?»
> agente: «¡Claro que sí! 🛵 Hacemos entregas a domicilio. ¿Qué medicamento quieres que te
> enviemos y a qué dirección?»

**Atribución medida** (mismo escenario, misma DB, mismo modelo, dos imágenes):

| Imagen | Respuesta a «¿hacen entrega a domicilio los domingos?» |
|---|---|
| `vocerocrm-nea:pref14` (sin el commit del cliente) | **(silencio)** — no afirma nada; escala en el mensaje siguiente |
| `vocerocrm-nea:latest` (con el commit `732cf1e`) | «¡Claro que sí! 🛵 Hacemos entregas a domicilio.» |

La frase vive en **`app/turn.py:607` y `:614`**, dentro del guard `_es_peticion_de_envio` que añadió
el commit **`732cf1e`** («"quiero que me lo envien" es una peticion de ENTREGA, no una busqueda»,
2-11-2026). Su regex `(delivery|…|domicilio)` atrapa «entrega **a domicilio** los domingos» y
contesta al envío sin saber el horario. **No es F14 ni F15**: es su guard contestando de más.
Sugerencia para ellos: en ese guard, si el mensaje pregunta por horario/condición y no hay dato,
escalar en vez de afirmar (y cubrir «vencido», «cambio», «devolución»).


### 7.4 El rojo `receta_foto`: también de la ruta nueva de OCR/foto

> cliente: (foto de receta)
> agente (1º): «No me llegó ninguna imagen por este canal — aquí solo me llega texto. Escríbeme el
> nombre de los medicamentos…»
> agente (2º): «**Leí tu receta: Losartán 50 mg, Daflon 500 mg y Esoz 40 mg.** Ninguno de los tres
> está en nuestro catálogo…»

El juez lo marca `alucinacion` y **aplica bien su regla F4** (`src/server/ai/prompts.ts`): solo
cuenta como alucinación si el agente **afirma** un dato concreto — aquí afirma haber leído una
imagen. La frase «Leí tu receta» **no existe en el código del agente** (`grep` sobre `app/`), y los
términos buscados en esa corrida (`losartan 50`, `daflon 500`, `esoz 40 mg`) son justo los de la
receta de prueba: vienen por la **ruta de OCR/foto del cliente** (sus 24 commits recientes:
«cada caja en TRES lineas (el formato real del OCR)», «el termino de cada envase va limpio (y traza
del OCR)»). Ni F14 ni F15 tocan imagen/OCR. Sugerencia para ellos: no afirmar «leí tu receta» cuando
el texto vino del OCR, y no decir «ninguno está en el catálogo» cuando el catálogo del entorno de
prueba está vacío.
