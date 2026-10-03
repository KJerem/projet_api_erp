"""Test 8 : DLQ — messages non traitables et retries épuisés.

A. Message JSON invalide              -> rejeté (basic.reject) -> DLX -> registration.dlq
B. Événement d'une inscription inconnue -> erreur permanente -> DLQ sans retry
C. ERPNext arrêté (HTTP 502) durablement -> 4 tentatives (0, +2 s, +4 s, +8 s) -> FAILED,
   place libérée, événement registration.failed, message en DLQ avec la cause.

Usage : .venv/Scripts/python scripts/test-dlq.py
"""

import json
import uuid
from datetime import UTC, datetime

import httpx

from _testlib import (API_URL, FINAL_STATUSES, Checks, compose, dlq_find, publish_raw, register, seats, sql,
                      wait_for, wait_status)


def main() -> None:
    t = Checks("DLQ — messages non traitables et retries épuisés")
    api = httpx.Client(base_url=API_URL, timeout=15)
    run = uuid.uuid4().hex[:8]

    # --- A. message invalide -------------------------------------------------------------------
    print("-- A. message JSON invalide")
    marker = f"poison-{run}"
    publish_raw("registration.accepted", f"{{ceci n'est pas du JSON {marker}".encode())
    msg = wait_for(lambda: dlq_find(marker) is not None, timeout=20) and dlq_find(marker)
    t.check("message invalide en DLQ", bool(msg))
    t.check("motif : rejet (x-death reason=rejected)",
            bool(msg) and (msg["headers"].get("x-death") or [{}])[0].get("reason") == "rejected",
            bool(msg) and (msg["headers"].get("x-death") or [{}])[0].get("reason"))

    # --- B. inscription inconnue ---------------------------------------------------------------
    print("\n-- B. événement valide mais inscription inconnue")
    ghost = str(uuid.uuid4())
    publish_raw("registration.accepted", json.dumps({
        "eventId": str(uuid.uuid4()), "eventType": "registration.accepted", "schemaVersion": 1,
        "correlationId": f"corr-ghost-{run}", "occurredAt": datetime.now(UTC).isoformat(),
        "payload": {"registrationId": ghost, "sessionId": "SESSION-PYTHON-01"}}).encode())
    msg = wait_for(lambda: dlq_find(ghost) is not None, timeout=20) and dlq_find(ghost)
    t.check("événement orphelin en DLQ", bool(msg))
    t.check("classé permanent, sans retry (x-attempts = 1)",
            bool(msg) and msg["headers"].get("x-error-kind") == "permanent" and msg["headers"].get("x-attempts") == 1,
            bool(msg) and {k: msg["headers"].get(k) for k in ("x-error-kind", "x-attempts", "x-error")})

    # --- C. retries épuisés --------------------------------------------------------------------
    print("\n-- C. ERPNext arrêté durablement (HTTP 502)")
    seats_before = seats(api, "SESSION-PYTHON-01")
    compose("stop", "-t", "1", "erpnext-backend")
    try:
        reg_id, _ = register(api, f"dlq-exhausted-{run}")
        view = wait_status(api, reg_id, FINAL_STATUSES, timeout=90)
    finally:
        compose("start", "erpnext-backend")
    history = [h["status"] for h in view["history"]]
    print(f"      historique : {' -> '.join(history)}")
    t.check("statut FAILED après épuisement des retries", view["status"] == "FAILED", view["status"])
    t.check("4 tentatives (1 + 3 retries)", view["attempts"] == 4 and history.count("RETRYING") == 3,
            f"attempts={view['attempts']} retries={history.count('RETRYING')}")
    t.check("erreur 502 visible", "HTTP 502" in (view["lastError"] or ""), view["lastError"])
    t.check("place libérée", seats(api, "SESSION-PYTHON-01") == seats_before, seats(api, "SESSION-PYTHON-01"))
    t.check("événement registration.failed émis (outbox)", sql(
        f"SELECT count(*) FROM outbox_event WHERE aggregate_id = '{reg_id}' "
        "AND event_type = 'registration.failed'") == "1")
    msg = wait_for(lambda: dlq_find(reg_id) is not None, timeout=20) and dlq_find(reg_id)
    t.check("message en DLQ avec motif « retries exhausted » et 4 tentatives",
            bool(msg) and msg["headers"].get("x-error-kind") == "retries exhausted"
            and msg["headers"].get("x-attempts") == 4,
            bool(msg) and {k: msg["headers"].get(k) for k in ("x-error-kind", "x-attempts")})
    t.done()


if __name__ == "__main__":
    main()
