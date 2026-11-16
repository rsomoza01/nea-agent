# ORDEN AL CODIFICADOR — curaduría de los avances del AUTOR ORIGINAL (2.ª pasada)

> **Pegar este documento completo como primer mensaje de la sesión del codificador.** Es autosuficiente.
> **Reemplaza** a `docs/PROMPT-CODIFICADOR-INTEGRAR-AUTOR-ORIGINAL.md` (marcado como SUPERSEDIDO): esa
> versión describe una base vieja (`cee0092`, 174 tests) y no debe seguirse.

**Fecha:** 2026-11-09 · **Repo:** `D:\Proyectos\GenteFarma\agentNEA`
**Base de trabajo:** `agent-qa` = `desarrollo` = **`8813f12`** (ambas ramas y los dos remotos, verificado)
**Entrega:** **una rama por candidato**, con su validación; el merge lo hace el responsable.

---

## 1. Objetivo

Determinar si el **autor original** (`upstream` = `kevinrivm/nea-agent`) tiene **avances nuevos que aporten
valor real** al agente de farmacia que mantenemos, e **integrar solo esos**, de a uno, sin romper nada de lo
que hoy funciona (252 tests verdes, F14/F15/F15.1 cerradas, G1–G13 activos).

**Esto NO es un merge del repositorio del autor.** Su `main` no tiene el flujo de farmacia ni nuestro
`app/turn.py`; un merge borra el agente. Esto es **curaduría**: comparar, decidir, adaptar, verificar.

**Criterio de valor (el único que cuenta):** ¿mejora el comportamiento de *nuestro* agente adaptado a
farmacias (precisión de búsqueda, no inventar datos, autorización de búsquedas, robustez del turno, latencia,
seguridad de dependencias)? Si el candidato es de **agenda, CRM, multi-organización o despliegue del autor**,
**no aporta aquí**: se descarta con su razón.

## 2. Estado de partida (verifícalo, no lo asumas)

```bash
cd D:/Proyectos/GenteFarma/agentNEA
git status -sb                      # limpio, en agent-qa
git rev-parse HEAD                  # 8813f12
git fetch gentefarma origin upstream --prune    # ¡uno por remoto si falla!
```

| Remoto | Qué es | Permiso |
|---|---|---|
| `upstream` | `kevinrivm/nea-agent` (autor original) | **SOLO LECTURA**: nunca push/tags/branches |
| `gentefarma` | `arquimedesgarcia/genteFarma-nea` (espejo) | push a **tu** rama; `agent-qa`/`desarrollo` solo lo toca el responsable |
| `origin` | `rsomoza01/nea-agent` (cliente) | **prohibido** en esta tarea |

Base de verificación actual (debe seguir así al terminar):

- `py_compile app/*.py` OK · **`pytest tests/ -q` = 252 passed** (sin fallos ni omitidos)
- `eval/qa-2026-11-06/sonda_bucle.py` → A1, A2, A3, A4, A7 y C0 **responden** (1 respuesta cada uno)
- `eval/qa-2026-11-06/sonda_g5.py` → B1, B2, B4, B5, B7-5, B7-7 **bloqueados**; B8-2 y B8-3 **pasan**; B9 pasa
- `eval/g13-afirmacion-sin-accion/detector_g13.py` → 12 textos, **0 coincidencias**
- `eval/f15-frases-conversacion/probe_autorizacion_por_termino.py` → **10 casos, 0 huecos**
  (con `--model deepseek/deepseek-v4.1-flash` → **17 casos, 0 huecos**)
- `eval/f14-busqueda/simular_casos.py` → salida **idéntica byte a byte** al árbol base `0f09e0a3`
- corpus `eval/f15-frases-conversacion/medir_corpus.py` → 49/50, **0 falsos positivos** (se registra por corrida)

## 3. Qué NO se puede romper (marcadores: compruébalos con `grep` antes y después)

1. **F15.1 — autorización de búsqueda por término y por candidato**: `_autorizar_busqueda`,
   `intent_terminos_autorizados`, `intent_rechazos`, `_clave_termino`, `_mismo_producto`,
   `candidato_obligatorio`, `_rechazar_por_fallo_comprension`; la puerta en `buscar_medicamento` **y** en
   `sugerir_generico`. Sonda: 0 huecos.
2. **F15 — capa de comprensión** (`app/intencion.py`): contrato de tres estados, confianza mínima, camino
   rápido solo para nombre pelado, fail-safe ante timeout/JSON inválido, rechazo nunca con
   `acto=consulta_medicamento`.
3. **F14 — precisión de búsqueda**: relevancia por tokens/núcleo, alias (`med_aliases`), las **tres puertas**
   de recuperación por catálogo.
