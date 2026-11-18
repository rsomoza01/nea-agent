# F15 — Diagnóstico: frases de conversación que se buscan como medicamento

**Estado:** diagnóstico medido, SIN implementar. Nada de esto se ejecutó ni se tocó en producción.
**Origen:** quejas del cliente (oct-2026), verbatim:

> - **«No puede ser Indu.»** — el usuario expresa que el laboratorio del medicamento no puede
>   ser Indu (o de la India); no es una petición de medicamento.
> - **«Vivo en Ciudad Bolivar.»** — el usuario indica dónde vive y el agente intentó buscar
>   *Bolivar* como medicamento, igual que *Vivo* y *Ciudad*.
> - **«Solo una marca?»** — pregunta por la marca (asumible: el laboratorio) del medicamento.
> - **«Por ahroa no»** — el agente asumió que «ahora» era un medicamento e intentó buscarla.

**Pregunta del usuario (a responder aquí):** ¿vale la pena investigar cómo hacer que el agente
**entienda primero la conversación** y ubique dónde está la pregunta de medicamento, en vez de
buscar cualquier frase como si fuera un fármaco?

**Respuesta corta:** sí, y el diagnóstico cambia el sitio del fix: no es "más frases en la
lista negra", es **de dónde sale la decisión de buscar**. Hoy esa decisión se toma por la
*forma* del texto, no por *evidencia de fármaco*.

---

## 1. Qué se midió (todo ejecutado sobre el árbol `a07f322`, local)

Arnés de funciones (`_extraer_termino_medicamento`, `hay_senal_de_farmaco`, `_parece_*`):

| Frase | término que se buscaría | `hay_senal_de_farmaco` | `_parece_consulta_medicamento` | `_parece_nombre_producto` |
|---|---|---|---|---|
| `No puede ser Indu.` | `ser indu` | **True** (`tokens_farmaco=['indu']`) | False | False |
| `Vivo en Ciudad Bolivar.` | `vivo ciudad bolivar` | **True** | **True** | **True** |
| `Solo una marca?` | `marca` | **True** | False | False |
| `Por ahroa no` | `ahroa` | **True** | False | False |
| `Vivo en Ciudad` | `vivo ciudad` | False | **True** | **True** |

Dos hechos incómodos:

1. **El extractor SIEMPRE devuelve un término.** `_extraer_termino_medicamento` quita verbos,
   relleno, apelativos, categorías terapéuticas y vías de administración; lo que quede, si
   tiene ≥3 letras, es "término". `'ser indu'`, `'marca'`, `'ahroa'`, `'vivo ciudad bolivar'`.
2. **La "señal de fármaco" no discrimina.** `hay_senal_de_farmaco` devolvió **True en 4 de las
   5 frases** — porque `tokens_farmaco` es, en la práctica, «cualquier palabra de ≥4 letras que
   no sea verbo de consulta». `'indu'` cuenta como fármaco. Esa bandera es la que después
   habilita la búsqueda, así que la puerta está abierta de par en par.

Y la puerta concreta que sí decide buscar está localizada: `_parece_nombre_producto`
(`app/turn.py`, ~4899) acepta **cualquier oración** que (a) no lleve `?`, (b) tenga ≤4 palabras,
(c) no contenga saludo/relleno conocido y (d) tenga una palabra de ≥5 letras. «Vivo en Ciudad
Bolivar.» cumple las cuatro: sin signo de pregunta, 4 palabras, ninguna en la lista de saludos,
y `ciudad`/`bolivar` tienen ≥5 letras ⇒ **True** ⇒ `_parece_consulta_medicamento` ⇒ **True** ⇒
se busca. El adjetivo "deliberadamente estrecho" del docstring describe el caso que se quiso
evitar («2 cajas», «que tal»), no este.

## 2. Reproducción end-to-end (no es teoría)

Con el simulador de conversaciones (`eval/f14-busqueda/simular_conversaciones.py`, escenario
`12-frases-que-no-son-medicamento`), la frase «Vivo en Ciudad Bolivar.» produjo **13 consultas
al catálogo** en un solo turno:

```
vivo ciudad bolivar → vivo → vivo ciudad oblivar → vivo ciudad bloivar → vivo ciudad boilvar
→ vivo ciudad bolviar → vivo ciudad boliavr → vivo ciudad bolivra → vivo ciudad volivar
→ boliva → boliv → boli → bol
```

