"""Format d'erreur commun {"error", "message"} pour les erreurs levées par FastAPI/Starlette elles-mêmes.

Ces erreurs surviennent avant toute route (lecture du body, routage) : aucun accès base n'est nécessaire.
Le TestClient est utilisé sans bloc `with`, donc sans lancer le lifespan (pas de pool PostgreSQL).
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

VALID_BODY_LATIN1 = (
    '{"student":{"email":"chloe@example.org","firstName":"Chloé","lastName":"Durand"},'
    '"company":{"name":"Globex"},"sessionId":"SESSION-PYTHON-01"}'
).encode("latin-1")


def test_body_non_utf8_returns_400_malformed_body():
    resp = client.post("/api/v1/registrations", content=VALID_BODY_LATIN1,
                       headers={"Content-Type": "application/json", "Idempotency-Key": "k-latin1"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "MALFORMED_BODY"
    assert "UTF-8" in body["message"]
    assert "detail" not in body


def test_unknown_route_returns_404_not_found():
    resp = client.get("/api/v1/inexistant")
    assert resp.status_code == 404
    assert resp.json() == {"error": "NOT_FOUND", "message": "Ressource inconnue : GET /api/v1/inexistant"}


def test_method_not_allowed_returns_405_and_keeps_allow_header():
    resp = client.delete("/api/v1/sessions")
    assert resp.status_code == 405
    assert resp.json()["error"] == "METHOD_NOT_ALLOWED"
    # L'en-tête Allow posé par Starlette est conservé par notre handler (il liste les méthodes
    # de la première route correspondant au chemin).
    assert resp.headers.get("allow")


@pytest.mark.parametrize("path", ["/api/v1/inexistant", "/api/v1/sessions"])
def test_framework_errors_carry_correlation_id_header(path):
    resp = client.request("PATCH", path, headers={"X-Correlation-ID": "corr-unit-test"})
    assert resp.headers["x-correlation-id"] == "corr-unit-test"


def test_malformed_json_stays_422_validation_error():
    """Un JSON syntaxiquement invalide (mais bien encodé) reste une erreur de validation 422."""
    resp = client.post("/api/v1/registrations", content=b'{"student": ',
                       headers={"Content-Type": "application/json", "Idempotency-Key": "k-bad-json"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "VALIDATION_ERROR"
