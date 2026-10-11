"""Outbox Dispatcher : publie dans RabbitMQ les événements de la table outbox_event.

Algorithme (une itération) :
  1. BEGIN ; SELECT … WHERE published_at IS NULL ORDER BY created_at LIMIT n FOR UPDATE SKIP LOCKED
  2. publication de chaque événement (message persistant, publisher confirm attendu)
  3. UPDATE published_at = now() pour les événements confirmés par le broker ; COMMIT

Pannes :
  - RabbitMQ indisponible : rien n'est marqué publié, les événements restent dans l'outbox, reconnexion.
  - Crash entre la confirmation du broker et le COMMIT : l'événement sera republié (doublon possible).
    C'est la garantie at-least-once ; les consumers dédupliquent par eventId (processed_events).
  - Plusieurs dispatchers : SKIP LOCKED répartit les lignes sans double publication simultanée.

Usage : python -m workers.outbox.main
"""

import logging
import os
import signal
import time

import psycopg
from pika.exceptions import AMQPError
from psycopg_pool import PoolTimeout

from shared.config import get_settings
from shared.db import create_pool
from shared.logging_setup import correlation_id_var, setup_logging
from shared.rabbit import EXCHANGE, connect, declare_topology, publish_event

settings = get_settings()
setup_logging(settings.service_name, settings.log_level)
log = logging.getLogger("outbox-dispatcher")

BATCH_SIZE = int(os.environ.get("OUTBOX_BATCH_SIZE", "50"))
POLL_INTERVAL = float(os.environ.get("OUTBOX_POLL_INTERVAL_SECONDS", "0.5"))

_running = True


def _stop(signum, _frame) -> None:
    global _running
    log.info("arrêt demandé", extra={"signal": signum})
    _running = False


def dispatch_batch(pool, channel) -> int:
    """Publie un lot ; retourne le nombre d'événements publiés. Lève AMQPError si RabbitMQ tombe,
    APRÈS avoir commité published_at pour les événements déjà confirmés par le broker."""
    published: list = []
    amqp_error: Exception | None = None
    with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_id, routing_key, correlation_id, payload
            FROM outbox_event
            WHERE published_at IS NULL
            ORDER BY created_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (BATCH_SIZE,),
        )
        rows = cur.fetchall()
        for row in rows:
            correlation_id_var.set(row["correlation_id"])
            try:
                publish_event(channel, EXCHANGE, row["routing_key"], row["payload"])
            except AMQPError as exc:
                amqp_error = exc
                cur.execute(
                    "UPDATE outbox_event SET publish_attempts = publish_attempts + 1, last_error = %s "
                    "WHERE event_id = %s",
                    (repr(exc)[:1000], row["event_id"]),
                )
                break
            published.append(row["event_id"])
            log.info(
                "événement publié (confirmé par le broker)",
                extra={"eventId": str(row["event_id"]), "eventType": row["routing_key"],
                       "registrationId": row["payload"]["payload"].get("registrationId")},
            )
        correlation_id_var.set(None)
        if published:
            cur.execute(
                "UPDATE outbox_event SET published_at = now(), publish_attempts = publish_attempts + 1, "
                "last_error = NULL WHERE event_id = ANY(%s)",
                (published,),
            )
    # COMMIT effectué : on peut maintenant signaler la panne RabbitMQ à l'appelant.
    if amqp_error is not None:
        raise amqp_error
    return len(published)


def run() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    pool = create_pool(settings)
    pool.open(wait=True, timeout=60)

    while _running:
        connection = connect(settings, connection_name="outbox-dispatcher")
        channel = connection.channel()
        declare_topology(channel)
        channel.confirm_delivery()
        log.info("connecté à RabbitMQ, topologie déclarée")
        try:
            while _running:
                try:
                    count = dispatch_batch(pool, channel)
                except (psycopg.Error, PoolTimeout) as exc:
                    # Base indisponible : rien n'a été marqué publié, on réessaie sans couper AMQP.
                    log.warning("base d'intégration indisponible", extra={"error": repr(exc)})
                    connection.sleep(2)
                    continue
                if count < BATCH_SIZE:
                    connection.sleep(POLL_INTERVAL)  # maintient les heartbeats AMQP pendant l'attente
        except AMQPError as exc:
            log.warning("connexion RabbitMQ perdue, les événements restent dans l'outbox", extra={"error": repr(exc)})
            time.sleep(2)
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - connexion déjà fermée
                pass

    pool.close()
    log.info("dispatcher arrêté")


if __name__ == "__main__":
    run()