Es decir: la frase de ubicación entra al buscador, y el **cascada de recuperación** del árbol
(transposiciones + truncamientos progresivos) la convierte en `bol`, `boli`, `boliv`… El
cliente ve «no tengo Bolivar» y el log `med_queries` se llena de basura que después contamina
el catálogo de typos y los alias derivados (F14.4). **El daño no es solo la respuesta mala: es
la contaminación del aprendizaje.**

Contra-caso medido (escenario `13-frases-con-contexto`): las MISMAS frases **en mitad de la
conversación** (después de listar un producto) **no dispararon ninguna consulta** — consultas
= `['macrodantina', 'macrodantina']`; las respuestas fueron conversacionales y correctas
(«¿Qué pasa con la macrodantina, el precio o algo más?», «Perfecto, aquí estoy cuando lo
necesites 👋»). El veneno está sobre todo en el mensaje **sin contexto previo**.

Límite honesto de esta medición: de las 4 quejas reproducí end-to-end **1** («Vivo en Ciudad
Bolivar.», el caso con cascada). En las otras 3 el *terreno* está medido (término basura +
bandera en True), y en producción el modelo del cliente sí las buscó, pero **no** las pude
reproducir con su modelo: este entorno corre LongCat y ellos pasaron el chat a DeepSeek v4.1
Flash (Ollama Cloud) en los últimos commits. Para cerrar el mapa hace falta correr los dos
escenarios con su modelo.

### 2.1 F15.0 — reproducido CON el modelo del cliente (`deepseek/deepseek-v4.1-flash`)

Corridos los escenarios 12 y 13 con el modelo de producción
(`eval/f15-frases-conversacion/antes-deepseek-v4.1-flash.{json,md}`):

**Queja 1 REPRODUCIDA al pie de la letra** (escenario 13, la frase llega después de un listado):

> **Cliente:** No puede ser Indu.
> **Agente:** ⚠️ **No tengo SER INDU**. No te ofrezco otro medicamento parecido para no
> confundirte: son cosas distintas. ¿Me confirmas el nombre tal como viene en la caja, o te paso
> con un auxiliar?

El agente le contesta al paciente que "no tiene SER INDU" — la frase sobre el **laboratorio**
convertida en medicamento, exactamente el síntoma reportado.

**Queja 2 REPRODUCIDA en el log** (escenario 12): aunque la respuesta visible fue correcta
(«Perfecto, Ciudad Bolívar anotado 👍 ¿Qué andas buscando hoy?»), el agente **igual consultó el
catálogo 13 veces**:

```
vivo ciudad bolivar → vivo → (7 transposiciones) → boliva → boliv → boli → bol
```

Es la peor variante del daño: **el cliente no lo ve, pero `med_queries` se contamina** — y de
ese log salen los alias y el catálogo de typos de F14.4 (una búsqueda basura hoy es una
"corrección" aprendida mañana).

**Las quejas 3 y 4 (`Solo una marca?`, `Por ahroa no`)** salieron conversacionalmente bien en
ambos modelos en estas corridas, pero el terreno está medido (§1: el extractor produce `marca` /
`ahroa` y la bandera dice True), así que siguen dentro del alcance: dependen del modelo y del
contexto.

**Dato suelto a vigilar:** en la corrida con DeepSeek aparece una consulta a `sildenafilo` que
ninguna frase del guion justifica — parece una **llamada suelta del modelo** (alucinada) al
buscador. No se ve en respuesta al cliente, pero conviene mirarlo cuando se instrumente el log
de la capa (F15.1 §3).

**Diferencia entre modelos (por qué el "antes" se congela con el del cliente):** LongCat y
DeepSeek eligen **distinto camino** ante las mismas frases — LongCat buscaba `vivo ciudad
bolivar` *y* contestaba conversacional; DeepSeek contesta conversacional en el escenario 12 pero
en el 13 suelta el «No tengo SER INDU». El fallo no es de un modelo: es del **decisor** común
(el heurístico de forma + la bandera de señal).

## 3. La tensión de fondo (por qué "más reglas" no alcanza)

El árbol tiene hoy una familia de filtros deterministas por frase/familia: `_parece_direccion`,
`_parece_referencia_sin_farmaco`, `_es_negativa_o_despedida`, `_es_reclamo_o_cortesia`,
`_es_solo_dosis_y_forma`, `_es_linea_descriptiva`, `_es_etiqueta_ocr`, `_SALUDOS_CONSULTA`…
y ninguno cubre las 4 quejas: `_parece_direccion('Vivo en Ciudad Bolivar.')` = **False**.

