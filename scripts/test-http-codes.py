"""Contrat HTTP de l'API : 200, 201, 202, 400, 404, 409, 422, 503 (+ correlation ID).

Usage : .venv/Scripts/python scripts/test-http-codes.py
"""

import uuid

import httpx

from _testlib import API_URL, Checks


def main() -> None:
    t = Checks("Codes HTTP et contrat de l'API")
    run = uuid.uuid4().hex[:8].upper()
    c = httpx.Client(base_url=API_URL, timeout=15)

    def post_reg(payload: dict, key: str | None = None) -> httpx.Response:
        return c.post("/api/v1/registrations", json=payload, headers={"Idempotency-Key": key or str(uuid.uuid4())})

    valid = {
        "student": {"email": f"codes-{run.lower()}@example.org", "firstName": "Code", "lastName": "Test"},
        "company": {"name": "ACME"},
        "sessionId": "SESSION-PYTHON-01",
    }

    r = c.get("/health")
    t.check("GET /health -> 200 {status: UP}", r.status_code == 200 and r.json() == {"status": "UP"}, r.json())
    r = c.get("/ready")
    t.check("GET /ready -> 200 (PostgreSQL + RabbitMQ)", r.status_code == 200, r.json())

    session_id = f"SESSION-CODES-{run}"
    new_session = {"sessionId": session_id, "capacity": 2, "moodleCourseShortname": "PYTHON-ADV",
                   "erpItemCode": "FORMATION-PYTHON"}
    r = c.post("/api/v1/sessions", json=new_session)
    t.check("POST /sessions -> 201 Created + Location", r.status_code == 201 and "location" in r.headers, r.status_code)
    r = c.post("/api/v1/sessions", json=new_session)
    t.check("POST /sessions (doublon) -> 409", r.status_code == 409, r.json().get("error"))

    r = post_reg({**valid, "sessionId": session_id})
    t.check("POST /registrations -> 202 Accepted + Location", r.status_code == 202 and "location" in r.headers,
            r.json())
    reg_id = r.json()["registrationId"]

    r = post_reg({**valid, "sessionId": session_id})
    t.check("même apprenant, autre clé -> 409 ALREADY_REGISTERED",
            r.status_code == 409 and r.json().get("error") == "ALREADY_REGISTERED", r.json().get("error"))

    r = c.get(f"/api/v1/registrations/{reg_id}", headers={"X-Correlation-ID": "corr-codes-test"})
    t.check("GET /registrations/{id} -> 200", r.status_code == 200, r.json().get("status"))
    t.check("X-Correlation-ID renvoyé tel quel", r.headers.get("x-correlation-id") == "corr-codes-test")
    t.check("correlation ID généré si absent", c.get("/health").headers.get("x-correlation-id", "").startswith("corr-"))

    r = c.get(f"/api/v1/registrations/{uuid.uuid4()}")
    t.check("GET inscription inconnue -> 404", r.status_code == 404, r.json().get("error"))
    r = post_reg({**valid, "sessionId": "SESSION-INEXISTANTE"})
    t.check("POST sur session inconnue -> 404", r.status_code == 404, r.json().get("error"))

    r = c.post("/api/v1/registrations", json=valid)
    t.check("POST sans Idempotency-Key -> 400", r.status_code == 400, r.json().get("error"))

    r = post_reg({**valid, "student": {**valid["student"], "email": "pas-un-email"}})
    t.check("email invalide -> 422", r.status_code == 422, r.json().get("error"))
    r = post_reg({"student": valid["student"], "sessionId": "SESSION-PYTHON-01"})
    t.check("champ company manquant -> 422", r.status_code == 422, r.json().get("error"))
    r = post_reg({**valid, "unexpected": True})
    t.check("champ inconnu -> 422 (extra=forbid)", r.status_code == 422, r.json().get("error"))
    r = c.get("/api/v1/registrations/pas-un-uuid")
    t.check("id non UUID -> 422", r.status_code == 422, r.json().get("error"))

    print("      (503 : couvert par scripts/test-db-down.sh — base d'intégration arrêtée)")
    t.done()


if __name__ == "__main__":
    main()
