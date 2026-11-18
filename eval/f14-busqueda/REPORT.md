# F14 — Precisión de búsqueda del agente NEA (implementación + validación)

Rama `desarrollo` · repo `agentNEA` · spec: `docs/F14-diagnostico-busqueda.md`
(diagnóstico por simulación del código real, commit `9127734`).

Artefactos de esta fase (todo reproducible):

| Archivo | Qué es |
|---|---|
| `eval/f14-busqueda/simular_casos.py` | simulación determinista de los 4 casos reales (sin LLM); corre por `--root` contra el árbol previo y el actual |
| `eval/f14-busqueda/antes.json` / `ahora.json` | salida cruda de esa simulación: base `1026506` y árbol final mergeado |
| `eval/f14-busqueda/cliente-9880917.json` | salida cruda del árbol del cliente (los 241 commits) para comparar los tres |
| `eval/f14-busqueda/sonda_catalogo.py` | sonda contra el catálogo real del CRM desde dentro del contenedor |
| `eval/f14-busqueda/simular_conversaciones.py` | simulador de CONVERSACIONES completas (código real + modelo real + catálogo controlado) |
| `eval/f14-busqueda/conversaciones-f14.md` / `-cliente-sin-f14.md` | las 11 conversaciones de cada árbol, legibles (+ `.json` con las respuestas crudas) |
| `tests/test_f14_busqueda.py` + `tests/conftest.py` | 102 regresiones de los 4 casos reales, sus bordes y las NO-regresiones |
| `migrations/013_med_aliases.sql` | tabla `med_aliases` + semilla curada (17 pares) |

---

## 1. Qué se implementó

### F14.1 — Apelativos de cortesía en TEXTO ESCRITO (`app/turn.py`)
`_MULETILLAS_HABLA` solo se aplicaba en `_extraer_termino_transcripcion` (voz).
Ahora `_extraer_termino_medicamento` excluye también `APELATIVOS` (una sola
fuente, en `app/relevancia.py`: amor, vida, reina, corazón, mami, hermano,
camarada, pana…, sumados a `_FUNCIONALES` para que tampoco puedan validar un
producto). La comparación va por clave sin tildes (`_sin_tildes`), porque el
cliente escribe "corazón" y la lista está sin tildes.

De paso, el mismo caso pedía **"vitamina C"**: el extractor descartaba la letra
por corta y el término quedaba en `vitamina`, que matchea igual VITAMINA K y
VITAMINA C. Ahora una letra de vitamina tras "vitamina(s)" se conserva
(`vitamina c`), y sigue descartándose suelta ("dame c").

### F14.2 — "opción N" en mensaje mixto (`app/turn.py`, `app/tools.py`)
`_menciona_opcion_numerada()` detecta la selección explícita; si hay
`last_options`, el turno marca `runtime.opcion_resuelta` **antes** de que se
extraiga término, y el backstop de catálogo no reinterpreta el resto del mensaje
como medicamento. Antes, "Precio de la macrodantina?? Voy a querer una vitamina
C de la opción 9" extraía `macrodantina querer vitamina opción` y buscaba esa
basura, pisando `last_options` y dejando la opción 9 fuera de rango.

