# Spec F15.1 — Autorización de búsqueda POR TÉRMINO (cierre del PENDIENTE #1 de F15)

Repo: `D:\Proyectos\GenteFarma\agentNEA` · rama de trabajo/entrega **`desarrollo`** · base `be8d8ce`.
Cambios **aditivos**; `app/guards.py` y la cadena de vetos de `turn.py`, intactos.

Es el hueco descrito en §4 del `PROMPT-CONTINUIDAD-F15.md`: *una vez que la capa autoriza el turno,
el MODELO puede lanzar sus propias búsquedas con términos genéricos o ruido, y esos resultados sí
llegan al cliente*. Reproducido en el Laboratorio con `vivacina` → listados de **VITAMINA C/K/E**
(la categoría que F14 cerró en la *extracción* del término, no en las búsquedas que el modelo decide).

---

## 1. El hueco, medido (no supuesto)

Sonda determinista, sin modelo y sin catálogo real:
`eval/f15-frases-conversacion/probe_autorizacion_por_termino.py`
(host: `PYTHONPATH=<repo> .venv-test/Scripts/python.exe eval/f15-frases-conversacion/probe_autorizacion_por_termino.py`).

Salida sobre el árbol actual (`be8d8ce`):

```
== A) turno autorizado por el BACKSTOP (termino='vivacina') ==
   modelo busca 'vivacina': PASA
   modelo busca 'vitamina': PASA   <-- HUECO
   modelo busca 'acabo':    PASA   <-- HUECO
   modelo busca 'ustedes':  PASA   <-- HUECO
== B) turno autorizado por la CAPA (termino='losartan') ==
   modelo busca 'losartan 50': PASA
   modelo busca 'vitamina':    PASA  <-- HUECO
   modelo busca 'acabo':       PASA  <-- HUECO
== C) otra puerta al catálogo: sugerir_generico ==
   sugerir_generico('vitamina') sin autorización → consultó el catálogo ['vitamina']  <-- HUECO
HUECOS DETECTADOS: 6
```

### 1.1 Hueco 1 — la autorización es del TURNO, no del término

`app/tools.py`, `_autorizar_busqueda` (líneas 2670-2678 en `be8d8ce`):

```python
if self.intent_fuente and self.intent_fuente != "llm":
    return None                      # el backstop ya autorizó → autoriza TODO el turno
if self.intent_autorizado is not None:
    return None if self.intent_autorizado else self._rechazo_comprension(nombre)
```

Autorizado `vivacina`, cualquier segundo término del mismo turno (`vitamina`, `acabo`, `ustedes`)
pasa por la puerta sin volver a mirar el mensaje del cliente. La puerta existe (cierra el caso
`intent_fuente` vacío, que era el agujero de F15) pero su granularidad es la equivocada.

### 1.2 Hueco 2 (nuevo, hallado al escribir este spec) — una segunda puerta sin candado

`app/tools.py`, `_sugerir_generico` (línea 3920): consulta el catálogo con
`self._ctx.crm.get_products(..., q=nombre, limit=8)` **sin llamar a `_autorizar_busqueda`**. El
modelo puede pedir genéricos de cualquier término que invente y el resultado llega al cliente.
La autorización central de F15 solo cubre `buscar_medicamento`.

---

## 2. Decisión de diseño

**Se adopta la opción 2 del pendiente** (autorización por término) **con el atajo de evidencia
farmacéutica de la opción 1 limitado a la PRIMERA búsqueda del turno**:

| Regla | Qué hace |
|---|---|
| Cada término distinto se autoriza individualmente | La capa vuelve a decidir, con el mensaje del cliente y el término candidato en la mano |
| El mismo término se reutiliza | Normalizado (mayúsculas/tildes/espacios): **0 llamadas extra** a la capa |
| Evidencia fuerte (dosis o forma farmacéutica) | Atajo **solo en la primera búsqueda del turno**; después, todo término nuevo pasa por la capa |
| Sin autorización | No se busca: se devuelve el rechazo de comprensión (el modelo pide aclaración) |

