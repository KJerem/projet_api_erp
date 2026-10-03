"""Validation des requêtes (ces erreurs deviennent des 422 côté API)."""

import pytest
from pydantic import ValidationError

from app.schemas import RegistrationRequest, SessionCreate


def valid() -> dict:
    return {
        "student": {"email": "alice@example.org", "firstName": "Alice", "lastName": "Martin"},
        "company": {"name": "ACME"},
        "sessionId": "SESSION-PYTHON-01",
    }


def errors_of(data: dict) -> list[dict]:
    with pytest.raises(ValidationError) as exc:
        RegistrationRequest.model_validate(data)
    return exc.value.errors()


def test_valid_request():
    req = RegistrationRequest.model_validate(valid())
    assert req.sessionId == "SESSION-PYTHON-01"
    assert req.student.email == "alice@example.org"


def test_email_is_stripped_and_lowercased():
    data = valid()
    data["student"]["email"] = "  Alice.Martin@Example.ORG  "
    data["student"]["firstName"] = "  Alice "
    req = RegistrationRequest.model_validate(data)
    assert req.student.email == "alice.martin@example.org"
    assert req.student.firstName == "Alice"


@pytest.mark.parametrize("email", ["pas-un-email", "alice@", "@example.org", "alice example.org", ""])
def test_invalid_email_rejected(email):
    data = valid()
    data["student"]["email"] = email
    errs = errors_of(data)
    assert any(e["loc"] == ("student", "email") for e in errs)


def test_unknown_top_level_field_forbidden():
    errs = errors_of({**valid(), "price": 0})
    assert any(e["type"] == "extra_forbidden" and e["loc"] == ("price",) for e in errs)


def test_unknown_nested_field_forbidden():
    data = valid()
    data["student"]["isAdmin"] = True
    errs = errors_of(data)
    assert any(e["type"] == "extra_forbidden" and e["loc"] == ("student", "isAdmin") for e in errs)


@pytest.mark.parametrize("session_id", ["session-python-01", "S", "-SESSION", "SESSION 01", "SESSION;DROP", "X" * 101])
def test_session_id_bad_format(session_id):
    errs = errors_of({**valid(), "sessionId": session_id})
    assert any(e["loc"] == ("sessionId",) and e["type"] == "string_pattern_mismatch" for e in errs)


def test_session_id_is_stripped_before_pattern_check():
    req = RegistrationRequest.model_validate({**valid(), "sessionId": "  SESSION-PYTHON-01 "})
    assert req.sessionId == "SESSION-PYTHON-01"


@pytest.mark.parametrize("missing", ["student", "company", "sessionId"])
def test_missing_field(missing):
    data = valid()
    del data[missing]
    errs = errors_of(data)
    assert any(e["type"] == "missing" and e["loc"] == (missing,) for e in errs)


def test_blank_names_rejected_after_strip():
    data = valid()
    data["student"]["firstName"] = "   "
    errs = errors_of(data)
    assert any(e["loc"] == ("student", "firstName") for e in errs)


@pytest.mark.parametrize("capacity", [0, -1, 10_001])
def test_session_capacity_bounds(capacity):
    with pytest.raises(ValidationError):
        SessionCreate.model_validate({"sessionId": "SESSION-X-01", "capacity": capacity,
                                      "moodleCourseShortname": "X", "erpItemCode": "ITEM"})