Pero el problema no es que falte la regla de "vivo en X". Es que compiten dos necesidades
legítimas:

- **«Shampo Dreene» debe buscar** (medido en su día: exigir verbo provocaba falsos negativos
  con nombres pelados).
- **«Vivo en Ciudad Bolivar» no debe buscar** (esta queja).

Ambas son "≤4 palabras, sin `?`, con palabra larga". Ninguna lista negra resuelve eso de
forma estable: cada queja nueva es una frase nueva. Lo que falta es **evidencia positiva de
que hay un fármaco** — y, cuando no la haya, **preguntar** en vez de buscar.

## 4. Opciones de diseño

> **DECISIÓN DEL USUARIO (30-oct-2026), registrada aquí antes de diseñar:**
> *«no quiero atarlo a una regla específica por palabras, como especializar más el agente
> para que sea más inteligente de diferenciar primero las conversaciones y luego entender cuál
> es el medicamento»*. Por lo tanto **A y B quedan descartadas como enfoque principal** (son
> especializaciones por palabra/clase): la investigación se centra en una **capa que entiende el
> acto de conversación ANTES de decidir si hay consulta de medicamento**, y solo después ubica el
> término. Modelo para este trabajo: tier **pro** (elegido por el usuario).

| | Enfoque | Qué resuelve | Costo / riesgo | Veredicto |
|---|---|---|---|---|
| **A** | Reglas por CLASE (preposición de lugar + topónimo, `ser/estar + nombre propio`, pregunta de atributo, dilatoria temporal) | las 4 quejas y sus primos | bajo | **descartado como enfoque** (es la lista negra un escalón más arriba; el usuario lo rechazó explícitamente) |
| **B** | Evidencia positiva obligatoria para buscar (token de fármaco conocido/afijo, dosis+unidad, forma farmacéutica, match de catálogo); sin prueba ⇒ preguntar | el caso general | medio; falso negativo en typos sin evidencia | **no como filtro de palabras**, sí como *señal de apoyo* de la capa de comprensión |
| **C** | **Capa de comprensión del acto de conversación**: clasifica el mensaje (¿saludo? ¿dato personal? ¿pregunta por un atributo? ¿dilatoria? ¿reclamo? ¿consulta de medicamento, y cuál es el término?) y **solo entonces** se busca; en duda, pregunta | el caso general, incluidos los que aún no reportaron | +1 llamada al modelo SOLO cuando el camino rápido no es concluyente; hay que cuidar estilo y latencia | **elegido: es la dirección a diseñar** |
| **D** | Híbrido A+B como *fast-path* + C como decisor en la duda | todo lo anterior con coste controlado | bajo-medio | es la forma de implementar C sin pagar una llamada en cada turno |

**Lo que hay que diseñar de C (esto es el trabajo de F15, no la implementación aún):**

1. **Dónde vive la capa.** Hoy la decisión de buscar la toma un heurístico de FORMA
   (`_parece_nombre_producto`) dentro del *backstop* determinista, cuyo objetivo legítimo es que
   el agente **no se olvide** de buscar cuando el modelo no llama la herramienta (su origen
   documentado: «Shampo Dreene» no se buscaba y el LLM negaba de memoria). La capa nueva debe
   **sustituir ese heurístico como decisor**, no añadirse al lado.
2. **Camino rápido vs duda.** Si el mensaje trae verbo de consulta (`tienen`, `busco`,
   `precio de`), o un término que **ya matchea el catálogo**, se busca sin consultar nada: la
   capa solo entra cuando hoy el heurístico *adivina* (sin verbo, sin match, mensaje corto).
   Así el coste extra se paga únicamente donde hoy está el error.
3. **Contrato de salida verificable.** La capa debe devolver algo auditable —
   `{acto, hay_consulta, termino, confianza, motivo}` — que se **loguee** junto a la búsqueda
   (`med_queries` o campo de auditoría) para poder medirla y para poder culpar a quien falló
   (capa o catálogo) en un caso real.
4. **Fail-safe declarado.** Si la capa no responde (timeout, proveedor caído): ¿se busca como
   hoy o se pregunta? Decisión de producto — recomendación: **fallback al comportamiento actual
   + log**, para no cambiar el flujo cuando el proveedor falla; la mejora se mide con la capa viva.
