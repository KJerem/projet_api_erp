#!/usr/bin/env bash
# Installation non interactive de Moodle + activation des Web Services REST.
# Idempotent : peut être rejoué à chaque démarrage du conteneur.
set -Eeuo pipefail

MOODLE_DIR=/var/www/html
CLI_DIR="${MOODLE_DIR}/public/admin/cli"
[ -d "${CLI_DIR}" ] || CLI_DIR="${MOODLE_DIR}/admin/cli"
SECRETS_DIR=/secrets

as_www() { runuser -u www-data -- "$@"; }

mkdir -p /var/www/moodledata
chown -R www-data:www-data /var/www/moodledata

echo "[opentraining] attente de PostgreSQL Moodle (${MOODLE_DB_HOST})..."
until php -r 'exit(@pg_connect(sprintf("host=%s dbname=%s user=%s password=%s connect_timeout=3", getenv("MOODLE_DB_HOST"), getenv("MOODLE_DB_NAME"), getenv("MOODLE_DB_USER"), getenv("MOODLE_DB_PASSWORD"))) ? 0 : 1);'; do
  sleep 2
done

if as_www php "${CLI_DIR}/cfg.php" --name=version >/dev/null 2>&1; then
  echo "[opentraining] Moodle déjà installé, upgrade éventuel..."
  as_www php "${CLI_DIR}/upgrade.php" --non-interactive || true
else
  echo "[opentraining] installation de Moodle..."
  as_www php "${CLI_DIR}/install_database.php" \
    --agree-license --lang=en \
    --adminuser="${MOODLE_ADMIN_USER}" \
    --adminpass="${MOODLE_ADMIN_PASSWORD}" \
    --adminemail="${MOODLE_ADMIN_EMAIL}" \
    --fullname="OpenTraining LMS" \
    --shortname="OpenTraining"
fi

echo "[opentraining] configuration des Web Services..."
TOKEN="$(as_www php /opt/opentraining/setup_ws.php | tail -n 1)"
if [ -z "${TOKEN}" ]; then
  echo "[opentraining] ERREUR : token Web Service vide" >&2
  exit 1
fi

mkdir -p "${SECRETS_DIR}"
umask 077
printf '%s' "${TOKEN}" > "${SECRETS_DIR}/moodle_token.tmp"
# Lisible uniquement par l'utilisateur non-root de l'Integration Hub (uid/gid 10001).
chown "${HUB_UID:-10001}:${HUB_GID:-10001}" "${SECRETS_DIR}/moodle_token.tmp"
chmod 0440 "${SECRETS_DIR}/moodle_token.tmp"
mv "${SECRETS_DIR}/moodle_token.tmp" "${SECRETS_DIR}/moodle_token"
echo "[opentraining] token Web Service écrit dans ${SECRETS_DIR}/moodle_token"
