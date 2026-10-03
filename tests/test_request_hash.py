"""Empreinte du body utilisée pour détecter la réutilisation d'une Idempotency-Key avec un autre contenu."""

from app.registrations import request_hash
from app.schemas import RegistrationRequest

BASE = {
    "student": {"email": "alice@example.org", "firstName": "Alice", "lastName": "Martin"},
    "company": {"name": "ACME"},
    "sessionId": "SESSION-PYTHON-01",
}


def h(data: dict) -> str:
    return request_hash(RegistrationRequest.model_validate(data))


def test_hash_is_sha256_hex():
    value = h(BASE)
    assert len(value) == 64
    int(value, 16)


def test_key_order_does_not_change_hash():
    reordered = {
        "sessionId": "SESSION-PYTHON-01",
        "company": {"name": "ACME"},
        "student": {"lastName": "Martin", "firstName": "Alice", "email": "alice@example.org"},
    }
    assert h(reordered) == h(BASE)


def test_email_case_does_not_change_hash():
    data = {**BASE, "student": {**BASE["student"], "email": "Alice@EXAMPLE.org"}}
    assert h(data) == h(BASE)


def test_surrounding_spaces_do_not_change_hash():
    data = {
        "student": {"email": "  alice@example.org ", "firstName": " Alice", "lastName": "Martin  "},
        "company": {"name": " ACME "},
        "sessionId": " SESSION-PYTHON-01 ",
    }
    assert h(data) == h(BASE)


def test_different_content_gives_different_hash():
    assert h({**BASE, "sessionId": "SESSION-DOCKER-01"}) != h(BASE)
    assert h({**BASE, "company": {"name": "Globex"}}) != h(BASE)
    assert h({**BASE, "student": {**BASE["student"], "firstName": "Alicia"}}) != h(BASE)
