#!/usr/bin/env bash
# 503 Service Unavailable : base d'intégration arrêtée, puis reprise automatique de l'API.
set -uo pipefail
cd "$(dirname "$0")/.."
API=${API_URL:-http://localhost:8000}
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS  $1 ($2)"; else echo "FAIL  $1 (obtenu $2, attendu $3)"; fail=1; fi; }
post() {
  curl -s -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/registrations" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $1" \
    -d "{\"student\":{\"email\":\"$1@example.org\",\"firstName\":\"Db\",\"lastName\":\"Down\"},\"company\":{\"name\":\"ACME\"},\"sessionId\":\"SESSION-PYTHON-01\"}"
}

echo "=== 503 quand PostgreSQL d'intégration est indisponible ==="
docker compose stop integration-db >/dev/null 2>&1
KEY="dbdown-$(date +%s)"
check "POST /registrations base arrêtée -> 503" "$(post "$KEY")" 503
check "GET /ready base arrêtée -> 503" "$(curl -s -o /dev/null -w '%{http_code}' "$API/ready")" 503
check "GET /health reste 200 (liveness)" "$(curl -s -o /dev/null -w '%{http_code}' "$API/health")" 200

docker compose start integration-db >/dev/null 2>&1
until docker compose exec -T integration-db pg_isready -q; do sleep 1; done
sleep 2
check "même Idempotency-Key rejouée après reprise -> 202" "$(post "$KEY")" 202
check "GET /ready après reprise -> 200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/ready")" 200

[ $fail -eq 0 ] && echo -e "\nRésultat : OK" || { echo -e "\nRésultat : échec"; exit 1; }
