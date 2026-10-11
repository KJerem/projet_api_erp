"""Tests 2 et 3 : Idempotency-Key répétée / réutilisée avec un autre body.

Usage : .venv/Scripts/python scripts/test-idempotency.py
"""

import asyncio
import uuid

import httpx

from _testlib import API_URL, Checks

SESSION = "SESSION-PYTHON-01"


def body(email: str, last_name: str = "Martin") -> dict:
    return {
        "student": {"email": email, "firstName": "Alice", "lastName": last_name},
        "company": {"name": "ACME"},
        "sessionId": SESSION,
    }


def remaining(client: httpx.Client) -> int:
    return client.get(f"/api/v1/sessions/{SESSION}").json()["remainingSeats"]


async def concurrent_same_key(n: int, key: str, payload: dict) -> list[httpx.Response]:
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        return await asyncio.gather(
            *(client.post("/api/v1/registrations", json=payload, headers={"Idempotency-Key": key}) for _ in range(n))
        )


def main() -> None:
    t = Checks("Idempotence HTTP — POST /api/v1/registrations")
    run = uuid.uuid4().hex[:8]
    client = httpx.Client(base_url=API_URL, timeout=30)

    # --- Test 2 : même clé + même payload ---------------------------------------------------
    key = f"idem-{run}"
    payload = body(f"alice-{run}@example.org")
    seats_before = remaining(client)

    first = client.post("/api/v1/registrations", json=payload, headers={"Idempotency-Key": key})
    t.check("1er appel -> 202 Accepted", first.status_code == 202, first.json())
    registration_id = first.json().get("registrationId")

    second = client.post("/api/v1/registrations", json=payload, headers={"Idempotency-Key": key})
    t.check("2e appel (même clé, même body) -> même statut 202", second.status_code == 202, second.status_code)
    t.check("2e appel -> même registrationId", second.json().get("registrationId") == registration_id, second.json())
    t.check("2e appel -> en-tête Idempotent-Replayed: true", second.headers.get("Idempotent-Replayed") == "true")

    # Body sémantiquement identique mais présenté autrement (ordre des clés, casse, espaces).
    reordered = {
        "sessionId": SESSION,
        "company": {"name": " ACME "},
        "student": {"lastName": "Martin", "firstName": "Alice", "email": f"ALICE-{run}@Example.org"},
    }
    third = client.post("/api/v1/registrations", json=reordered, headers={"Idempotency-Key": key})
    t.check("3e appel (body normalisé identique) -> rejeu", third.json().get("registrationId") == registration_id,
            third.status_code)

    t.check("une seule place consommée", seats_before - remaining(client) == 1,
            f"avant={seats_before} après={remaining(client)}")

    # --- Test 3 : même clé + payload différent ---------------------------------------------
    conflict = client.post(
        "/api/v1/registrations", json=body(f"alice-{run}@example.org", last_name="Dupont"),
        headers={"Idempotency-Key": key},
    )
    t.check("même clé + body différent -> 409", conflict.status_code == 409, conflict.status_code)
    t.check("code IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
            conflict.json().get("error") == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD", conflict.json())

    # --- Même clé envoyée 10 fois EN PARALLÈLE ----------------------------------------------
    key2 = f"idem-par-{run}"
    seats_before = remaining(client)
    responses = asyncio.run(concurrent_same_key(10, key2, body(f"bob-{run}@example.org")))
    ids = {r.json().get("registrationId") for r in responses}
    t.check("10 requêtes simultanées même clé -> toutes 202", all(r.status_code == 202 for r in responses),
            [r.status_code for r in responses])
    t.check("10 requêtes simultanées -> un seul registrationId", len(ids) == 1, ids)
    t.check("10 requêtes simultanées -> une seule place consommée", seats_before - remaining(client) == 1)

    # --- En-tête absent ------------------------------------------------------------------------
    missing = client.post("/api/v1/registrations", json=body(f"carol-{run}@example.org"))
    t.check("sans Idempotency-Key -> 400", missing.status_code == 400, missing.json())

    t.done()


if __name__ == "__main__":
    main()
