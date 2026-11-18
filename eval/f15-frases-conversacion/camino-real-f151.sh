#!/usr/bin/env bash
# F15.1 — camino REAL (`POST /chat`), para comparar la imagen VIEJA y la NUEVA.
#
# Por qué no sirve el simulador: `simular_conversaciones.py` corre EN PROCESO (AppContext
# + MemoryStore): no deja filas en `intent_decisions` ni logs del contenedor, que es
# justamente lo que hay que mirar aquí (qué término pidió el MODELO y qué se rechazó).
#
# Qué hace, por corrida:
#   1. DB desechable FRESCA (nunca el volumen del cliente) + contenedor del agente.
#   2. Crea conversaciones de prueba NUEVAS en el CRM (is_test, ai_enabled, last_inbound_at=now).
#   3. Manda cada escenario con POST /chat (el mismo endpoint que usa el Laboratorio).
#   4. Guarda: respuestas, `intent_decisions`, `med_queries` y los términos que el agente
#      intentó buscar (del log del contenedor).
#
# Uso:
#   bash eval/f15-frases-conversacion/camino-real-f151.sh vocerocrm-nea:latest      nuevo
#   bash eval/f15-frases-conversacion/camino-real-f151.sh vocerocrm-nea:prefix151   viejo
set -u

IMG="${1:?imagen (vocerocrm-nea:latest | vocerocrm-nea:prefix151)}"
ETIQUETA="${2:?etiqueta de la corrida (nuevo | viejo)}"
SUF="${3:?sufijo de contenedores/ids (j1 | j2)}"
DESDE="${4:-1}"   # escenario por el que se empieza (para retomar una corrida cortada)

AGENTE=/d/Proyectos/GenteFarma/agentNEA
CRM=/d/Proyectos/GenteFarma/voceroCRM
SALIDA="$AGENTE/eval/f15-frases-conversacion/camino-real-f151-$ETIQUETA"
mkdir -p "$SALIDA"

cd "$CRM"
eval "$(grep -E '^(POSTGRES_PASSWORD|BOT_API_KEY)=' .env | tr -d '\r')"
PG="vocerocrm-postgres-1"
DB="nea-db-qa-$SUF"
NEA="vocerocrm-nea-$SUF"

if [ "${SKIP_SETUP:-0}" != "1" ]; then
echo "=== 1) DB desechable fresca + contenedor del agente ($IMG) ==="
docker rm -f "$DB" "$NEA" >/dev/null 2>&1
docker run -d --name "$DB" --network vocerocrm_default \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=nea \
  postgres:16-alpine >/dev/null

for i in $(seq 1 30); do
  docker exec "$DB" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 2
done

docker run -d --name "$NEA" --network vocerocrm_default --network-alias nea \
  -e CRM_BASE_URL=http://app:3000 -e CRM_BOT_API_KEY="$BOT_API_KEY" \
  -e OPENAI_API_KEY=ollama -e OPENAI_MODEL=deepseek/deepseek-v4.1-flash \
  -e OPENAI_BASE_URL=http://host.docker.internal:11500/v1 \
  -e DATABASE_URL="postgresql://postgres:$POSTGRES_PASSWORD@$DB:5432/nea" \
  -e AGENT_NAME=Nea -e AGENT_TIMEZONE=America/Caracas "$IMG" >/dev/null

for i in $(seq 1 40); do
  SALUD=$(docker exec vocerocrm-app-1 sh -c "wget -qO- http://nea:8000/health" 2>/dev/null || true)
  case "$SALUD" in *'"status":"ok"'*) echo "health: $SALUD"; break;; esac
  sleep 3
done
fi

echo "=== 2) conversaciones de prueba nuevas en el CRM ==="
# identidades y conversaciones NUEVAS por corrida: el estado del agente (na) se indexa por
# wa_identity, así que reutilizarlas arrastraría el historial de la corrida anterior.
escenarios=(
  "vivacina|tienen vivacina?"
  "ruido|hola, tienen vivacina? de paso les cuento que no me sirvieron las que compre la semana pasada"
  "dos-med|necesito losartan y metformin"
  "charla|solo una marca? no era indu"
)
for i in "${!escenarios[@]}"; do
  n=$((i + 1))
  ct="ct_f151${SUF}${n}"; cv="cv_f151${SUF}${n}"; wa="58411${SUF}000$n"
  docker exec "$PG" psql -U postgres -d vocero -q -c "
    delete from conversation where id='$cv';
    delete from contact where id='$ct';
    insert into contact (id, organization_id, name, wa_identity, created_at, updated_at)
      values ('$ct', 'org_eh8sds294yxw3mebiy6o', '[F15.1] ${escenarios[$i]%%|*}', '$wa', now(), now());
    insert into conversation (id, organization_id, contact_id, is_test, ai_enabled, last_inbound_at, last_message_at, created_at, updated_at)
      values ('$cv', 'org_eh8sds294yxw3mebiy6o', '$ct', true, true, now(), now(), now(), now());" >/dev/null
done

echo "=== 3) POST /chat por escenario ==="
if [ "$DESDE" = "1" ]; then
  : > "$SALIDA/respuestas.jsonl"
  : > "$SALIDA/terminos-intentados.txt"
fi
for i in "${!escenarios[@]}"; do
  n=$((i + 1))
  if [ "$n" -lt "$DESDE" ]; then continue; fi
  nombre="${escenarios[$i]%%|*}"
  texto="${escenarios[$i]#*|}"
  cv="cv_f151${SUF}${n}"; wa="58411${SUF}000$n"
  antes=$(docker logs "$NEA" 2>&1 | wc -l)
  CUERPO=$(printf '{"conversationId":"%s","text":"%s","waIdentity":"%s"}' "$cv" "$texto" "$wa")
  RESPUESTA=$(docker exec -e K="$BOT_API_KEY" -e B="$CUERPO" vocerocrm-app-1 sh -c \
    "wget -qO- --header='Content-Type: application/json' --header=\"X-API-Key: \$K\" --post-data=\"\$B\" http://nea:8000/chat" 2>&1)
  echo "{\"escenario\":\"$nombre\",\"texto\":\"$texto\",\"respuesta\":$RESPUESTA}" >> "$SALIDA/respuestas.jsonl"
  echo "--- $nombre" >> "$SALIDA/terminos-intentados.txt"
  docker logs "$NEA" 2>&1 | tail -n +"$antes" | grep -E "buscar_medicamento|NO autorizada|autorización de búsqueda|no es una consulta" \
    >> "$SALIDA/terminos-intentados.txt" || true
  echo "$nombre → $RESPUESTA"
done

echo "=== 4) auditoría: intent_decisions + med_queries ==="
docker exec "$DB" psql -U postgres -d nea -c \
  "select fuente, acto, hay_consulta, candidato, termino, motivo from intent_decisions order by id;" \
  > "$SALIDA/intent_decisions.txt" 2>&1
docker exec "$DB" psql -U postgres -d nea -c \
  "select term, result_count, intent_fuente, intent_acto, intent_motivo from med_queries order by id;" \
  > "$SALIDA/med_queries.txt" 2>&1
cat "$SALIDA/intent_decisions.txt" "$SALIDA/med_queries.txt"
echo "guardado en $SALIDA"