4. **P1 — ningún turno termina en silencio**: `productos_turno`, `_acumular_productos_turno`,
   `_fallback_farmacia`, el corte por bucle degenerado y `MAX_TOOL_ROUNDS` (no lo bajes para «arreglar» nada).
5. **P2 — G5 con evidencia por producto**: `guards.afirma_composicion`, `guards.hay_evidencia_composicion`,
   el veto de G5 en `app/turn.py` (evidencia por *afirmación, producto*; fail-closed si el payload no expone
   composición).
6. **P3 — G13 (acción que no ocurrió)**: `guards.G13_PEDIDO_REGISTRADO`, `G13_AGREGADO_AL_PEDIDO`,
   `G13_APARTADO_O_AGENDADO`, `G13_NEGACION`, `TPL_G13_ACCION`; el `elif` de G13 en `app/turn.py` y las
   señales de estado en `app/tools.py`. **Exige señal de estado**: la frase sola no puede disparar.
7. **Comprensión antes de buscar**: `_termino_inferido_si_mejora` y la regla de meta-frases
   (`\b(principio|ingrediente)\s+activo\b` no se fuerza al catálogo).
8. **G1–G12** y la cadena de vetos: no se debilita **ninguno**.
9. **Hints LLM** (`LLM_REASONING_EFFORT` / `LLM_PROVIDER_SORT`): siguen **vacíos**; no los enciendas.

## 4. Qué ya está integrado del autor (para no duplicar)

| Del autor | Estado aquí |
|---|---|
| `4f110da` — `reasoning.effort` / `provider.sort` por `extra_body` + reintento sin hints | **Adaptado** (opt-in, apagado por defecto) en `app/llm.py`, `app/config.py`, `app/main.py`, con 4 tests |
| `4a473c7` / `83f2d3e` — `pypdf 6.21.4` | **Integrado** en `requirements.txt` (+ test de regresión de PDF) |
| `fb8bdf5` — modo ensayo Vocero Cloud | **Descartado**: depende de `crm_brains`, `multiorg`, firma de Vocero Cloud |
| `5ffc420` / `8186fb8` / `Dockerfile.deploy` — CI y despliegue del autor | **Descartado**: específico de su entorno |
| `app/multiorg.py`, `app/ensayo.py` | **No existen aquí** (farmacia mono-`provider_id`) |

## 5. Primer paso obligatorio: ¿hay algo nuevo del autor? (coste 0, sin modelo)

```bash
git fetch upstream --prune
git ls-remote upstream refs/heads/main          # ¿sigue en ad89138?
git rev-list --count ad891389cc5402e35a9b2a3b4dcd15fda0a846d5..upstream/main

# ramas del autor con trabajo que NO está en su main (las únicas candidatas reales):
for r in $(git for-each-ref --format='%(refname:short)' refs/remotes/upstream/ | grep -v 'upstream/HEAD'); do
  git merge-base --is-ancestor "$r" upstream/main 2>/dev/null || \
    echo "UNICA $r (+$(git rev-list --count upstream/main.."$r") commits)"
done
```

**Medición de referencia hecha el 2026-11-09** (si cambió, manda lo que veas tú, no esta tabla):

| Rama del autor | Commit | Toca | Lectura inicial |
|---|---|---|---|
| `fix/032-seguimiento-permitidos` | `a1494ae` | `config.py`, `db.py`, `followup.py`, `sender.py`, `state.py`, `turn.py` | que el seguimiento y los reintentos respeten `ALLOWED_WA_IDS` — **es WhatsApp/envíos, no farmacia**: evalúa si el defecto existe en *nuestra* instancia (¿tenemos `followup.py`? ¿`ALLOWED_WA_IDS` en producción está vacía?) y decide con evidencia |
| `fix/conflicto-409-anidado` | `428a62b` | `crm.py`, `tools.py` | el 409 del CRM viene anidado y `slot_taken` nunca se activaba — **agenda**, mira si nuestro `crm.py` tiene ese patrón |
| `fix/zoom-reminder-confirmation` | `6a91f08` | `crm_brains.py`, `tools.py`, `turn.py` | depende de `app/crm_brains.py`, **que no existe aquí** → descarte razonado esperado |

`upstream/main` = `ad89138` **sin commits nuevos** a esa fecha: si sigue igual y no aparecen ramas nuevas,
la respuesta correcta puede ser **«no hay nada que integrar»** — y eso también es un entregable válido.
**No inventes trabajo para justificar la tarea.**

## 6. Método (una candidata a la vez)

Para **cada** candidata:

1. **Leer el commit**: `git show <sha> --stat` y `git show <sha>` completo. Escribe en una frase qué
   defecto corrige y **para quién** (farmacia / agenda / despliegue / multi-org).
