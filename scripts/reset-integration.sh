#!/usr/bin/env bash
# Remet l'Integration Hub dans un état propre avant une démo :
#   - vide toutes les queues RabbitMQ (métier, retry, DLQ) ;
#   - recrée le schéma de la base d'intégration (migrations + sessions du catalog.json).
# Ne touche NI à Moodle NI à ERPNext (leurs données de test restent en place).
#
# Usage : bash scripts/reset-integration.sh [-y]
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1

if [ "${1:-}" != "-y" ]; then
  read -r -p "Effacer la base d'intégration et vider les queues RabbitMQ ? [o/N] " answer
  [[ "$answer" =~ ^[oOyY]$ ]] || { echo "Annulé."; exit 1; }
fi

set -a; . ./.env; set +a

# Services de l'Integration Hub présents dans le compose (les workers apparaissent au fil des phases).
HUB_SERVICES=$(docker compose config --services | grep -E '^(integration-api|outbox-dispatcher|.*-worker)$' || true)

echo "1/4 arrêt des services du Hub : $(echo $HUB_SERVICES)"
docker compose stop $HUB_SERVICES >/dev/null 2>&1

echo "2/4 purge des queues RabbitMQ"
for queue in $(docker compose exec -T rabbitmq rabbitmqctl list_queues --quiet --no-table-headers name | tr -d '\r'); do
  docker compose exec -T rabbitmq rabbitmqctl purge_queue --quiet "$queue" >/dev/null
  echo "      purgée : $queue"
done

echo "3/4 recréation du schéma de la base d'intégration"
docker compose exec -T integration-db psql -q -v ON_ERROR_STOP=1 \
  -U "${INTEGRATION_DB_USER:-integration}" -d "${INTEGRATION_DB_NAME:-integration}" \
  -c "SET client_min_messages = warning; DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION \"${INTEGRATION_DB_USER:-integration}\";"
docker compose run --rm --no-deps --pull never migrate 2>/dev/null | grep '^{' | sed 's/^/      /'

echo "4/4 redémarrage des services du Hub"
docker compose up -d $HUB_SERVICES >/dev/null 2>&1

echo
echo "Reset terminé. Sessions disponibles :"
docker compose exec -T integration-db psql -At -F ' | ' \
  -U "${INTEGRATION_DB_USER:-integration}" -d "${INTEGRATION_DB_NAME:-integration}" \
  -c "SELECT id, remaining_seats || '/' || capacity || ' places' FROM training_session ORDER BY id" | sed 's/^/      /'
