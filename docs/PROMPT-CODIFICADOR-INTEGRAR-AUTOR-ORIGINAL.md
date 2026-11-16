# Prompt para el agente codificador — integrar (selectivamente) los avances del autor original

> **SUPERSEDIDO el 2026-11-09.** Vigente: **`docs/ORDEN-CODIFICADOR-CURADURIA-UPSTREAM-2026-11-09.md`**
> (base `8813f12`, 252 tests). Se conserva solo como registro histórico: su base (`cee0092`, 174 tests)
> está obsoleta y ya se integró lo que este documento pedía. **No lo sigas.**

> Pegar esto como primer mensaje de la sesión del codificador. Es autosuficiente: rutas, estado,
> reglas, método de evaluación, validación y entregables.

---

Trabajas en el agente NEA (Python): `D:\Proyectos\GenteFarma\agentNEA`, rama **`agent-qa`**
(HEAD `cee0092`). Antes de tocar nada, carga la skill **`vocero-nea-qa`**: tiene el arranque del
entorno, las trampas ya medidas y las recetas de validación.

## 1. Objetivo de esta tarea

**Evaluar los avances del AUTOR ORIGINAL del proyecto** (`upstream` = `kevinrivm/nea-agent`, hoy en
`upstream/main` = `ad89138`, 5-nov-2026) y **seleccionar e integrar** al repositorio de QA
(`agent-qa` del espejo `arquimedesgarcia/genteFarma-nea`) **solo lo que aporte valor**, sin romper
nada de lo que ya tenemos.

**No** es un merge del repo completo: el `main` del autor **no tiene el flujo de farmacia** tal como
lo usa este despliegue, y su árbol está refactorizado. Un merge tal cual borra el agente que está en
producción. La tarea es de **curaduría**: elegir, adaptar y verificar cambio por cambio.

## 2. Repos y remotos (reglas duras)

| Remoto | Qué es | Permiso |
|---|---|---|
| `origin` | `rsomoza01/nea-agent` (el cliente que usa el agente) | push **solo** a `desarrollo` si la tarea lo pide |
| `gentefarma` | `arquimedesgarcia/genteFarma-nea` (espejo del usuario) | push a `agent-qa` y `desarrollo` |
| `upstream` | `kevinrivm/nea-agent` (**autor original**) | **SOLO LECTURA**: nunca push, nunca tags, nunca branches |

- **Nunca** `--force` / `push -f` a ningún remoto. Si divergen, para y pregunta.
- Nada de `git push origin main` ni `upstream main`.
- El clon local lo usan **otros agentes**: antes de empezar, `git status -sb` y confirma que estás en
  `agent-qa` y con el árbol limpio. Si necesitas otro árbol, usa `git worktree add` (no cambies la rama
  del repo principal a medias).
- Cambios **aditivos**. `app/guards.py` y la cadena de vetos de `app/turn.py` solo se tocan si el
  cambio del autor lo justifica y lo explicas; **G1-G12 no se pueden romper**.

## 3. Qué NO se puede romper (comprobado, no negociable)

1. **F15.1 — autorización de búsqueda por término y por candidato** (`_autorizar_busqueda`):
   estado `intent_terminos_autorizados` / `intent_rechazos`, helpers `_clave_termino` y
   `_mismo_producto`, la puerta en `buscar_medicamento` **y** en `sugerir_generico`,
   `candidato_obligatorio=True` hacia la capa y el fail-closed (`_rechazar_por_fallo_comprension`
   intacto). Sonda de control: `eval/f15-frases-conversacion/probe_autorizacion_por_termino.py`
   → **0 huecos**.
2. **F15 — capa de comprensión** (`app/intencion.py`): contrato de tres estados, confianza mínima,
   camino rápido solo para nombre pelado, fail-safe ante timeout/JSON inválido, y la regla de que un
   rechazo **no** puede venir con `acto=consulta_medicamento`.
3. **F14 — precisión de búsqueda**: relevancia por tokens/núcleo y alias (`med_aliases`). Ojo: las
   recuperaciones por catálogo son **tres puertas** distintas; cerrar una no cierra el bug.
4. **Los fixes del cliente de nov-2026** (ver §4): forma por línea, cache de búsquedas del turno con
   clave normalizada, familia pedida, G6 con la lista mostrada, alias curado para el principio activo,
   turno en vuelo, etc.
5. **Guardas G1-G12** y la cadena de vetos.
6. Base actual de verificación: **`pytest tests/` = 174 verdes**, `py_compile app/*.py` OK,
   corpus de 50 frases = **50/50 (0 falsos positivos, 0 falsos negativos)**.

## 4. Lo que ya está integrado (para no duplicar)

- Nuestro trabajo: F14, F15, F15.1 (comprensión + puerta por término/candidato + fail-closed).
- Del cliente (`rsomoza01`, 6-7 nov, ya mergeado en `agent-qa`): `7c7f7cd` (no se afirma que una marca
  sea principio activo; la forma no es un producto), `97313c5` (sin alias curado no se adivina el
  principio activo), `4480f5d` (G6 no veta si los importes son de la lista ya mostrada), `d194278`
  y `ae14d92` (no se contesta con productos de otra familia), `4d18206` (el turno no muere en silencio
  cuando la forma descarta todo), `fab9b8a` (mensaje que llega con el turno en vuelo), `89c51da`
  (firma de `_formatear_receta`), `4c14e9a` (el cache conserva el aviso de forma), `84ea926`
  (`_clave_busqueda` normalizada), `490734e` (cada línea del cliente resuelve la forma de UN
  medicamento). Traen 3 archivos de tests nuevos.
