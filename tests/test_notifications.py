"""Rendu des emails du notification worker."""

import pytest

from shared.errors import PermanentError
from workers.notifications.main import render


def event(event_type: str, **payload) -> dict:
    base = {"registrationId": "reg-42", "sessionId": "SESSION-PYTHON-01",
            "student": {"email": "alice@example.org", "firstName": "Alice", "lastName": "Martin"}}
    return {"eventId": "e-1", "eventType": event_type, "payload": {**base, **payload}}


def test_confirmed():
    subject, body = render(event("registration.confirmed", courseShortname="PYTHON-ADV", courseId=7,
                                 erpSalesOrderId="SAL-ORD-2026-00001"))
    assert subject == "Inscription confirmée — SESSION-PYTHON-01"
    assert body.startswith("Bonjour Alice,")
    for expected in ("reg-42", "PYTHON-ADV", "7", "SAL-ORD-2026-00001"):
        assert expected in body


def test_compensated():
    subject, body = render(event("registration.compensated"))
    assert subject == "Inscription annulée — SESSION-PYTHON-01"
    assert "annulée" in body and "reg-42" in body


def test_failed():
    subject, body = render(event("registration.failed"))
    assert subject == "Inscription non aboutie — SESSION-PYTHON-01"
    assert "reg-42" in body


def test_notification_requested_uses_payload_subject_and_body():
    assert render({"eventType": "notification.requested",
                   "payload": {"subject": "Rappel", "body": "Texte"}}) == ("Rappel", "Texte")


def test_greeting_without_first_name():
    _, body = render({"eventType": "registration.failed", "payload": {"registrationId": "r", "sessionId": "S"}})
    assert body.startswith("Bonjour,")


def test_unknown_type_is_permanent_error():
    with pytest.raises(PermanentError):
        render(event("registration.accepted"))
