"""Classification des réponses Moodle (httpx.MockTransport, aucun réseau)."""

import copy
from urllib.parse import parse_qs

import httpx
import pytest

from shared.errors import PermanentError, TransientError
from shared.moodle import MoodleClient

BASE_URL = "http://moodle.test"


# Construit une seule fois : le vrai constructeur crée un contexte SSL (~0,8 s sous Windows).
_TEMPLATE = MoodleClient(BASE_URL, token="unit-test-token", timeout=1.0, correlation_id="corr-1")
_TEMPLATE.close()


def client_with(handler) -> MoodleClient:
    """Client réel dont le transport HTTP est remplacé par un MockTransport (aucun réseau)."""
    client = copy.copy(_TEMPLATE)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return client


def test_ok_returns_json_and_sends_expected_form():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json=[{"id": 12, "email": "alice@example.org"}])

    client = client_with(handler)
    assert client.find_user_by_email("alice@example.org") == 12
    assert seen["path"] == "/webservice/rest/server.php"
    assert seen["form"]["wsfunction"] == ["core_user_get_users_by_field"]
    assert seen["form"]["moodlewsrestformat"] == ["json"]
    assert seen["form"]["values[0]"] == ["alice@example.org"]


def test_find_user_not_found():
    client = client_with(lambda r: httpx.Response(200, json=[]))
    assert client.find_user_by_email("nobody@example.org") is None


def test_exception_in_http_200_is_permanent():
    body = {"exception": "invalid_parameter_exception", "errorcode": "invalidparameter",
            "message": "Valeur de paramètre incorrecte"}
    client = client_with(lambda r: httpx.Response(200, json=body))
    with pytest.raises(PermanentError, match="invalidparameter"):
        client.call("core_user_create_users")


@pytest.mark.parametrize("code", ["dmlreadexception", "dmlwriteexception", "dbconnectionfailed"])
def test_infrastructure_errorcode_is_transient(code):
    body = {"exception": "dml_read_exception", "errorcode": code, "message": "Error reading from database"}
    client = client_with(lambda r: httpx.Response(200, json=body))
    with pytest.raises(TransientError, match=code):
        client.call("core_enrol_get_users_courses")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_5xx_and_429_are_transient(status):
    client = client_with(lambda r: httpx.Response(status, text="error"))
    with pytest.raises(TransientError, match=str(status)):
        client.call("enrol_manual_enrol_users")


@pytest.mark.parametrize("status", [400, 403, 404])
def test_4xx_is_permanent(status):
    client = client_with(lambda r: httpx.Response(status, text="error"))
    with pytest.raises(PermanentError):
        client.call("enrol_manual_enrol_users")


def test_non_json_200_is_transient():
    client = client_with(lambda r: httpx.Response(200, text="<html>Maintenance</html>"))
    with pytest.raises(TransientError, match="non JSON"):
        client.call("core_course_get_courses_by_field")


def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientError, match="timeout"):
        client_with(handler).call("core_user_get_users_by_field")


def test_connection_error_is_transient():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransientError, match="injoignable"):
        client_with(handler).call("core_user_get_users_by_field")


def test_is_enrolled():
    client = client_with(lambda r: httpx.Response(200, json=[{"id": 3}, {"id": 7}]))
    assert client.is_enrolled(12, 7) is True
    assert client.is_enrolled(12, 8) is False


def test_list_courses():
    client = client_with(lambda r: httpx.Response(200, json={"courses": [
        {"id": 7, "shortname": "PYTHON-ADV", "fullname": "Python avancé"}]}))
    assert client.list_courses() == {"PYTHON-ADV": {"id": 7, "fullname": "Python avancé"}}


def test_unenrol_does_not_send_roleid():
    """enrol_manual_unenrol_users en Moodle 5.3 refuse le paramètre roleid."""
    seen = {}

    def handler(request):
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, text="null")

    client_with(handler).unenrol(12, 7)
    assert seen["form"]["wsfunction"] == ["enrol_manual_unenrol_users"]
    assert not any("roleid" in k for k in seen["form"])