5. **No tocar la cadena de vetos**: la capa decide *antes* de la búsqueda; `guards.py` y los
   vetos siguen siendo la última palabra sobre la respuesta.
6. **Proveedor propio y configurable** (mismo patrón que su `VISION_*` reciente):
   `INTENT_MODEL` / `INTENT_API_KEY` / `INTENT_BASE_URL`, con vacío = usar el modelo del chat.
7. **Riesgo medido a respetar**: en F14 se comprobó que **una prohibición extra en el chasis
   desvía el estilo** en turnos sin catálogo. Por eso el contrato de la capa va en SU prompt
   (aislado), no en el prompt del chat.

**La decisión de producto que hay que tomar:** ¿preferimos *buscar de más* (molesta con
respuestas sobre palabras que no son medicamentos, contamina `med_queries`) o *buscar de menos*
(el cliente escribe un nombre raro y el agente pregunta en vez de negar)? La queja actual dice
que el segundo error se tolera mejor — es exactamente el «si no tiene la seguridad de cuál es
la pregunta real, que pregunte» que planteas.

## 5. Plan de evaluación (antes de tocar código)

1. **Corpus de no-consultas** (~40): las 4 quejas + variantes por clase (lugar, laboratorio,
   marca, dilatoria, cortesía, datos del paciente, reclamos) + las que ya cubre el árbol.
2. **Corpus de consultas legítimas** (~40): nombres reales del log `med_queries`, con typos
   (`vivacina`, `omeprasol`, `lorsatan`), con dosis, con forma, con marca, y nombres pelados
   (`Shampo Dreene`).
3. **Métrica**: por un lado consultas basura evitadas; por el otro **falsos negativos**:
   consultas legítimas que el agente deja de buscar. El objetivo no es 100/100 en el primero,
   es **cero falsos negativos** en el segundo (regla: ante duda, preguntar).
4. **Tres niveles**, como en F14: funciones (arnés determinista) → conversación (simulador,
   escenarios `12`/`13` + con su modelo) → Laboratorio (solo estilo; recordar: el Laboratorio
   local no tiene catálogo y oscila ±14).
5. Reproducir primero las 4 quejas **con el modelo del cliente** (DeepSeek v4.1 Flash) para
   tener el "antes" verdadero.

## 6. Restricciones (las mismas de F14)

- `app/guards.py` **intocable**; cadena de vetos en `turn.py` intacta.
- Cambios **aditivos**; nada de reescribir el flujo de turno.
- Push SOLO a `desarrollo` de `rsomoza01`/`gentefarma`; nunca a `main` ni al upstream.
- Simulaciones con modelo barato. NO usar Ollama local (GPU inestable).

## 7. Orden propuesto de implementación (re-scopado tras la decisión del usuario)

- **F15.0** — reproducir las 4 quejas **con el modelo del cliente** (escenarios 12/13) y congelar el "antes". *(en curso)*
- **F15.1** — **diseño de la capa de comprensión** (C): contrato de salida, dónde vive, camino rápido vs duda, fail-safe, log de auditoría, prompt aislado. Nada de reglas por palabra.
- **F15.2** — **prototipo fuera del turno**: la capa + los dos corpus (~40 no-consultas / ~40 consultas legítimas) medidos **offline**, sin tocar el flujo. Criterio: cero falsos negativos en consultas legítimas.
- **F15.3** — **integración**: la capa sustituye al heurístico de forma (`_parece_nombre_producto`) como *decisor* del backstop, conservando el camino rápido y el fail-safe; tests por acto de conversación (saludo, dato personal, atributo, dilatoria, reclamo, consulta).
- **F15.4** — **medición end-to-end**: escenarios 12/13 en los dos modelos, las 4 quejas antes/después, regresión completa y reporte. Laboratorio solo para estilo (sin catálogo, oscila ±14).
- **F15.5** — configuración `INTENT_MODEL/API_KEY/BASE_URL` + documento de operación (si F15.2/F15.3 lo justifican).

**Modelo:** tier **pro** elegido por el usuario (Nous-only: `openai/gpt-6-astra-pro`, o
`anthropic/sonnet-4.5` en el Portal a 3.00/15.00 $/M, ~7x el `z-ai/glm-4.7` que la skill
considera suficiente para esta categoría). El cambio de modelo de la sesión lo aplica el usuario
(`/model`); el spec se diseñó para que el trabajo arranque en cuanto esté puesto.
