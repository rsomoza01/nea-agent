# Prompt de continuación — agente NEA (F15 cerrado, fail-closed integrado)

> Pegar esto como primer mensaje de la sesión nueva. Es autosuficiente: repo, estado, evidencia,
> pendientes, reglas y recetas.

---

Trabajamos en el agente NEA: `D:\Proyectos\GenteFarma\agentNEA` (Python), rama **`desarrollo`**,
HEAD **`be8d8ce`**. Carga la skill **`vocero-nea-qa`** ANTES de ejecutar nada: tiene el paso a paso
del Laboratorio, el arranque de contenedores y las trampas que ya costaron medidas.

## 1. Dónde vive todo

| Qué | Dónde |
|---|---|
| Agente (código que tocamos) | `D:\Proyectos\GenteFarma\agentNEA` · rama `desarrollo` |
| `origin` | https://github.com/rsomoza01/nea-agent.git (**push autorizado solo a `desarrollo`**) |
| `gentefarma` (espejo del usuario) | https://github.com/arquimedesgarcia/genteFarma-nea.git |
| `upstream` kevinrivm | **solo lectura**, nunca push (su `main` no tiene el flujo de farmacia) |
| CRM (corre el chat y el Laboratorio) | `D:\Proyectos\GenteFarma\voceroCRM` · rama `desarrollo` |
| Remotos del CRM | rsomoza01/vocero-crm.git (`origin`) y arquimedesgarcia/genteFarma-vocero.git (`gentefarma`) |

Los dos remotos del agente deben quedar sincronizados en `desarrollo`. El espejo puede quedar atrás
si el cliente pushea: `git fetch origin` → fast-forward → `git push gentefarma desarrollo`.

## 2. Estado actual (verificado, no supuesto)

- **F14** (precisión de búsqueda: apelativos en texto escrito, «opción N», typos que no cruzan de
  categoría, redacción contradictoria, multi-medicamento, tabla `med_aliases`) está en `desarrollo`.
- **F15** (capa de comprensión del acto de conversación, para que el agente **no busque antes de
  entender**) está en `desarrollo`:
  - `app/intencion.py` — clasifica el acto (saludo, dato personal, atributo, dilatoria, cortesía,
    reclamo, consulta de medicamento, otro) y decide si se busca; camino rápido determinista solo
    para nombre suelto corto; contrato JSON estricto; confianza mínima; corta-circuitos.
  - `app/tools.py` → `_autorizar_busqueda` es la **única puerta**: sin autorización de la capa no
    se busca y el modelo recibe la orden de pedir aclaratoria. **Fail-closed**: si la capa no puede
    emitir veredicto (timeout, JSON inválido, baja confianza, error, import roto) **no se busca**.
  - `migrations/014` y `015` — auditoría: deja rastro de **todas** las decisiones (incluidas las
    que NO buscaron) en `intent_decisions`, y `med_queries.intent_fuente/acto`.
- **Último cambio integrado** (rama `fix/f15-fail-closed` del codificador, ya mergeada en
  `desarrollo` como `be8d8ce`): cerrar el hueco fail-open de `_autorizar_busqueda` cuando la capa
  falla al cargar o lanza excepción. Antes devolvía `None` y la búsqueda pasaba.
- **Evidencia**: `eval/f15-frases-conversacion/REPORT.md` (4 casos reales antes/después, corridas
  del Laboratorio con `runId`, atribución de los rojos) y `eval/f14-busqueda/REPORT.md`.
- El **diagnóstico externo** (pedido al usuario) existe en
  `eval/f15-frases-conversacion/diagnostico-sonnet-codigo.md`: Sonnet 5.5/5/4.6 daban 429 y la
  cadena cayó a `openai/gpt-6-astra-pro` (veredicto 5/10). Sus 6 mejoras ya están aplicadas.

## 3. Números medidos (para no volver a medirlos de cero)

- `pytest tests/ -q` del árbol: **154 verdes**. `py_compile app/*.py`: OK.
  Usar el venv del repo: `.venv-test/Scripts/python.exe` (el `python` del sistema no tiene
  `pydantic_settings`).
- **Corpus de 50 frases** (`eval/f15-frases-conversacion/medir_corpus.py`): **50/50 (100%)**,
  0 falsos positivos, 0 falsos negativos, 48 llamadas, ~145 s.
