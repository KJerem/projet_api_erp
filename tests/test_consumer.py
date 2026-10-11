"""Consumer générique : validation de l'enveloppe et décision ack / retry / dlq / reject.

La base (processed_events) est remplacée par un ensemble en mémoire ; RabbitMQ n'est pas utilisé
(_process ne fait que décider, la publication a lieu dans _finish).
"""

import json
import uuid

import pytest

from shared.consumer import MAX_ATTEMPTS, Consumer, InvalidMessage, parse_event
from shared.errors import DuplicateInFlight, PermanentError, TransientError
from shared.events import new_event


def body_of(event: dict) -> bytes:
    return json.dumps(event).encode("utf-8")


def make_event(**overrides) -> dict:
    event = new_event("registration.accepted", {"registrationId": "reg-1"}, "corr-1")
    event.update(overrides)
    return event


# -- parse_event ------------------------------------------------------------------------------------

def test_parse_valid_envelope():
    event = make_event()
    assert parse_event(body_of(event)) == event


@pytest.mark.parametrize("raw", [b"not json", b"", b"[]", b"null", b"\xff\xfe"])
def test_parse_invalid_json(raw):
    with pytest.raises(InvalidMessage):
        parse_event(raw)


def test_parse_event_id_not_uuid():
    with pytest.raises(InvalidMessage):
        parse_event(body_of(make_event(eventId="pas-un-uuid")))


@pytest.mark.parametrize("missing", ["eventId", "payload", "eventType"])
def test_parse_missing_field(missing):
    event = make_event()
    del event[missing]
    with pytest.raises(InvalidMessage):
        parse_event(body_of(event))


def test_parse_payload_must_be_object():
    with pytest.raises(InvalidMessage):
        parse_event(body_of(make_event(payload="texte")))


@pytest.mark.parametrize("version", [2, 0, "1", None])
def test_parse_unsupported_schema_version(version):
    with pytest.raises(InvalidMessage, match="schemaVersion"):
        parse_event(body_of(make_event(schemaVersion=version)))


def test_parse_missing_schema_version():
    event = make_event()
    del event["schemaVersion"]
    with pytest.raises(InvalidMessage):
        parse_event(body_of(event))


# -- Consumer._process ------------------------------------------------------------------------------

class FakeHandler:
    def __init__(self, error: Exception | None = None, give_up: str = "dlq", give_up_error: Exception | None = None):
        self.error = error
        self.give_up = give_up
        self.give_up_error = give_up_error
        self.handled: list[tuple[str, int]] = []
        self.retries: list[tuple[int, int]] = []
        self.give_ups: list[int] = []

    def handle(self, event, attempt):
        self.handled.append((event["eventId"], attempt))
        if self.error is not None:
            raise self.error

    def on_retry(self, event, error, attempt, delay):
        self.retries.append((attempt, delay))

    def on_give_up(self, event, error, attempts):
        self.give_ups.append(attempts)
        if self.give_up_error is not None:
            raise self.give_up_error
        return self.give_up


class Processed:
    """Doublure de la table processed_events."""

    def __init__(self, initial=()):
        self.ids = set(initial)
        self.marked: list[str] = []

    def install(self, consumer: Consumer) -> None:
        consumer.already_processed = lambda event_id: event_id in self.ids
        consumer.mark_processed = self.mark

    def mark(self, event_id):
        self.marked.append(event_id)
        self.ids.add(event_id)


def make_consumer(handler, processed=None, retry_enabled=True) -> tuple[Consumer, Processed]:
    consumer = Consumer("test-consumer", "registration.orchestrator", settings=None, pool=None,
                        handler=handler, retry_enabled=retry_enabled)
    processed = processed or Processed()
    processed.install(consumer)
    return consumer, processed


def test_max_attempts_is_four():
    assert MAX_ATTEMPTS == 4


def test_success_acks_and_marks_processed():
    handler = FakeHandler()
    consumer, processed = make_consumer(handler)
    event = make_event()
    outcome = consumer._process(body_of(event), {})
    assert outcome.kind == "ack"
    assert handler.handled == [(event["eventId"], 1)]
    assert processed.marked == [event["eventId"]]


def test_duplicate_acks_without_calling_handler():
    event = make_event()
    handler = FakeHandler()
    consumer, processed = make_consumer(handler, Processed({event["eventId"]}))
    outcome = consumer._process(body_of(event), {"x-attempt": 2})
    assert outcome.kind == "ack"
    assert handler.handled == []
    assert processed.marked == []


