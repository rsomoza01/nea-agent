#!/usr/bin/env bash
# F15.1 — corrida PAREADA del Laboratorio: imagen PRE-F15.1 vs imagen con F15.1.
#
# Por qué: el 82 de referencia de las fases anteriores es un número DOCUMENTADO, no medido hoy.
# Con la banda de oscilación ±14, la única forma de atribuir un delta es correr las dos imágenes
# el mismo día, con DB desechable fresca cada una y el mismo juez gratuito.
#
# Uso: bash eval/lab-ab-f151.sh
set -u

AGENTE=/d/Proyectos/GenteFarma/agentNEA
CRM=/d/Proyectos/GenteFarma/voceroCRM
cd "$CRM"
eval "$(grep -E '^(POSTGRES_PASSWORD|BOT_API_KEY)=' .env | tr -d '\r')"
PG=vocerocrm-postgres-1

correr() {
  local IMG="$1" ETIQUETA="$2" SUF="$3"
  local DB="nea-db-qa-$SUF" NEA="vocerocrm-nea-$SUF"
  echo "=== [$ETIQUETA] DB fresca + agente con $IMG ==="
  docker stop vocerocrm-nea-j nea-db-qa-a nea-db-qa-b vocerocrm-nea-a vocerocrm-nea-b >/dev/null 2>&1
  docker rm -f "$DB" "$NEA" >/dev/null 2>&1
  docker run -d --name "$DB" --network vocerocrm_default \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=nea \
    postgres:16-alpine >/dev/null
  for i in $(seq 1 30); do docker exec "$DB" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 2; done
  docker run -d --name "$NEA" --network vocerocrm_default --network-alias nea \
    -e CRM_BASE_URL=http://app:3000 -e CRM_BOT_API_KEY="$BOT_API_KEY" \
    -e OPENAI_API_KEY=ollama -e OPENAI_MODEL=deepseek/deepseek-v4.1-flash \
    -e OPENAI_BASE_URL=http://host.docker.internal:11500/v1 \
    -e DATABASE_URL="postgresql://postgres:$POSTGRES_PASSWORD@$DB:5432/nea" \
    -e AGENT_NAME=Nea -e AGENT_TIMEZONE=America/Caracas "$IMG" >/dev/null
  for i in $(seq 1 40); do
    S=$(docker exec vocerocrm-app-1 sh -c "wget -qO- http://nea:8000/health" 2>/dev/null || true)
    case "$S" in *'"status":"ok"'*) echo "health: $S"; break;; esac
    sleep 3
  done
  echo "    limpiando corridas zombis del CRM..."
  docker exec "$PG" psql -U postgres -d vocero -q -c "update agent_test_run set status='failed' where status='running';" >/dev/null

  RUN=$(curl -sk -b /tmp/cookies.txt -X POST https://localhost/api/lab/runs \
        -H 'Content-Type: application/json' -d '{"model":"meituan/longcat-2.0"}' \
        | python -c "import json,sys; print(json.load(sys.stdin).get('runId',''))")
  echo "    runId=$RUN"
  for i in $(seq 1 90); do
    ESTADO=$(curl -sk -b /tmp/cookies.txt "https://localhost/api/lab/runs/$RUN" \
             | python -c "import json,sys; print(json.load(sys.stdin)['run']['status'])" 2>/dev/null || echo "?")
    [ "$ESTADO" != "running" ] && break
    sleep 20
  done
  curl -sk -b /tmp/cookies.txt "https://localhost/api/lab/runs/$RUN" > "$AGENTE/eval/lab-ab-f151-$ETIQUETA.json"
  docker exec "$DB" psql -U postgres -d nea -c \
    "select fuente, acto, hay_consulta, candidato from intent_decisions order by id;" \
    > "$AGENTE/eval/lab-ab-f151-$ETIQUETA-intent.txt" 2>&1
  echo "    [$ETIQUETA] estado=$ESTADO guardado en eval/lab-ab-f151-$ETIQUETA.json"
}

correr vocerocrm-nea:prefix151 pre-f151 a
correr vocerocrm-nea:latest    con-f151 b

echo "=== restaurando el contenedor de QA (imagen final, DB fresca) ==="
docker stop vocerocrm-nea-a vocerocrm-nea-b >/dev/null 2>&1
docker rm -f vocerocrm-nea-a vocerocrm-nea-b nea-db-qa-a nea-db-qa-b >/dev/null 2>&1
docker rm -f nea-db-qa-j >/dev/null 2>&1
docker run -d --name nea-db-qa-j --network vocerocrm_default \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=nea postgres:16-alpine >/dev/null
for i in $(seq 1 30); do docker exec nea-db-qa-j pg_isready -U postgres >/dev/null 2>&1 && break; sleep 2; done
docker rm -f vocerocrm-nea-j >/dev/null 2>&1
docker run -d --name vocerocrm-nea-j --network vocerocrm_default --network-alias nea \
  -e CRM_BASE_URL=http://app:3000 -e CRM_BOT_API_KEY="$BOT_API_KEY" \
  -e OPENAI_API_KEY=ollama -e OPENAI_MODEL=deepseek/deepseek-v4.1-flash \
  -e OPENAI_BASE_URL=http://host.docker.internal:11500/v1 \
  -e DATABASE_URL="postgresql://postgres:$POSTGRES_PASSWORD@nea-db-qa-j:5432/nea" \
  -e AGENT_NAME=Nea -e AGENT_TIMEZONE=America/Caracas vocerocrm-nea:latest >/dev/null
echo "listo"
