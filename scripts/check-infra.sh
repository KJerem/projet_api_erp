#!/usr/bin/env bash
# Smoke test de la Phase 0 : chaque système répond via son API officielle,
# avec les credentials générés par les bootstraps (lus dans le volume secrets).
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONIOENCODING=utf-8

pass=0; fail=0
check() {  # check "libellé" commande...
  local label="$1"; shift
  if out="$("$@" 2>&1)"; then echo "PASS  $label"; pass=$((pass+1)); [ -n "$out" ] && echo "      $out"
  else echo "FAIL  $label"; echo "      $out"; fail=$((fail+1)); fi
}

# Lecture des secrets générés (jamais stockés dans Git).
# MSYS_NO_PATHCONV : empêche Git Bash (Windows) de réécrire "/secrets/..." en chemin Windows.
read_secret() { MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T --entrypoint cat moodle-bootstrap "/secrets/$1" 2>/dev/null; }
ERP_CREDS="$(read_secret erpnext.json)" || { echo "FAIL  secret ERPNext illisible (bootstrap terminé ?)"; exit 1; }
ERP_TOKEN="token $(echo "$ERP_CREDS" | python -c 'import json,sys; c=json.load(sys.stdin); print(c["api_key"]+":"+c["api_secret"])')"
MOODLE_TOKEN="$(read_secret moodle_token)" || { echo "FAIL  token Moodle illisible"; exit 1; }
ERP=http://localhost:${ERPNEXT_PORT:-8080}
MOODLE=http://localhost:${MOODLE_PORT:-8081}

check "ERPNext : authentification par token API" \
  bash -c "curl -fsS -H 'Authorization: $ERP_TOKEN' $ERP/api/method/frappe.auth.get_logged_user"
check "ERPNext : GET /api/resource/Customer" \
  bash -c "curl -fsS -H 'Authorization: $ERP_TOKEN' '$ERP/api/resource/Customer?limit_page_length=1'"
check "ERPNext : articles de formation + prix" \
  bash -c "curl -fsS -G -H 'Authorization: $ERP_TOKEN' '$ERP/api/resource/Item%20Price' \
    --data-urlencode 'fields=[\"item_code\",\"price_list_rate\",\"currency\"]' \
    --data-urlencode 'filters=[[\"item_code\",\"like\",\"FORMATION-%\"]]'"
check "Moodle : core_webservice_get_site_info" \
  bash -c "curl -fsS '$MOODLE/webservice/rest/server.php' -d wstoken=$MOODLE_TOKEN -d wsfunction=core_webservice_get_site_info -d moodlewsrestformat=json \
    | python -c 'import json,sys; d=json.load(sys.stdin); assert \"exception\" not in d, d; print(d[\"sitename\"], d[\"release\"])'"
check "Moodle : core_course_get_courses_by_field (PYTHON-ADV)" \
  bash -c "curl -fsS '$MOODLE/webservice/rest/server.php' -d wstoken=$MOODLE_TOKEN -d wsfunction=core_course_get_courses_by_field -d field=shortname -d value=PYTHON-ADV -d moodlewsrestformat=json \
    | python -c 'import json,sys; c=json.load(sys.stdin)[\"courses\"]; assert c, \"cours absent\"; print(c[0][\"id\"], c[0][\"fullname\"])'"
check "RabbitMQ : management API" \
  bash -c "set -a; . ./.env; curl -fsS -u \"\$RABBITMQ_USER:\$RABBITMQ_PASSWORD\" http://localhost:\${RABBITMQ_MGMT_PORT:-15672}/api/overview | python -c 'import json,sys; print(\"RabbitMQ\", json.load(sys.stdin)[\"rabbitmq_version\"])'"
check "PostgreSQL d'intégration : pg_isready" \
  docker compose exec -T integration-db pg_isready -q
check "Mailpit : API" \
  bash -c "curl -fsS http://localhost:${MAILPIT_UI_PORT:-8025}/api/v1/info | python -c 'import json,sys; print(\"Mailpit\", json.load(sys.stdin)[\"Version\"])'"

echo; echo "Résultat : $pass PASS / $fail FAIL"
[ "$fail" -eq 0 ]