- **Fail-closed end-to-end** (camino real, no simulado): contenedor con
  `INTENT_MODEL=noexiste/modelo-invalido` → la capa da 404 → **0 búsquedas**, decisión
  `fail-safe` en `intent_decisions`, y el agente contesta honesto y deriva a humano. Control con el
  modelo válido, mismo mensaje y misma conversación: **6 búsquedas**, decisión
  `llm/consulta_medicamento`.
- **Laboratorio** (3 corridas, solo modelos gratis/baratos): referencia 100 (11 verdes); corrida con
  estado sucio 82 (descartada); corrida limpia 82 = 9 verdes + 2 rojos, **los dos rojos son del
  código nuevo del cliente** (ver §4).
- En el Laboratorio real: **0 términos basura** buscados (antes había `ladrones`, `estafa`,
  `puedo devolverlas`) y **0 búsquedas sin atribución**.

## 4. PENDIENTE #1 — el siguiente hueco real (ya reproducido)

**Una vez que la capa autoriza el turno, el MODELO puede lanzar sus propias búsquedas con términos
genéricos o ruido**, y esos resultados sí llegan al cliente. Reproducido con `vivacina`: en algunas
corridas el agente termina listando **VITAMINA C/K/E** — la categoría que F14 cerró en la
*extracción* del término, pero **no** en las búsquedas que el modelo decide por su cuenta
(`vitamina`, `acabo`, `ustedes`). Pasa en las dos ramas y de forma no determinista: **no lo introdujo
el cambio del codificador**, es nuestro.

Arreglo sugerido (elegir y medir, no adivinar):
1. Exigir a **toda** búsqueda la misma evidencia farmacéutica que exige la capa (forma farmacéutica,
   dosis, o typo creíble) — hoy solo la exige la autorización del turno.
2. O, en un turno ya autorizado, permitir únicamente el término que la capa aprobó (y registrar como
   `rechazado` cualquier otro término que pida el modelo).

Antes de tocar código: escribir el spec en `docs/` y **commitearlo**, como se hizo con F14/F15.

## 5. PENDIENTE #2 — para el cliente, no nuestro

Los 2 rojos del Laboratorio salen de sus commits; las sugerencias concretas están en
`eval/f15-frases-conversacion/REPORT.md` §7.3 y §7.4:
- `fuera_de_kb`: el guard `_es_peticion_de_envio` afirma «hacemos entregas a domicilio» sin saber el
  horario (`app/turn.py:607` y `:614`, commit `732cf1e`). Control: con la imagen previa el agente no
  afirmaba nada.
- `receta_foto`: la ruta de OCR/foto hace que el agente afirme «leí tu receta».

## 6. Trampas ya medidas (no repetirlas)

- **Estado fresco obligatorio en el Laboratorio**: las personas usan `wa_identity` fijas; si se reusa
  la DB, el chat queda en handoff y salen rojos/amarillos fantasma. Recrear la DB **desechable**
  (`nea-db-qa-j`), nunca el volumen del cliente (`nea_data`).
- El **Laboratorio mide comportamiento, no precisión** (sin credenciales de Firebase su catálogo
  responde vacío) y oscila **±14** entre corridas idénticas.
- El **simulador de conversaciones corre en proceso** (store en memoria): sirve para comportamiento,
  no deja filas en la DB ni logs del contenedor. Para el camino real usar el endpoint `POST /chat`.
- Turnos que terminan **en silencio**: suele ser el tope de rondas de herramientas del modelo (+ G5
  vetando una afirmación de composición), **no** la capa de comprensión.
- `docker cp` con destino en el host: usar ruta nativa (`C:/...`), nunca `/tmp` (docker es binario
  nativo y escribe en `D:\tmp`).

## 7. Reglas de trabajo (del usuario)

- Push **solo** a `desarrollo` de rsomoza01 o gentefarma. Nunca a `main` de nadie ni al upstream.
- **No romper las guardas G1-G12**: `app/guards.py` intocable, cadena de vetos en `turn.py` intacta.
  Cambios aditivos.