@pytest.mark.parametrize("attempt,delay", [(1, 2), (2, 4), (3, 8)])
def test_transient_error_with_attempts_left_schedules_retry(attempt, delay):
    handler = FakeHandler(TransientError("Moodle timeout"))
    consumer, processed = make_consumer(handler)
    headers = {} if attempt == 1 else {"x-attempt": attempt}
    outcome = consumer._process(body_of(make_event()), headers)
    assert outcome.kind == "retry"
    assert outcome.delay == delay
    assert outcome.headers["x-attempt"] == attempt + 1
    assert "Moodle timeout" in outcome.headers["x-last-error"]
    assert handler.retries == [(attempt, delay)]
    assert handler.give_ups == []
    assert processed.marked == []


def test_unexpected_exception_is_treated_as_transient():
    handler = FakeHandler(RuntimeError("bug ou base indisponible"))
    consumer, _ = make_consumer(handler)
    outcome = consumer._process(body_of(make_event()), {})
    assert outcome.kind == "retry" and outcome.delay == 2


def test_on_retry_failure_does_not_prevent_retry():
    handler = FakeHandler(TransientError("x"))
    handler.on_retry = lambda *a: (_ for _ in ()).throw(RuntimeError("db down"))
    consumer, _ = make_consumer(handler)
    assert consumer._process(body_of(make_event()), {}).kind == "retry"


def test_transient_error_on_fourth_attempt_gives_up_to_dlq():
    handler = FakeHandler(TransientError("ERPNext timeout"))
    consumer, processed = make_consumer(handler)
    outcome = consumer._process(body_of(make_event()), {"x-attempt": 4})
    assert outcome.kind == "dlq"
    assert outcome.headers["x-error-kind"] == "retries exhausted"
    assert outcome.headers["x-attempts"] == 4
    assert outcome.headers["x-consumer"] == "test-consumer"
    assert handler.retries == []
    assert handler.give_ups == [4]
    assert processed.marked == []


def test_retry_disabled_gives_up_immediately():
    handler = FakeHandler(TransientError("x"))
    consumer, _ = make_consumer(handler, retry_enabled=False)
    assert consumer._process(body_of(make_event()), {}).kind == "dlq"
    assert handler.give_ups == [1]


def test_permanent_error_gives_up_immediately():
    handler = FakeHandler(PermanentError("HTTP 417 LinkValidationError"))
    consumer, _ = make_consumer(handler)
    outcome = consumer._process(body_of(make_event()), {})
    assert outcome.kind == "dlq"
    assert outcome.headers["x-error-kind"] == "permanent"
    assert outcome.headers["x-attempts"] == 1
    assert handler.retries == []
    assert handler.give_ups == [1]


def test_give_up_handled_acks_and_marks_processed():
    handler = FakeHandler(PermanentError("x"), give_up="handled")
    consumer, processed = make_consumer(handler)
    event = make_event()
    outcome = consumer._process(body_of(event), {})
    assert outcome.kind == "ack"
    assert processed.marked == [event["eventId"]]


def test_give_up_handled_after_exhausted_retries():
    handler = FakeHandler(TransientError("x"), give_up="handled")
    consumer, _ = make_consumer(handler)
    assert consumer._process(body_of(make_event()), {"x-attempt": 4}).kind == "ack"


def test_give_up_dlq():
    handler = FakeHandler(PermanentError("x"), give_up="dlq")
    consumer, processed = make_consumer(handler)
    assert consumer._process(body_of(make_event()), {}).kind == "dlq"
    assert processed.marked == []


def test_give_up_raising_keeps_message_in_dlq():
    """Compensation impossible (Moodle indisponible pendant la désinscription) : DLQ pour intervention."""
    handler = FakeHandler(PermanentError("x"), give_up_error=TransientError("Moodle timeout"))
    consumer, processed = make_consumer(handler)
    outcome = consumer._process(body_of(make_event()), {})
    assert outcome.kind == "dlq"
    assert "Moodle timeout" in outcome.headers["x-give-up-error"]
    assert processed.marked == []


def test_duplicate_in_flight_acks_without_marking():
    handler = FakeHandler(DuplicateInFlight())
    consumer, processed = make_consumer(handler)
    assert consumer._process(body_of(make_event()), {}).kind == "ack"
    assert processed.marked == []
    assert handler.give_ups == [] and handler.retries == []


@pytest.mark.parametrize("raw", [
    b"not json",
    json.dumps({"eventId": str(uuid.uuid4()), "eventType": "x", "schemaVersion": 99, "payload": {}}).encode(),
])
def test_invalid_message_is_rejected_without_handler(raw):
    handler = FakeHandler()
    consumer, processed = make_consumer(handler)
    assert consumer._process(raw, {}).kind == "reject"
    assert handler.handled == [] and processed.marked == []
