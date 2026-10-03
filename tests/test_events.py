"""Enveloppe d'événement v1 et nommage des queues de retry."""

import uuid
from datetime import UTC, datetime, timedelta

from shared.events import REGISTRATION_ACCEPTED, SCHEMA_VERSION, new_event
from shared.rabbit import RETRY_DELAYS, retry_queue_name


def test_new_event_envelope():
    payload = {"registrationId": "r-1", "sessionId": "SESSION-PYTHON-01"}
    event = new_event(REGISTRATION_ACCEPTED, payload, "corr-123")
    assert set(event) == {"eventId", "eventType", "schemaVersion", "correlationId", "occurredAt", "payload"}
    assert uuid.UUID(event["eventId"]).version == 4
    assert event["eventType"] == "registration.accepted"
    assert event["schemaVersion"] == SCHEMA_VERSION == 1
    assert event["correlationId"] == "corr-123"
    assert event["payload"] == payload


def test_new_event_occurred_at_is_utc_with_z_suffix():
    event = new_event(REGISTRATION_ACCEPTED, {}, "c")
    occurred = event["occurredAt"]
    assert occurred.endswith("Z") and "+00:00" not in occurred
    # Précision milliseconde : 2026-10-03T12:34:56.789Z
    assert len(occurred.split(".")[1]) == 4
    parsed = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert abs(datetime.now(UTC) - parsed) < timedelta(seconds=5)


def test_new_event_ids_are_unique():
    assert len({new_event("x", {}, "c")["eventId"] for _ in range(100)}) == 100


def test_retry_queue_name():
    assert retry_queue_name("registration.orchestrator", 2) == "registration.orchestrator.retry.2s"
    assert [retry_queue_name("notifications", d) for d in RETRY_DELAYS] == [
        "notifications.retry.2s", "notifications.retry.4s", "notifications.retry.8s"]


def test_retry_delays_backoff():
    assert RETRY_DELAYS == (2, 4, 8)