- **No ejecutar corridas del Laboratorio con modelos de alto costo.** Agente en
  `deepseek/deepseek-v4.1-flash` ($0.05/$0.25 por M) y `model` del POST del Lab en
  `meituan/longcat-2.0` (gratis). Para cualquier tarea cara: proponer modelo y esperar confirmación.
- Nada de **Ollama local**. Modelos Nous por el proxy local `:11500`.
- No borrar ni escribir fuera del espacio de desarrollo. **Nunca** tocar la DB/volumen del cliente.
- Entregables en `docs/` o `eval/`, **commiteados** (al usuario le molestan los untracked).

## 8. Arranque del entorno (recetas cortas)

```bash
# 0) proxy Nous local (si está caído; comprobar con curl al /v1/chat/completions)
cd /d/Proyectos/GenteFarma/voceroCRM && python scripts/llm-proxy.py 11500

# 1) secretos locales (POSTGRES_PASSWORD, BOT_API_KEY) desde voceroCRM/.env
eval "$(grep -E '^(POSTGRES_PASSWORD|BOT_API_KEY)=' .env | tr -d '\r')"

# 2) DB desechable FRESCA + contenedor del agente con el código actual
docker rm -f nea-db-qa-j vocerocrm-nea-j
docker run -d --name nea-db-qa-j --network vocerocrm_default \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=nea postgres:16-alpine
cd /d/Proyectos/GenteFarma/agentNEA && docker build -q -t vocerocrm-nea:latest .
docker run -d --name vocerocrm-nea-j --network vocerocrm_default --network-alias nea \
  -e CRM_BASE_URL=http://app:3000 -e CRM_BOT_API_KEY="$BOT_API_KEY" \
  -e OPENAI_API_KEY=ollama -e OPENAI_MODEL=deepseek/deepseek-v4.1-flash \
  -e OPENAI_BASE_URL=http://host.docker.internal:11500/v1 \
  -e DATABASE_URL="postgresql://postgres:$POSTGRES_PASSWORD@nea-db-qa-j:5432/nea" \
  -e AGENT_NAME=Nea -e AGENT_TIMEZONE=America/Caracas vocerocrm-nea:latest
docker exec vocerocrm-app-1 sh -c "wget -qO- http://nea:8000/health"   # {"status":"ok","db":"ok"}

# 3) tests y corpus (con el venv del repo)
.venv-test/Scripts/python.exe -m pytest tests/ -q
OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://127.0.0.1:11500/v1 INTENT_PROXY=http://127.0.0.1:11500/v1 \
  .venv-test/Scripts/python.exe eval/f15-frases-conversacion/medir_corpus.py --model deepseek/deepseek-v4.1-flash

# 4) camino real del agente (un turno, sin WhatsApp): el mismo endpoint que usa el Laboratorio
#    Necesita una conversación de prueba del CRM con ai_enabled=true y last_inbound_at reciente.
docker exec -e K="$BOT_API_KEY" vocerocrm-app-1 sh -c "wget -qO- \
  --header='Content-Type: application/json' --header=\"X-API-Key: \$K\" \
  --post-data='{\"conversationId\":\"<cv_...>\",\"text\":\"tienen losartan?\",\"waIdentity\":\"<wa_...>\"}' \
  http://nea:8000/chat"

# 5) auditoría de lo que decidió la capa
docker exec nea-db-qa-j psql -U postgres -d nea -c \
  "select fuente, acto, hay_consulta, motivo from intent_decisions order by id desc limit 10;"

# 6) Laboratorio (juez gratis): auth con el usuario QA del lab (ver skill) y
curl -sk -b cookies.txt -X POST https://localhost/api/lab/runs \
  -H 'Content-Type: application/json' -d '{"model":"meituan/longcat-2.0"}'
curl -sk -b cookies.txt https://localhost/api/lab/runs/<runId>   # {run,cases}: status/score/veredictos
```

## 9. Primer paso sugerido de la sesión nueva

1. `git fetch origin` y confirmar que `desarrollo` sigue en `be8d8ce` (o integrar lo que el cliente
   haya subido encima, siempre con fast-forward/merge, nunca force).
2. Correr `pytest` + el corpus para confirmar el punto de partida (~2 min, sin costo relevante).
3. Escribir el spec del **PENDIENTE #1** en `docs/`, commitearlo, y recién después implementar y
   medir con el corpus + el Laboratorio (modelos baratos/gratis).