Por qué no la opción 1 sola (exigir evidencia farmacéutica a toda búsqueda): **es un heurístico de
forma**, exactamente la clase de regla que F15 vino a retirar, y castigaría consultas legítimas que
el cliente escribe sin dosis ni forma (`tienen vivacina?`, `busco algo para el dolor de cabeza`).
Por qué no autorizar por turno y filtrar después: el daño de F14 no fue solo la respuesta — los
términos basura entran a `med_queries` y contaminan alias y corrección de typos. Hay que cortar
**antes** de la consulta.

**Sin léxico nuevo.** La directiva del usuario sigue vigente: nada de listas de palabras ni reglas
por frase. El veredicto lo da la capa con contexto; lo único que cambia es *cuándo* se le pregunta
(por término, no por turno).

**Riesgo simétrico que hay que proteger:** una conversación legítima con **dos medicamentos**
(`necesito losartan y metformina`) o una corrección (`no, era metformina`) debe seguir buscando
ambos. La regla no es «una búsqueda por turno»: es «un término aprobado por el mensaje del cliente,
cada vez».

---

## 3. Reglas de implementación

- **R1** — `_autorizar_busqueda(nombre)` mantiene la firma (`None` = autorizada, dict = rechazo) y su
  lugar único como puerta. El estado por turno se guarda como conjunto de términos ya autorizados
  (normalizados); `intent_autorizado` se conserva por compatibilidad/auditoría.
- **R2** — Un término nuevo **siempre** pasa por la capa, aunque el turno ya tenga una búsqueda
  aprobada. El atajo `_tiene_evidencia_de_producto` solo aplica cuando el turno aún no buscó.
- **R3** — Mismo término (normalizado) ⇒ reutiliza la autorización, sin llamada. Y el
  **mismo producto con otro formato** (`losartan 50 mg` tras aprobar `losartan 50`) también:
  el modelo añadió la dosis, no cambió de medicamento. Un rechazo **conclusivo** del turno se
  cachea: insistir con el mismo término no vuelve a pagar llamada ni duplica la fila de auditoría.
- **R4** — `_sugerir_generico` pasa por la misma puerta, con el término que recibe del modelo.
  Si no está autorizado, devuelve el rechazo (no consulta el catálogo).
- **R5** — Las **derivaciones internas** de una búsqueda ya autorizada NO vuelven a pedir permiso:
  alias→canónico, limpieza de saludos/muletillas, cascada de núcleo/typo, principio activo
  (`_buscar_por_principio_activo`) y las recuperaciones por catálogo. Solo se autoriza el término
  que el MODELO pasa a la herramienta. (En F14 el bug se escapaba por tres puertas: cerrar una no
  cierra el bug — aquí las recuperaciones son legítimas y no deben romperse.)
- **R6** — **Fail-closed intacto**: `_rechazar_por_fallo_comprension` se conserva y sigue cubriendo
  capa no disponible, excepción de la capa y timeout. Un término nuevo que no pueda evaluarse
  **no se busca**.
- **R7** — **Auditoría**: toda decisión sigue registrándose en `intent_decisions` (migración 015 ya
  trae `fuente/acto/hay_consulta/termino/candidato/motivo/confianza`) — incluidos los términos
  **rechazados** con `candidato=<término del modelo>` y `fuente='llm'`. **No hace falta migración
  nueva.** Sin este asiento no se puede contar cuánto ruido intentó el modelo.
- **R8** — La capa recibe el mensaje del cliente y el término candidato (contrato actual). Si al
  medir aparece que aprueba términos que el cliente no mencionó, el ajuste va en el **prompt de la
  capa** (`app/intencion.py`), nunca en `app/prompt.py` ni en `guards.py`.
- **R9** *(añadida al medir, ver REPORT-F15.1 §2.b)* — La puerta pregunta por **el candidato**, no
  por el mensaje: `entender(..., candidato_obligatorio=True)` añade al contexto una pregunta estricta
  («el veredicto es sobre ESTE término»). Sin ella la capa aprobaba «el mensaje trae una consulta»
  (la del cliente) y ese «sí» autorizaba el término que el modelo eligió: en el Laboratorio entraron
  al catálogo `oiga`, `firma`, `medica` y `maria fernandez cmp 45231`.
