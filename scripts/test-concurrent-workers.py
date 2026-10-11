"""Plusieurs workers concurrents : pas de doublon ERPNext même si ERPNext n'impose aucune unicité.

ERPNext accepte deux Customer de même nom (il suffixe le second : « ACME - 1 »). Le find-or-create
est donc sérialisé par un verrou consultatif PostgreSQL partagé par tous les workers.
6 apprenants d'une même NOUVELLE entreprise, traités par 3 workers -> 1 seul Customer.

Usage : .venv/Scripts/python scripts/test-concurrent-workers.py
"""

import asyncio
import uuid

import httpx

from _testlib import API_URL, Checks, Externals, compose, wait_status

N_STUDENTS, N_WORKERS = 6, 3


async def post_all(company: str, run: str) -> list[str]:
    async with httpx.AsyncClient(base_url=API_URL, timeout=30) as client:
        responses = await asyncio.gather(*(client.post(
            "/api/v1/registrations", headers={"Idempotency-Key": f"cw-{run}-{i}"},
            json={"student": {"email": f"cw{i}-{run}@example.org", "firstName": "Worker", "lastName": str(i)},
                  "company": {"name": company}, "sessionId": "SESSION-PYTHON-01"})
            for i in range(N_STUDENTS)))
    return [r.json()["registrationId"] for r in responses]


def main() -> None:
    t = Checks(f"{N_WORKERS} workers concurrents, {N_STUDENTS} apprenants d'une même nouvelle entreprise")
    run = uuid.uuid4().hex[:8]
    company = f"PARALLEL CORP {run.upper()}"
    api = httpx.Client(base_url=API_URL, timeout=15)
    ext = Externals()

    compose("up", "-d", "--scale", f"registration-worker={N_WORKERS}", "--no-recreate", "registration-worker")
    ids = asyncio.run(post_all(company, run))
    statuses = [wait_status(api, rid, {"CONFIRMED", "COMPENSATED", "FAILED"})["status"] for rid in ids]
    t.check(f"{N_STUDENTS} inscriptions CONFIRMED", statuses.count("CONFIRMED") == N_STUDENTS, statuses)

    customers = ext.erp_list("Customer", [["customer_name", "like", f"{company}%"]])
    t.check("ERPNext : exactement 1 Customer (aucun « - 1 »)", len(customers) == 1, [c["name"] for c in customers])
    orders = ext.erp_list("Sales Order", [["customer", "=", company]])
    t.check(f"ERPNext : {N_STUDENTS} Sales Orders rattachés à ce Customer", len(orders) == N_STUDENTS, len(orders))

    logs = compose("logs", "--since", "3m", "registration-worker")
    used = {line.split("|", 1)[0].strip() for line in logs.splitlines() if "inscription confirmée" in line
            and any(rid in line for rid in ids)}
    t.check("travail réparti sur plusieurs workers", len(used) >= 2, sorted(used))

    compose("up", "-d", "--scale", "registration-worker=1", "registration-worker")
    t.done()


if __name__ == "__main__":
    main()
