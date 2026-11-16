#!/usr/bin/env bash
# F15.1 — repeticiones del escenario que reprodujo el hueco en el Laboratorio («tienen
# vivacina?»), para comparar VIEJA vs NUEVA imagen EN EL CAMINO REAL.
#
# El hueco es no determinista (el modelo decide si lanza un segundo término), así que una
# sola pasada no prueba nada: se repite el MISMO mensaje con identidades NUEVAS y se cuentan
# los términos que el agente intentó buscar, con el log del contenedor como fuente.
#
# Uso: bash eval/f15-frases-conversacion/camino-real-reps.sh <imagen> <etiqueta> <sufijo> <reps>
set -u

IMG="${1:?imagen}"
ETIQUETA="${2:?etiqueta}"
SUF="${3:?sufijo}"
REPS="${4:-4}"

AGENTE=/d/Proyectos/GenteFarma/agentNEA
CRM=/d/Proyectos/GenteFarma/voceroCRM
SALIDA="$AGENTE/eval/f15-frases-conversacion/camino-real-reps-$ETIQUETA"
mkdir -p "$SALIDA"

cd "$CRM"
eval "$(grep -E '^(POSTGRES_PASSWORD|BOT_API_KEY)=' .env | tr -d '\r')"
PG="vocerocrm-postgres-1"
DB="nea-db-qa-$SUF"
NEA="vocerocrm-nea-$SUF"

if [ "${SKIP_SETUP:-0}" != "1" ]; then
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

: > "$SALIDA/respuestas.jsonl"
echo "=== $REPS turnos de «tienen vivacina?» con la imagen $IMG ==="
for r in $(seq 1 "$REPS"); do
  ct="ct_f151rep${SUF}${r}"; cv="cv_f151rep${SUF}${r}"; wa="58413${SUF}000$r"
  docker exec "$PG" psql -U postgres -d vocero -q -c "
    delete from conversation where id='$cv';
    delete from contact where id='$ct';
    insert into contact (id, organization_id, name, wa_identity, created_at, updated_at)
      values ('$ct', 'org_eh8sds294yxw3mebiy6o', '[F15.1 rep $r] vivacina', '$wa', now(), now());
    insert into conversation (id, organization_id, contact_id, is_test, ai_enabled, last_inbound_at, last_message_at, created_at, updated_at)
      values ('$cv', 'org_eh8sds294yxw3mebiy6o', '$ct', true, true, now(), now(), now(), now());" >/dev/null
  antes=$(docker logs "$NEA" 2>&1 | wc -l)
  RESPUESTA=$(docker exec -e K="$BOT_API_KEY" vocerocrm-app-1 sh -c \
    "wget -qO- --header='Content-Type: application/json' --header=\"X-API-Key: \$K\" \
     --post-data='{\"conversationId\":\"$cv\",\"text\":\"tienen vivacina?\",\"waIdentity\":\"$wa\"}' \
     http://nea:8000/chat" 2>&1)
  echo "{\"rep\":$r,\"respuesta\":$RESPUESTA}" >> "$SALIDA/respuestas.jsonl"
  echo "turno $r → $(echo "$RESPUESTA" | head -c 220)"
done

echo "=== términos que el agente intentó buscar (log del contenedor) ==="
docker logs "$NEA" 2>&1 | grep -E "buscar_medicamento:|NO autorizada por la capa|no es una consulta|no busco en catálogo|solo muletilla" \
  | grep -v "typing" > "$SALIDA/terminos.txt" || true
sed -E "s/.*(buscar_medicamento|búsqueda NO autorizada)[^\n]*/\0/" "$SALIDA/terminos.txt" | sort | uniq -c | sort -rn | head -40
echo
echo "=== auditoría ==="
docker exec "$DB" psql -U postgres -d nea -c \
  "select candidato, termino, hay_consulta, acto, motivo from intent_decisions order by id;" \
  > "$SALIDA/intent_decisions.txt" 2>&1
docker exec "$DB" psql -U postgres -d nea -c \
  "select term, result_count, intent_fuente, intent_acto from med_queries order by id;" \
  > "$SALIDA/med_queries.txt" 2>&1
cat "$SALIDA/intent_decisions.txt" "$SALIDA/med_queries.txt"
echo "guardado en $SALIDA"