- **R10** *(añadida al medir)* — El atajo por evidencia **no** se dispara con cualquier número: una
  cifra suelta solo cuenta como dosis si el término tiene ≤3 tokens y la cifra 2-4 dígitos. Antes,
  «maria fernandez cmp 45231» (nombre + colegiatura, el texto que el OCR de una receta pone delante
  del modelo) se autorizaba sin pasar por la capa.

---

## 4. Validación (3 niveles + Laboratorio)

1. **Determinista (sonda)**: `probe_autorizacion_por_termino.py` → **0 huecos** (A, B y C).
2. **Tests** (`.venv-test/Scripts/python.exe -m pytest tests/ -q`):
   - mismo término reutilizado ⇒ **0 llamadas** a la capa;
   - término distinto ⇒ la capa decide (se le pasa el candidato y el mensaje);
   - rechazo ⇒ la herramienta devuelve `no_autorizado_por_comprension` y **no** consulta el catálogo;
   - `sugerir_generico` sin autorización ⇒ 0 consultas al catálogo;
   - los dos tests de fail-closed existentes siguen verdes;
   - suite completa ≥ 160 verdes, `py_compile app/*.py` OK.
3. **Corpus de 50 frases** (`medir_corpus.py`, `deepseek/deepseek-v4.1-flash`): **50/50**, 0 falsos
   positivos, 0 falsos negativos (no regresión; baseline de hoy: 50/50 en 115 s).
4. **Camino REAL (`POST /chat`)**, no el simulador (el simulador corre en proceso: no prueba la
   autorización ni deja filas): turno de un solo término donde el modelo lanza un segundo término
   genérico ⇒ `med_queries` **sin** el término basura y fila en `intent_decisions` con el rechazo.
   Repetir el escenario `vivacina` del informe F15 y comprobar que **no** se listan VITAMINA C/K/E.
5. **Laboratorio A/B** (solo modelos gratis/baratos: agente `deepseek/deepseek-v4.1-flash`, juez
   `meituan/longcat-2.0`; estado fresco = DB desechable por corrida): delta dentro de la banda
   (±14) y **ningún rojo nuevo atribuible**; si hay rojos, se atribuyen antes de tocar código.

Criterio de PASS: 1-4 en verde **y** en el Laboratorio el caso `vivacina` deja de listar categorías
distintas sin que aparezcan rojos nuevos.

---

## 5. Riesgos y contención

| Riesgo | Contención |
|---|---|
| Falso negativo: consulta legítima sin dosis/forma tratada como charla | La capa decide con el mensaje completo; caso de prueba obligatorio: `tienen vivacina?`, `busco algo para el dolor de cabeza` |
| Multi-medicamento en un turno (`losartan y metformina`) | Caso de prueba explícito: ambos términos se autorizan; el fallo cae del lado de preguntar, no de inventar |
| Coste: una llamada extra por término nuevo | El tope de rondas de herramientas acota las búsquedas por turno; el modelo del chat no lleva `temperature=0` pero la capa sí decide determinista-por-prompt |
| Romper las recuperaciones por catálogo (F14) | R5: solo el término del modelo entra por la puerta; el resto del camino queda igual |
| Latencia del proveedor de la capa | Circuito de corte ya existente (3 fallos) y `INTENT_LAYER=false` devuelve la conducta anterior |

---

## 6. Anexo — estado de la rama del codificador (verificada, NO mergeable tal cual)

`gentefarma/fix/f15-term-scoped-search` (`9bab279`, 4 commits sobre `be8d8ce`) implementa la opción 2
y va en la dirección de este spec, pero **regresiona F15**:

- borra `_rechazar_por_fallo_comprension` y **sigue llamándolo** en los dos `except` de
  `_autorizar_busqueda` ⇒ `AttributeError` en vez de fail-closed;
- medida en worktree con el venv del repo: **2 fallidos, 156 pasados**
  (`test_import_error_deniega_busqueda`, `test_error_inesperado_deniega_busqueda`);
- no cubre la segunda puerta (`sugerir_generico`, hueco 2 de §1.2);
- no trae el spec en `docs/`.

Tampoco es fast-forward a `desarrollo` (su base es `be8d8ce`, sin el commit de docs). Lo recuperable
es su idea y sus tests (autorización por término + registro del rechazo); la implementación necesita
el helper restaurado, la puerta en `sugerir_generico` y la validación de §4.