2. **¿Ya lo tenemos?** Busca su lógica en nuestro árbol (`grep -rn`), en la historia (`git log --grep`) y en
   las fases cerradas (`docs/SPEC-F15-1-*.md`, `docs/QA-2026-11-06-*.md`, `docs/CIERRE-VERSION-2026-11-09.md`).
   Si ya está (aunque con otro nombre), **descarta** y dilo.
3. **¿Aplica a nuestro árbol?** Si el archivo/módulo no existe, descarta con la razón.
4. **Adaptar** en **una rama propia** (`fix/up-<tema>`), cambios **aditivos y quirúrgicos**, sin dependencias
   nuevas y sin tocar la puerta de búsqueda ni los vetos salvo que el candidato sea exactamente de eso.
5. **Validar** (ver §7) y comparar **antes/después**. Si algo empeora, se revierte el candidato, no se
   "ajusta" el instrumento de medición.
6. **Documentar** en la tabla de decisión y commitear la evidencia.

**Prohibido**: mezclar dos candidatas en una rama; tocar tests existentes; editar sondas de `eval/`;
cambiar `MAX_TOOL_ROUNDS`, los patrones de los vetos o la puerta de autorización para que algo "pase".

## 7. Cómo se valida (y las trampas ya medidas)

```bash
PY=D:/Proyectos/GenteFarma/agentNEA/.venv-test/Scripts/python.exe

$PY -m py_compile app/*.py
$PY -m pytest tests/ -q                                                    # 252 passed (o más, nunca menos)
$PY eval/qa-2026-11-06/sonda_bucle.py                                      # A1..A4 responden
$PY eval/qa-2026-11-06/sonda_g5.py                                         # B1/B2/B4/B5/B7 bloqueados; B8 pasan
$PY eval/g13-afirmacion-sin-accion/detector_g13.py                         # 0 falsos positivos
$PY eval/f15-frases-conversacion/probe_autorizacion_por_termino.py         # 10 casos, 0 huecos
$PY eval/f14-busqueda/simular_casos.py --root . --out <scratch>/f14.json   # comparar con el árbol base
```

Trampas que **te van a morder** si no las respetas:

- **Las sondas de `eval/qa-2026-11-06/` fijan `REPO = D:/Proyectos/GenteFarma/agentNEA`** en `sys.path`.
  Si las corres desde un worktree, **miden el árbol del repo principal**, no tu worktree. Valida en el repo
  principal o verifica con `python -c "import app.turn; print(app.turn.__file__)"` qué código se está midiendo.
- El venv **no tiene `pip`**: usa `uv pip` (`uv pip check --python <venv>/Scripts/python.exe`).
- El corpus **escribe** `eval/f15-frases-conversacion/corpus-<modelo>.json` (archivo versionado): córrelo
  desde una **copia** del árbol, nunca en el repo, o sobrescribirás un archivo del branch.
- Sin `temperature=0`, el modelo oscila: **no** declares mejora/regresión por una sola corrida.
- La sonda con modelo real necesita el proxy `http://127.0.0.1:11500/v1` vivo (compruébalo con un POST corto;
  si no responde, esa validación queda **BLOQUEADA**, no "aprobada").
- Modelos: solo **gratis o baratos** (`deepseek/deepseek-v4.1-flash` para agente/laboratorio,
  `meituan/longcat-2.0` para juez). **Nada** de Sonnet/GPT-pro para medir.

## 8. Entregables

1. **Rama(s)** en `gentefarma` con los cambios aceptados (una por candidata), **sin** mergear a
   `agent-qa`/`desarrollo`.
2. **`docs/INTEGRACION-AUTOR-<fecha>.md`** con:
   - tabla de decisión: *candidato · commit · qué hace · ¿ya lo tenemos? · decisión (integrar/adaptar/descartar)
     · riesgo · evidencia (archivo:línea, test, sonda)*;
   - comandos ejecutados y **salida literal** (antes/después) por candidata;
   - qué **no** se ejecutó y por qué;
   - discrepancias con este documento.
3. **Tests nuevos** en `tests/` para lo que integres (+ arnés propio por paquete si aplica).
4. Reporte de vuelta: rama y SHA(s), qué cierra cada cambio (`archivo:función`), resultado real de cada
   validación y el riesgo que introduce.

## 9. Definición de terminado

- `py_compile` OK y **252 tests (o más) verdes**, sin fallos ni omitidos.
- Las 6 sondas de §7 con **el mismo veredicto que en la base** (o mejor, si el candidato arregla algo medido).
- Marcadores de §3 intactos (`grep` antes/después, pegado en el reporte).
- Cada candidata decidida y documentada, **incluidas las descartadas con su razón**.
- Nada mergeado a `agent-qa`/`desarrollo`; nada pusheado a `upstream` ni a `origin`.

**Un agente que responde menos, pero no inventa y nunca deja al cliente sin una respuesta controlada, es
preferible a uno más conversacional que genera información no respaldada.** Si dudas, no integres: documenta.