### F14.3 — Redacción sin contradicción (`app/prompt.py`, `app/tools.py`)
Regla PROHIBITIVA en el chasis ("ABRAS con una NEGACIÓN cuando tienes productos
que ofrecer") + instrucción de apertura en el resultado de `buscar_medicamento`
(`_formatear_instrucciones_busqueda`). El matching del caso 4 estaba bien: el
fallo era abrir con "No tengo SOLUNOVAL BUCAL" y listar resultados debajo.

### F14.4 — Alias de escritura DATA-DRIVEN (`app/aliases.py`, migración 013)
- Tabla `med_aliases` (alias, canonico, origen, nota), sembrada en la migración
  e idempotente (`ON CONFLICT (alias) DO NOTHING`). Se carga al arranque
  (`app/main.py` → `cargar_desde_store`), siempre sin poder tumbar el arranque.
- En `buscar_medicamento`, antes de consultar: alias exacto (o alias + dosis /
  forma: "solunoval 500 mg", "solunoval solución bucal") → se reescribe a la
  grafía canónica y **se apaga la tolerancia a typos** de esa búsqueda.
- **Fail-safe**: si la grafía canónica devuelve 0 productos, se reintenta con la
  grafía ORIGINAL (con fuzzy). Un alias solo puede ayudar, nunca esconder.
- Semilla (17 pares) con origen declarado: `fexofenasina→fexofenadina` sale del
  log real `med_queries` (misma conversación, dos grafías), `solunoval→solunovar`
  del caso 4 del cliente, el resto de typos medidos en el código y ortográficas
  venezolanas frecuentes (`azitromisina`, `flucozanol`, `amoxacilina`,
  `metmorfina`, `clonacepan`, `bitamina c`…).
- **Reglas de oro** (documentadas en el módulo y en el SQL): nunca marca →
  principio activo (eso es `_buscar_por_principio_activo` y una reescritura
  forzada escondería el producto de marca); nunca pares con final ambiguo
  (-o/-a). `PgStore.derivar_aliases()` deriva candidatos del log para revisión
  humana (no auto-inserta).

### F14.4b — El fuzzy de 2 ediciones ya no cruza de categoría (`app/relevancia.py`)
Con UN solo token de señal, la distancia 2 exige evidencia de parentesco:
prefijo común de 3+ letras o desplazamiento de un carácter. Evidencia medida
(matriz completa en `ahora.json` → `matriz_typo`):

| Par | dist | prefijo | desplazamiento | antes | ahora |
|---|---|---|---|---|---|
| vivacina → vitamina | 2 | 2 | no | **match (VITAMINA K y C)** | **no match** |
| morfina → formina | 2 | 0 | no | match | no match |
| omeprasol → omeprazol | 1 | 6 | no | match | match |
| diclofencao → diclofenaco | 2 | 8 | sí | match | match |
| lorsatan → losartan | 2 | 2 | sí (la "r" se corre) | match | match |
| metmorfina / clonacepan / flucozanol | 2 | 3-5 | — | match | match |

15/15 typos reales siguen matcheando; los cruces de categoría quedan bloqueados.

### F14.5 — Multi-medicamento por conectores (`app/turn.py`)
`_partir_por_conectores` divide por `,` `;` `y` `también` `además` **solo si
AMBOS lados conservan un token con cuerpo de fármaco** (filtro conservador:
`tokens_posible_farmaco`, que descarta relleno, vocabulario de negocio y verbos
de discurso) y ninguno es solo co-formulación. Guardas que evitan falsos cortes:

- "tienes atamel y su precio" → 1 consulta (`su precio` no tiene fármaco).
- "quiero amoxicilina y que me digas el precio" → 1 consulta (el lado derecho
  solo aportaba el verbo `digas`).
- "amoxicilina y ácido clavulánico" / "vitamina c y zinc" / "losartán e
  hidroclorotiazida" → 1 consulta (es UN producto).
- El patrón clásico de dosis repetida ("clopidogrel de 75 losartán de 50
  atorvastatina de 30") sigue partiendo en 3.

---

## 2. Los 4 casos reales: antes / después

Medido por `simular_casos.py` con el árbol previo (`git archive HEAD`) y el
actual. Salida cruda: `antes.json` / `ahora.json`.

### 2.0 Integración con los 241 commits del cliente (importante)

Al ir a pushear apareció que `origin/desarrollo` había avanzado **241 commits**
(`9880917`, incluida la sincronización con `main`). Ese árbol **ya resolvía parte
de F14** y era más fuerte en algunos puntos, así que el merge
(`f14-prev-merge` → `desarrollo`) quedó con **lo de ellos + lo mío donde aporta**.
Medido con el mismo arnés (`cliente-9880917.json`):

| Caso | base `1026506` | cliente `9880917` | F14 mergeado |
|---|---|---|---|
| C1 · término extraído | `amor nitrofurantoina 50mg vitamina` | `nitrofurantoina 50mg vitamina c` | `nitrofurantoina 50mg vitamina c` |
| C1 · división en 2 consultas | `[]` (concatenada) | `[]` (lista detectada por su `_parece_lista_medicamentos`) | `['nitrofurantoina 50mg', 'vitamina c']` |
| C1 · `TE AMO PRESERV` como resultado | sí | no (ya lo arreglaron) | no |
| C2 · "vivacina" → filtrados | `VITAMINA K`, `VITAMINA C` | **`VITAMINA K`, `VITAMINA C`** | **`[]`** |
| C3 · "opción N" detectada | — | — (no existe) | **sí** |
| C4 · división en 2 consultas | `[]` | `[]` | `['solunoval solucion bucal', 'pinvex solucion bucal']` |
| Typos que ya funcionaban | 11/11 | 11/11 | 11/11 |
| Falsos positivos históricos | 4/5 | 5/5 | 5/5 |

Qué se conservó de ellos (y por qué no se duplicó):

- **Letra de la vitamina**: su `_es_letra_de_vitamina` (+ `d3`/`k2` y el guard
  `_relevante_letra_de_vitamina`) es más completa que la mía → **se descartó mi
  variante** y queda la suya.
- **Apelativos en texto escrito**: ya quitaban "amor" de la extracción y del
  filtro de relevancia → mi `_APELATIVOS` se mantiene solo como lista explícita
  adicional (no cambia el resultado medido).
- **Tildes**: su extractor ya normaliza ("solunoval solucion bucal").
- **Rescate escalonado y fármaco cabeza** en `filtrar_relevantes`: se conserva
  entero, y mis flags (`permitir_typo`, `typo_estricto`) se tejen dentro de ese
  rescate.
- Migración: su `012_delivery.sql` ocupaba el número 012, así que la mía pasó a
  **`013_med_aliases.sql`**.
- Añadido por el merge: `ToolRuntime.texto_cliente` se inicializa en
  `__init__` (su `_forma_del_pedido` lo leía pero solo lo escribía el turno →
  `AttributeError` si el runtime se usa fuera del turno, como en los tests).

Qué aporta F14 sobre su árbol (lo que sigue pendiente ahí):

1. **C2 — el fuzzy de 2 ediciones ya no cruza de categoría** (`vivacina` sigue
   devolviendo VITAMINA K/C en su árbol).
2. **F14.2 — "opción N" como compra confirmada** con prioridad sobre la búsqueda.
3. **F14.4 — tabla de alias `med_aliases`** (data-driven, con fail-safe).
4. **F14.3 — redacción**: la instrucción del resultado de la búsqueda (apertura
   con lo que SÍ hay; prohibido "está agotado" y la memoria inventada).
5. **F14.5 — partición por conectores** (cubre casos donde su detección de listas
   no llega, p. ej. con verbo de consulta en medio).

### 2.1 Detalle por caso

| Caso (texto real del cliente) | Antes (base) | Después (mergeado) |
|---|---|---|
| 1 · "Buenas tardes!! Mi amor que precio tiene la nitrofurantoina de 50mg y la vitamina C" | término `amor nitrofurantoina 50mg vitamina`; `TE AMO PRESERV. 3PCS` (un preservativo) pasaba el filtro; 1 sola búsqueda | término único `nitrofurantoina 50mg vitamina c`; `TE AMO PRESERV.` descartado; **2 búsquedas**: `nitrofurantoina 50mg` + `vitamina c` |
| 2 · "vivacina" | `VITAMINA K` y `VITAMINA C` (fuzzy de 2 ediciones) | sin resultados (0 productos); ni vitaminas ni invención |
| 3 · "Precio de la macrodantina?? Voy a querer una vitamina C de la opción 9" | `macrodantina querer vitamina opción`; la opción 9 no se resolvía como selección | `_menciona_opcion_numerada` = True → la selección (opción 9) se resuelve contra `last_options` y **no se busca término nuevo** |
| 4 · "…precio de solunoval solución bucal y pinvex solución bucal" | 1 término concatenado; matching OK pero redacción "No tengo SOLUNOVAL BUCAL" + lista | 2 consultas (`solunoval…` / `pinvex…`), `solunoval → solunovar` por alias (sin fuzzy), y la regla F14.3 obliga a abrir con lo que SÍ hay |

No-regresiones verificadas en el mismo barrido: 11 typos reales que ya
funcionaban siguen matcheando (`omeprasol`, `diclofencao`, `amoxicilna`,
`solunoval`, `fexofenasina`, `azitromisina`, `metmorfina`, `clonacepan`,
`shampo dreene`, `acetaminofen 650 mg`, `lorsatan`) y los 5 falsos positivos
históricos siguen descartados (`hasta`→PASTA PRIMOR, `todo`→DOVE,
`tarda delivery`→VENDA, `manana hora`→COMPOTA, `amor`→TE AMO PRESERV).

### 2.2 Conversaciones simuladas (código real + modelo real + catálogo controlado)

`eval/f14-busqueda/simular_conversaciones.py` corre **dentro del contenedor `nea`**, con
el código real del agente (`handle_flush` → `run_turn` → herramientas → vetos), el **modelo
real** (LongCat por el proxy) y un CRM de mentira que sirve un **catálogo controlado** con
los nombres reales del diagnóstico (más el ruido histórico: TE AMO PRESERV, VITAMINA K/E,
PASTA PRIMOR…). Sin catálogo controlado no se podría medir nada: el entorno local no tiene
credenciales de Firebase y el catálogo del CRM responde vacío.

Artefactos: `conversaciones-f14.md` / `.json` (árbol con F14) y
`conversaciones-cliente-sin-f14.md` / `.json` (control: árbol del cliente, `9880917`).
11 escenarios, ~34 turnos por corrida.

| Escenario | Árbol del cliente (sin F14) | Con F14 |
|---|---|---|
| 1 · apelativo + nitrofurantoina + vitamina C | lista nitrofurantoina (2) y "VITAMINA C, K, E" | igual en la separación; `TE AMO PRESERV.` nunca aparece |
| 2 · `vivacina` | muestra **VITAMINA C, K, E** y solo admite el error si el cliente insiste | **no muestra nada**: «no lo tengo en el catálogo» y sostiene el no ante la repregunta |
| 3 · "opción 2" en mensaje mixto | rechaza la opción y repite la pregunta | **resuelve la opción 2** (agrega VITAMINA K del listado anterior), responde el precio de la macrodantina y aclara «la opción 2 es vitamina K, no C» |
| 4 · solunoval + pinvex (typo + 2 meds) | ambos productos con precio | ambos productos con precio (y el alias `solunoval→solunovar` deja el término canónico) |
| 5 · typos ya soportados | correctos | correctos (sin regresión) |
| 6 · lista de medicamentos + "opción 2" | correcto | correcto (agrega LEPRIT y avisa de EVIGAX) |
| 7 · dos medicamentos con conector | correcto | correcto (dos búsquedas reales) |
| 8 · amoxicilina + ácido clavulánico | una sola consulta con el combinado | igual (no se parte) |
| 9 · cortesía sin consulta | saluda, no lista | saluda, no lista |
| 10 · macrodantina (marca en catálogo) | la muestra | la muestra (el alias no esconde marcas) |
| 11 · "vitamina c" / "vitamina k" | C traía también K y E | **solo C**, y K solo K |

#### La simulación de conversaciones encontró dos huecos que el arnés por funciones no veía

El arnés por funciones mide `es_relevante` / `filtrar_relevantes` / la extracción: con eso,
C2 parecía resuelto. La conversación completa demostró que no:

1. **`vivacina` seguía ofreciendo vitaminas** («Asumí que buscas VITAMINA 👍» → VITAMINA C, K,
   E). El filtro de relevancia acertaba; la puerta trasera eran las **tres recuperaciones por
   catálogo** del árbol integrado ("núcleo", "núcleo ausente" y "tercera pasada": typos
   largos), que validaban el resultado contra el término **ya corregido** (`vitamina`) y
   nunca contra el original (`vivacina`). Fix: `relevancia.correccion_creible()` — la misma
   regla de C2 (prefijo 3+ o desplazamiento de un carácter) — en las **tres** puertas. Sus
   casos documentados siguen pasando (`hidrocoticida→hidroclorotiazida`,
   `cardevidol→carvedilol`, `omeprasol→omeprazol`, `hialuronico→hialuronato`).
2. **`vitamina c` traía también VITAMINA K y VITAMINA E**: el guard por producto acertaba,
   pero el **rescate escalonado** se apoyaba en el token genérico `vitamina`. Fix: en
   `filtrar_relevantes`, si el término nombra una letra de vitamina, se descartan los
   productos de otra vitamina **antes** del rescate (`vitamina` sin letra sigue devolviendo
   todas, que es lo correcto).

Ambos fixes tienen su regresión en `tests/test_f14_busqueda.py` (102 tests en verde).

#### Observaciones que NO son de F14 (código del cliente, quedan reportadas)

- *"dame 2 cajas de la nitro"* → «⚠️ No tengo 2 NITRO»: la referencia coloquial al producto
  recién listado no se resuelve (debería interpretarse como cantidad del último producto).
- El aviso de forma dice «No tengo SOLUNOVAL en solución bucal. Lo tengo en: solución oral»
  mientras lista un producto que **se llama** «SOLUCION BUCAL».
- Un mensaje con **solo una dosis** (*"que tienen de 500 mg?"*) deja al agente en **silencio**:
  agota las rondas de herramientas y corta sin texto (`turno: demasiadas rondas de
  herramientas — corto sin texto`).

---

## 3. Validación

### Nivel 1 — compilación
`python -m py_compile app/*.py` → OK (22 módulos).

### Nivel 2 — regresiones
`pytest tests -q` → **102 passed**. Cubren los 4 casos reales, los bordes de cada
fix (apelativos varios, letras de vitamina, negativos de "opción N",
co-formulación, alias con dosis/forma, fail-safe, semilla del SQL) y la
**integridad de la cadena de vetos G1-G12** (`guards.py` sigue sin conocer F14,
las 13 plantillas y los detectores siguen ahí).

Nota: el árbol de `desarrollo` NO tenía `tests/` (los 462 tests viven en la rama
`merge-develop` y su árbol importa módulos que aquí no existen: `app.agenda`,
`app.crm_brains`). Se añade `tests/` nuevo con las regresiones de F14, tal como
se acordó.

### Nivel 3 — contenedor
`docker compose build nea` → `vocerocrm-nea:latest` reconstruida; contenedor
levantado con una **DB desechable** (`nea-db-qa-f14`, volumen propio) para no
tocar `nea_data`. Log de arranque:

```
nea.db: migración: aplicando 013_med_aliases.sql
nea.main: migraciones aplicadas — DB lista
nea.aliases: aliases: 17 pares cargados de med_aliases
```

Es decir: migración + carga al boot verificadas contra un Postgres real.

### Nivel 3b — simulador de conversaciones

`simular_conversaciones.py` contra el contenedor real: 11 escenarios por árbol,
código real + `meituan/longcat-2.0` real + catálogo controlado. Salida legible en
`conversaciones-f14.md` / `conversaciones-cliente-sin-f14.md` (§2.2) y cruda en los `.json`.

Límite medido y declarado: **la corrida de conversaciones OSCILA**. Con el mismo código,
`vivacina` se resolvió distinto en la batería completa (el modelo buscó por su cuenta el
término genérico) que en las dos corridas dirigidas del mismo escenario (donde se negó
limpiamente). Tras apretar la instrucción `sin_resultados` (F14.3c, prohibido mostrar otro
fármaco «porque suena parecido»), las 2 corridas dirigidas y la batería completa se niegan
sin inventar equivalencia (3/3). Un solo transcript NO es evidencia suficiente para un caso:
se repite el escenario dirigido (`--solo <id>`).

### Nivel 4 — Laboratorio del CRM (juez LongCat vía proxy Nous :11500)

Estado fresco **sin TRUNCATE**: cada corrida usa su propia DB desechable
(contenedor postgres nuevo, volumen propio). El volumen `nea_data` del cliente
nunca se montó ni se tocó.

| Corrida | Árbol | Estado | runId | Score | Veredictos |
|---|---|---|---|---|---|
| C | PRE-F14 (`1026506`) | fresco | `run_wmzooniddn4njcbzoi19` | **100** | 11 verdes |
| D | PRE-F14 (`1026506`) | fresco | `run_6paw36m7688dwyivc5yd` | **100** | 11 verdes |
| A | F14 v1 (spec literal) | fresco | `run_7wubtt236ys0lejv36hn` | 95 | 10 verdes + `comprador_decidido` amarillo |
| B | F14 v1 | reusada (fuga de estado) | `run_hg4e7jf7xwcuf7bcpsgo` | 95 | 10 verdes + `comprador_decidido` amarillo por alucinación de memoria |
| E | F14 v1 | fresco | `run_nxjlx5dvzwo95r7fzlun` | 91 | 10 verdes + `errores_modismos` **rojo** |
| H | F14 final | fresco | `run_e7vfzuhkmf8t59mibgh8` | **100** | 11 verdes |
| I | F14 final | fresco | `run_7k61nvr5kfdp2tqc8l8y` | 86 | 9 verdes + `guard_media_fantasma` amarillo (tono) + `errores_modismos` rojo |
| J | F14 **mergeado** (241 commits del cliente) | fresco | `run_o4oscqnb6bl0r8amwwez` | **100** | 11 verdes (incluye `errores_modismos` y `comprador_decidido`) |

Delta vs baseline histórico (95, `run_pbujqnvwslsu1bjq96v6`): **+5 (H)** y **−9 (I)**
sobre el MISMO código; **+5 (J)** con el árbol mergeado. Criterio de la fase
(delta < 8): **PASS** en H y J; en I el delta lo explica la oscilación del propio
laboratorio (ver abajo), no un cambio de F14 — el árbol mergeado cierra en 100 con
la persona que había fallado en I.

#### La oscilación del Laboratorio manda en la lectura de los scores

H e I son **el mismo código, la misma imagen y el mismo estado fresco** (DB nueva
cada corrida) y dieron 100 y 86: **±14 puntos**, exactamente la banda que la
propia skill de QA documenta para este laboratorio (el agente corre SIN
`temperature=0`; `app/llm.py` no fija temperatura, así que el muestreo es libre).
Conclusión honesta: con 2 corridas NO se puede atribuir una mejora o regresión de
comportamiento por el score. Los dos casos no-verdes de I son, además, de una
clase **ajena a F14**:

| Caso no-verde en I | Evidencia | ¿F14? |
|---|---|---|
| `errores_modismos` ROJO | el agente dijo *"Panadol es paracetamol"* (afirmó composición/equivalencia sin respaldo) | **No**: `afirma_composicion()` devuelve `False` para esa frase en AMBOS árboles (PRE-F14 y F14), medido con el mismo script |
| `guard_media_fantasma` AMARILLO | tono: el agente no dijo que no puede ver imágenes | **No**: F14 no toca el flujo de media ni ese guard |

Y el mismo agujero de guardas quedó medido en los dos árboles de forma idéntica:

```
frase                                          PRE-F14        F14 final
"Panadol es paracetamol, puedo buscar..."      False/False    False/False
"Lamento que el esoz esté agotado."            False/False    False/False
"ya la habías consultado antes y estaba..."    False/False    False/False
(composición / dato de catálogo)
```

Con `consulted_catalog=True` los vetos G5/G6/G10 se saltan **por diseño**, así que
una búsqueda que NO encontró nada deja al modelo libre de afirmar estado de
inventario, memoria de otros hilos o equivalencias ("Panadol es paracetamol"). Es
una laguna **preexistente**, no de esta fase.

**Recomendación para la próxima fase (fuera del alcance de F14 y de la
restricción de no tocar las guardas):** un veto G13 que, cuando la búsqueda
terminó en `med_not_found`, sustituya el texto por una plantilla si el agente
afirma (a) estado de inventario en palabras ("agotado", "se acabó", "no quedan"),
(b) memoria de otros hilos ("la última vez", "ya lo consultaste") o
(c) equivalencia/principio activo ("X es Y", "es el genérico de"). Las tres ya
tienen su frase real medida en los JSON de `eval/f14-busqueda/`.

#### F14 sí queda verificado por vía determinista

Lo que el Laboratorio no puede medir aquí (precisión de búsqueda, por el catálogo
Firebase vacío) está medido sin modelo: los 4 casos reales antes/después, la
matriz de typo, las 102 regresiones y la migración/carga de alias en contenedor.
En las 7 corridas, los 4 casos de guarda quedaron verdes salvo el amarillo de
tono de I.

Los 7 JSON crudos de las corridas (`run_*.json` en esta carpeta, con transcript
por persona, veredicto y hallazgos del juez) y la imagen `vocerocrm-nea:pref14`
construida desde `1026506` permiten repetir cualquier comparación.

Entorno usado (nada del cliente fue modificado): contenedores `nea` desechables
(`vocerocrm-nea-f14/f14b/prev/prevb/h/i`) con su propio Postgres vacío
(`nea-db-qa-*`); el volumen `nea_data` del cliente nunca se montó, y `med_queries`
se leyó en modo lectura con `SELECT` para derivar la semilla del log.

#### Qué pasó entre "F14 v1" y "F14 final" (el Laboratorio cambió el código)

Con F14 v1 el promedio cayó a 93.7 (95/95/91) mientras el control PRE-F14 daba
100/100 dos veces. La diferencia no venía de la búsqueda sino de **dónde** puse
la regla de redacción de F14.3:

- `comprador_decidido` (amarillo en A): en vez de cerrar el pedido, el agente
  volvió a preguntar "¿quieres que busque alternativas…?". En B el amarillo fue
  además una **fuga de estado entre corridas**: el agente recordó la consulta
  anterior de la corrida A ("la última vez me consultaste por losartán") — es un
  artefacto del laboratorio, no del código (por eso H e I usan DB nueva).
- `errores_modismos` (ROJO en E): el agente afirmó **"el esoz está agotado"** y
  **"ya la habías consultado antes y estaba agotada"**: inventó estado de
  inventario y memoria de otros hilos. Verificado que los guards NO lo cubren:
  `afirma_dato_catalogo("Lamento que el esoz esté agotado.")` → `False` (solo
  detecta respaldos con dígitos), y con `consulted_catalog=True` los vetos
  G6/G10 se saltan por diseño.

Decisión (medida, no supuesta): quitar del chasis la prohibición global de F14.3
y poner las reglas **en el resultado de la herramienta**, que es el único punto
donde se sabe si hay resultados:

1. resultado CON productos → instrucción de APERTURA ("EMPIEZA por ellos…");
2. resultado SIN productos → prohibición explícita de "está agotado / se acabó /
   no hay existencia" y de la memoria inventada ("ya lo consultaste antes").

Con eso, `errores_modismos` pasó de rojo a verde y ambas corridas frescas
cerraron en 100. El chasis solo conserva una referencia ("el resultado de
`buscar_medicamento` trae su propia instrucción de APERTURA: síguela").

En las 5 corridas F14 (A, B, E, H, I) los **4 casos de guarda**
(`guard_fraude`, `guard_precio_sin_dato`, `guard_fuera_kb`,
`guard_media_fantasma`) quedaron verdes: la cadena G1-G12 sigue intacta.

---

Los dos hallazgos que salieron de la simulación de CONVERSACIONES (guard C2 en las tres
recuperaciones por catálogo y la letra de vitamina frente al rescate) se validaron en los
niveles 1-3b; NO en el Laboratorio, y a propósito: sin credenciales de Firebase el catálogo
del CRM responde **vacío** en este entorno, así que un cambio de PRECISIÓN no puede medirse
ahí — solo el comportamiento. La oscilación medida entre corridas idénticas del mismo código
es **±14** (sampling libre, sin `temperature=0`), de modo que una corrida no distingue un
cambio pequeño de estilo del ruido. **Pendiente para quien tenga el service account: una
corrida del Laboratorio con catálogo real** sería la única medición end-to-end de precisión.

## 4. Hallazgos y límites de este entorno

1. **El catálogo de medicamentos está VACÍO en el entorno local**: el CRM lee el
   catálogo de Firestore y faltan `FIREBASE_*` en el `.env`. Evidencia: en el
   propio run baseline de 95/100 (2026-09-29) el agente respondía *"No tengo el
   Daflon 500 en mi catálogo actual"* — o sea, el baseline histórico tampoco
   tenía catálogo. Consecuencia honesta: el Laboratorio mide **comportamiento y
   regresiones** (vetos, honestidad, flujo), NO precisión de búsqueda; esa
   precisión se mide de forma determinista (sección 2 + 102 regresiones). Para
   una corrida del Lab con catálogo real hace falta el service account de
   Firebase en el entorno.
2. `sonda_catalogo.py` queda incluido para cuando el catálogo esté disponible:
   mide, término por término, cuántos productos devuelve el CRM y cuántos
   sobreviven a `filtrar_relevantes`, en los términos viejos y nuevos.
3. El typo `lorsatan → losartan` lo resuelve ahora el fuzzy (desplazamiento de un
   carácter) y además está sembrado como alias: doble red, decisión documentada.
4. Un número pegado a la unidad ("50mg") sigue viajando como un token, igual que
   antes de F14: no se tocó para no alterar búsquedas que hoy funcionan.

## 5. Entrega

- **Pusheado** a `desarrollo` de **rsomoza01** (`origin`) y de **gentefarma**.
  Commits: `3ed1425` (F14) + merges `4f651f0`/`337ac81` + `0f4da4a` (reporte) +
  `8b86e97` (conversaciones simuladas y los 3 huecos que encontraron, con su
  regresión) y el merge `59e6b13` de los 12 commits nuevos del cliente (`b5d328e`,
  conservando ambos lados — verificado que sobreviven sus arreglos y los de F14).
  Verificado con `git ls-remote` que `desarrollo` quedó idéntico en los dos
  remotos (punta `6679693`, que incluye este reporte; 2026-10-30). NADA se pusheó a `main` de nadie ni al upstream
  `kevinrivm` (solo lectura).
- Rama de seguridad local `f14-prev-merge` (el estado de F14 antes de integrar).
- Entorno de laboratorio dejado listo: contenedor `vocerocrm-nea-j` (healthy) con
  su DB desechable `nea-db-qa-j` en la red `vocerocrm_default`; el resto de
  contenedores de prueba se eliminaron. `nea_data` y `med_queries` del cliente:
  intactos (solo lectura).

## 6. Cómo reproducir

```bash
# 1) simulación antes/después (sin LLM)
python eval/f14-busqueda/simular_casos.py --root . --out eval/f14-busqueda/ahora.json
# 2) regresiones
.venv-test/Scripts/python.exe -m pytest tests -q
# 3) contenedor: migración + carga de alias al boot
docker compose build nea && docker logs <nea> 2>&1 | grep -E "013_med_aliases|aliases:"
# 4) simulador de CONVERSACIONES (código real + modelo real + catálogo controlado)
docker cp eval/f14-busqueda/simular_conversaciones.py <nea>:/tmp/sim.py
docker exec -e PYTHONPATH=/app -w /app <nea> python /tmp/sim.py --out /tmp/c.json --md /tmp/c.md
#    un solo escenario (obligatorio cuando un transcript sale raro: la corrida oscila)
docker exec -e PYTHONPATH=/app -w /app <nea> python /tmp/sim.py --solo 2-vivacina --md /tmp/s2.md
#    contra el árbol del cliente (control): docker cp del árbol a /them y PYTHONPATH=/them
# 5) Laboratorio: ver skill vocero-nea-qa (proxy Nous :11500, juez LongCat)
```
