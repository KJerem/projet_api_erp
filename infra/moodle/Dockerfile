# Moodle 5.3.0 (dernière stable) sur l'image PHP officielle maintenue par Moodle HQ.
# Le webroot de Moodle >= 5.1 est `public/` : l'image moodlehq le détecte automatiquement.
FROM moodlehq/moodle-php-apache:8.4

ARG MOODLE_VERSION=v5.3.0

COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer

RUN cd / && rm -rf /var/www/html \
    && git clone --depth 1 --branch "${MOODLE_VERSION}" https://github.com/moodle/moodle.git /var/www/html \
    && cd /var/www/html \
    && COMPOSER_ALLOW_SUPERUSER=1 composer install --no-dev --classmap-authoritative --no-interaction --no-progress \
    && rm -rf .git \
    && mkdir -p /var/www/moodledata \
    && chown -R www-data:www-data /var/www/html /var/www/moodledata

COPY config.php /var/www/html/config.php
COPY setup_ws.php /opt/opentraining/setup_ws.php
# Exécuté par l'entrypoint moodlehq avant le démarrage d'Apache (après 10-wwwroot.sh).
COPY --chmod=0755 50-opentraining-setup.sh /docker-entrypoint.d/50-opentraining-setup.sh

HEALTHCHECK --interval=10s --timeout=5s --start-period=60s --retries=60 \
    CMD curl -fsS -o /dev/null http://localhost/login/index.php || exit 1
