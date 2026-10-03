"""Enveloppe d'événement v1 et écriture dans l'outbox (toujours dans la transaction métier de l'appelant)."""

import uuid
from datetime import UTC, datetime

from psycopg import Cursor
from psycopg.types.json import Jsonb

SCHEMA_VERSION = 1

REGISTRATION_ACCEPTED = "registration.accepted"
REGISTRATION_CONFIRMED = "registration.confirmed"
REGISTRATION_FAILED = "registration.failed"
REGISTRATION_COMPENSATED = "registration.compensated"


def new_event(event_type: str, payload: dict, correlation_id: str) -> dict:
    return {
        "eventId": str(uuid.uuid4()),
        "eventType": event_type,
        "schemaVersion": SCHEMA_VERSION,
        "correlationId": correlation_id,
        "occurredAt": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "payload": payload,
    }


def insert_outbox(cur: Cursor, event: dict, aggregate_type: str, aggregate_id: str) -> None:
    """Le routing key RabbitMQ est le type d'événement (ex. registration.accepted)."""
    cur.execute(
        """
        INSERT INTO outbox_event (event_id, event_type, routing_key, aggregate_type, aggregate_id, correlation_id, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event["eventId"],
            event["eventType"],
            event["eventType"],
            aggregate_type,
            aggregate_id,
            event["correlationId"],
            Jsonb(event),
        ),
    )