- Un intento viejo de fusión con el autor: rama local `merge-develop` (`42f0be2`, 30-sep) — **solo
  como referencia** de qué se intentó; no la mergees tal cual.

## 5. Método (obligatorio, en este orden)

1. **Inventario del autor**: `git fetch upstream --prune`; lista sus ramas y su `main`; identifica
   **qué hay nuevo desde nuestro merge-base** y qué archivos toca cada cosa.
   Ordena los candidatos por valor real (fallo que arregla / función nueva) y por riesgo.
2. **Filtrar lo que ya tenemos**: para cada candidato, comprueba si ese comportamiento **ya existe**
   en `agent-qa` (grep por comportamiento, no por nombre; el autor renombra). Un cambio ya cubierto
   se descarta **con la prueba** de que está cubierto (archivo:línea o test que lo demuestra).
3. **Tabla de decisión** (esto es el entregable central), una fila por candidato:

   | Commit/rama del autor | Qué hace | ¿Ya lo tenemos? | Decisión | Riesgo | Evidencia de la decisión |
   |---|---|---|---|---|---|
   | ... | ... | sí/no/parcial | integrar / adaptar / descartar | bajo/medio/alto | archivo:línea, test, sonda |

4. **Integrar de a UNO** (cherry-pick si aplica limpio; si no, portar a mano el comportamiento, nunca
   copiar el archivo entero). Tras **cada** integración: compilar, tests y las sondas de §3.
5. **Validación por niveles** (los cuatro):
   - `py_compile app/*.py`;
   - `.venv-test/Scripts/python.exe -m pytest tests/ -q` (≥174, sin rojos);
   - sondas deterministas: `eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` (**0
     huecos**) y las sondas de F14 que correspondan;
   - corpus de 50 frases con el modelo real (`medir_corpus.py`) = 50/50, 0 FP, 0 FN.
   Si el cambio es conversacional, además el simulador de conversaciones (transcripciones) y, si
   aplica, una corrida del Laboratorio con la DB desechable fresca.
6. **Cierre**: deja el resultado en `agent-qa` **solo si todo está verde**; si algo queda a medias,
   déjalo en una rama `fix/integracion-autor-<tema>` desde `agent-qa` y dilo.

## 6. Reglas de trabajo (del usuario)

- **Modelos**: nada de modelos caros. Corridas del Laboratorio con agente `deepseek/deepseek-v4.1-flash`
  y juez `meituan/longcat-2.0` (gratis). Para cualquier tarea cara, propón modelo y espera confirmación.
- **Nada de Ollama local**. Modelos Nous por el proxy local `:11500` (si está caído:
  `cd /d/Proyectos/GenteFarma/voceroCRM && python scripts/llm-proxy.py 11500`; comprueba con un POST
  corto **antes** de medir, porque sin proxy el corpus y las sondas dan fallos falsos por fail-closed).
- **Nunca** tocar la DB ni el volumen del cliente: el estado fresco se consigue con una DB desechable
  por corrida (contenedor postgres propio).
- Entregables en `docs/` o `eval/`, **commiteados** (al usuario le molestan los untracked).
- Push **solo** a `agent-qa` / `desarrollo` del espejo (y a `desarrollo` de `origin` si se pide).
- Un solo flag por vez: si aparece que dos ramas del autor se pisan entre sí, intégralas en orden y
  verifica después de cada una.
- **Nada se borra**: si descartas un cambio del autor, queda documentado en la tabla; sus ramas no se
  tocan.

## 7. Entregables

1. `docs/INTEGRACION-AUTOR-ORIGINAL.md` — evaluación completa: inventario, tabla de decisión,
   justificación de cada descarte y de cada adaptación.
2. La integración (commits en `agent-qa` o en la rama de trabajo) con **la evidencia pegada**:
   salida de `pytest` (antes/después), sonda de la puerta, corpus.
3. Un resumen final de 10 líneas: qué se integró, qué se descartó y por qué, qué quedó pendiente y con
   qué riesgo.

## 8. Trampas ya medidas (no las repitas)

- **Al 9-nov-2026 la máquina venía de un reinicio**: el proxy `:11500` estaba caído (se relanzó) y **el
  daemon de Docker Desktop no estaba corriendo** → arráncalo antes de cualquier corrida del
  Laboratorio; y ojo: `docker compose up -d` puede no levantar nada en silencio, así que revisa
  `docker ps` y usa `docker start <contenedor>` + health si hace falta.
- Sin proxy `:11500` arriba, el corpus devuelve ~2/50 y las sondas fallan: **no es una regresión**.
- El árbol del autor **no** tiene el flujo de farmacia: comparar archivo a archivo, no por nombre de
  función.
- `tools.py` es enorme y lo tocan los tres lados (autor, cliente y nosotros): un merge automático sin
  conflictos **no** prueba que todo sobrevivió — hay que comprobar los marcadores de cada lado y
  correr las sondas.
- El simulador de conversaciones corre en proceso: sirve para comportamiento, **no** prueba la
  autorización ni deja filas en la BD; para eso, el endpoint `/chat`.
- El Laboratorio oscila ±14 entre corridas idénticas: un delta solo se atribuye con corrida pareada
  (`eval/lab-ab-f151.sh` y `eval/comparar-ab-f151.py` son el ejemplo ya hecho).
