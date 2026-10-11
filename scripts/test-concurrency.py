"""Test 4 : 20 requêtes concurrentes (clés différentes) sur une session de capacité 1.

Par défaut, crée une session neuve SESSION-CONCURRENCY-<run> (capacité 1) pour que le test soit rejouable.
`--session SESSION-CONCURRENCY` utilise la session du catalogue (une seule exécution possible).

Usage : .venv/Scripts/python scripts/test-concurrency.py [--requests 20] [--session ID]
"""

import argparse
import asyncio
import uuid
from collections import Counter

import httpx

from _testlib import API_URL, Checks


async def register(client: httpx.AsyncClient, start: asyncio.Event, session_id: str, i: int, run: str):
    payload = {
        "student": {"email": f"user{i}-{run}@example.org", "firstName": "Test", "lastName": str(i)},
        "company": {"name": "Concurrent Corp"},
        "sessionId": session_id,
    }
    await start.wait()  # toutes les requêtes partent au même instant
    response = await client.post(
        "/api/v1/registrations", json=payload, headers={"Idempotency-Key": str(uuid.uuid4())}
    )
    return response.status_code, response.json()


async def fire(n: int, session_id: str, run: str):
    limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
    async with httpx.AsyncClient(base_url=API_URL, timeout=30, limits=limits) as client:
        start = asyncio.Event()
        tasks = [asyncio.create_task(register(client, start, session_id, i, run)) for i in range(n)]
        await asyncio.sleep(0.2)
        start.set()
        return await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--session", default=None)
    args = parser.parse_args()

    run = uuid.uuid4().hex[:8]
    t = Checks(f"Concurrence — {args.requests} requêtes simultanées, capacité 1")
    client = httpx.Client(base_url=API_URL, timeout=30)

    session_id = args.session
    if session_id is None:
        session_id = f"SESSION-CONCURRENCY-{run.upper()}"
        created = client.post("/api/v1/sessions", json={
            "sessionId": session_id, "capacity": 1,
            "moodleCourseShortname": "CONCURRENCY", "erpItemCode": "FORMATION-CONCURRENCY",
        })
        t.check(f"création de {session_id} (capacité 1) -> 201", created.status_code == 201, created.status_code)

    results = asyncio.run(fire(args.requests, session_id, run))
    statuses = Counter(code for code, _ in results)
    errors = Counter(body.get("error") for code, body in results if code != 202)
    print(f"      répartition : {dict(statuses)}  refus : {dict(errors)}")

    t.check("exactement 1 inscription acceptée (202)", statuses[202] == 1, statuses[202])
    t.check(f"{args.requests - 1} refusées (409 SESSION_FULL)",
            statuses[409] == args.requests - 1 and errors["SESSION_FULL"] == args.requests - 1, dict(errors))
    t.check("aucune erreur 5xx", not any(code >= 500 for code in statuses), dict(statuses))

    session = client.get(f"/api/v1/sessions/{session_id}").json()
    t.check("remainingSeats = 0 (jamais -1)", session["remainingSeats"] == 0, session["remainingSeats"])
    t.check("1 seule inscription active en base", session["activeRegistrations"] == 1, session["activeRegistrations"])

    t.done()


if __name__ == "__main__":
    main()
