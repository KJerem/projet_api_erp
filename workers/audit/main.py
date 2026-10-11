"""Audit Worker — consomme `audit` (binding « # » : tous les événements du SI) et les journalise.

Naturellement idempotent : audit_log.event_id est UNIQUE (INSERT … ON CONFLICT DO NOTHING).

Usage : python -m workers.audit.main
"""

import logging

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from shared.config import get_settings
from shared.consumer import Consumer
from shared.db import create_pool
from shared.logging_setup import setup_logging
from shared.rabbit import QUEUE_AUDIT

CONSUMER_NAME = "audit-worker"
log = logging.getLogger(CONSUMER_NAME)


class AuditHandler:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def handle(self, event: dict, attempt: int) -> None:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO audit_log (event_id, event_type, correlation_id, payload) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (event_id) DO NOTHING",
                (event["eventId"], event["eventType"], event.get("correlationId"), Jsonb(event)),
            )
            conn.execute("INSERT INTO processed_events (consumer_name, event_id) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (CONSUMER_NAME, event["eventId"]))
        log.info("événement audité", extra={"eventId": event["eventId"], "eventType": event["eventType"],
                                            "registrationId": event["payload"].get("registrationId")})

    def on_retry(self, event: dict, error: Exception, attempt: int, delay: int) -> None:
        pass

    def on_give_up(self, event: dict, error: Exception, attempts: int) -> str:
        return "dlq"


def main() -> None:
    settings = get_settings()
    setup_logging(settings.service_name, settings.log_level)
    pool = create_pool(settings)
    pool.open(wait=True, timeout=60)
    Consumer(CONSUMER_NAME, QUEUE_AUDIT, settings, pool, AuditHandler(pool)).run()


if __name__ == "__main__":
    main()
