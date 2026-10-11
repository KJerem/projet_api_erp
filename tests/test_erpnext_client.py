"""Classification des réponses ERPNext/Frappe et messages d'erreur lisibles (httpx.MockTransport)."""

import copy
import json

import httpx
import pytest

from shared.erpnext import ErpNextClient
from shared.errors import PermanentError, TransientError

BASE_URL = "http://erpnext.test"


# Construit une seule fois : le vrai constructeur crée un contexte SSL (~0,8 s sous Windows).
_TEMPLATE = ErpNextClient(BASE_URL, auth_header="token fake:fake", timeout=1.0, company="OpenTraining")
_TEMPLATE.close()


def client_with(handler) -> ErpNextClient:
    """Client réel dont le transport HTTP est remplacé par un MockTransport (aucun réseau)."""
    client = copy.copy(_TEMPLATE)
    client._http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    return client


def frappe_error(status: int, exc_type: str, message: str) -> httpx.Response:
    server_messages = json.dumps([json.dumps({"message": message})])
    return httpx.Response(status, json={"exc_type": exc_type, "_server_messages": server_messages})


def test_ok_find_lead():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["filters"] = json.loads(request.url.params["filters"])
        return httpx.Response(200, json={"data": [{"name": "CRM-LEAD-2026-00001"}]})

    assert client_with(handler).find_lead("alice@example.org") == "CRM-LEAD-2026-00001"
    assert seen["path"] == "/api/resource/Lead"
    assert seen["filters"] == [["email_id", "=", "alice@example.org"]]


def test_find_sales_order_none():
    client = client_with(lambda r: httpx.Response(200, json={"data": []}))
    assert client.find_sales_order("reg-1") is None


def test_create_sales_order_carries_registration_id():
    seen = {}

    def handler(request):
        seen["doc"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"name": "SAL-ORD-2026-00001", "docstatus": 0}})

    result = client_with(handler).create_sales_order("ACME", "FORMATION-PYTHON", "reg-1")
    assert result == {"name": "SAL-ORD-2026-00001", "docstatus": 0}
    assert seen["doc"]["po_no"] == "reg-1"
    assert seen["doc"]["company"] == "OpenTraining"
    assert seen["doc"]["items"][0]["item_code"] == "FORMATION-PYTHON"


def test_get_item_prices():
    client = client_with(lambda r: httpx.Response(200, json={"data": [
        {"item_code": "FORMATION-PYTHON", "price_list_rate": "850.0", "currency": "EUR"}]}))
    assert client.get_item_prices(["FORMATION-PYTHON"]) == {"FORMATION-PYTHON": {"price": 850.0, "currency": "EUR"}}
    assert client.get_item_prices([]) == {}


def test_4xx_is_permanent_with_readable_message():
    resp = frappe_error(417, "LinkValidationError", "Could not find <strong>Item: NOPE</strong>")
    with pytest.raises(PermanentError) as exc:
        client_with(lambda r: resp).create_lead("a@example.org", "A", "B", "ACME")
    message = str(exc.value)
    assert "HTTP 417" in message
    assert "LinkValidationError" in message
    assert "Could not find Item: NOPE" in message
    assert "<strong>" not in message


@pytest.mark.parametrize("status", [400, 403, 404, 409, 417])
def test_4xx_statuses_are_permanent(status):
    with pytest.raises(PermanentError):
        client_with(lambda r: httpx.Response(status, json={"exc_type": "ValidationError"})).find_lead("x@y.z")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_5xx_and_429_are_transient(status):
    with pytest.raises(TransientError, match=str(status)):
        client_with(lambda r: httpx.Response(status, json={})).find_customer("ACME")


def test_html_502_page_is_transient_with_plain_text():
    html = "<html>\n<head><title>502 Bad Gateway</title></head>\n<body><center><h1>502 Bad Gateway</h1></center>" \
           "<hr><center>nginx</center></body></html>"
    with pytest.raises(TransientError) as exc:
        client_with(lambda r: httpx.Response(502, text=html)).find_customer("ACME")
    message = str(exc.value)
    assert "HTTP 502" in message
    assert "502 Bad Gateway" in message and "nginx" in message
    assert "<" not in message.split(" : ", 1)[1]


def test_timeout_is_transient():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TransientError, match="timeout"):
        client_with(handler).find_lead("a@example.org")


def test_connection_error_is_transient():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(TransientError, match="injoignable"):
        client_with(handler).find_lead("a@example.org")
