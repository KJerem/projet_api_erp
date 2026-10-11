"""Test 5 : message RabbitMQ dupliqué (même eventId) -> un seul effet métier.

A. Événement déjà traité, republié 2 fois : ACK sans nouvel effet (processed_events).
B. Événement neuf publié 3 fois d'un coup, consommé par 2 workers concurrents :
   une seule exécution de la saga (verrou par inscription + déduplication).

Usage : .venv/Scripts/python scripts/test-duplicate-message.py
"""

import time
import uuid

import httpx

from _testlib import (API_URL, Checks, Externals, compose, outbox_event, publish_raw, sql, wait_for,
                      wait_status)

FINAL = {"CONFIRMED", "COMPENSATED", "FAILED"}


def register(api: httpx.Client, tag: str) -> tuple[str, str]:
    email = f"dup-{tag}@example.org"
    resp = api.post("/api/v1/registrations", headers={"Idempotency-Key": f"dup-{tag}"}, json={
        "student": {"email": email, "firstName": "Dup", "lastName": "Licate"},
        "company": {"name": f"DUP CORP {tag.upper()}"}, "sessionId": "SESSION-PYTHON-01"})
    resp.raise_for_status()
    return resp.json()["registrationId"], email


def effects(ext: Externals, reg_id: str, email: str) -> dict:
    users = ext.moodle_users(email)
    return {
        "moodleUsers": len(users),
        "leads": len(ext.erp_list("Lead", [["email_id", "=", email]])),
        "salesOrders": len(ext.erp_list("Sales Order", [["po_no", "=", reg_id]])),
        "confirmedEvents": int(sql(f"SELECT count(*) FROM outbox_event WHERE aggregate_id = '{reg_id}' "
                                   "AND event_type = 'registration.confirmed'")),
        "history": int(sql(f"SELECT count(*) FROM registration_status_history WHERE registration_id = '{reg_id}'")),
    }


def main() -> None:
    t = Checks("Message RabbitMQ dupliqué — un seul effet métier")
    api = httpx.Client(base_url=API_URL, timeout=15)
    ext = Externals()
    run = uuid.uuid4().hex[:8]

    # --- A. Événement déjà traité ---------------------------------------------------------------
    print("-- A. republication d'un événement déjà traité")
    reg_a, email_a = register(api, f"a-{run}")
    t.check("inscription confirmée", wait_status(api, reg_a, FINAL)["status"] == "CONFIRMED")
    before = effects(ext, reg_a, email_a)
    event = outbox_event(reg_a)
    event_id = event.split('"eventId": "')[1].split('"')[0]
    for _ in range(2):
        publish_raw("registration.accepted", event.encode(), message_id=event_id)
    t.check("les 2 doublons sont acquittés (logs « déjà traité »)", wait_for(lambda: compose(
        "logs", "--since", "1m", "registration-worker").count(f'"eventId": "{event_id}"') >= 2
        and "déjà traité" in compose("logs", "--since", "1m", "registration-worker"), timeout=20))
    time.sleep(2)
    after = effects(ext, reg_a, email_a)
    t.check("aucun effet supplémentaire (Moodle, ERPNext, outbox, historique)", before == after,
            f"avant={before} après={after}")

    # --- B. Événement neuf dupliqué + 2 workers concurrents ---------------------------------------
    print("\n-- B. événement neuf publié 3 fois, 2 workers en parallèle")
    compose("up", "-d", "--scale", "registration-worker=2", "--no-recreate", "registration-worker")
    compose("stop", "outbox-dispatcher")
    reg_b, email_b = register(api, f"b-{run}")
    event = outbox_event(reg_b)
    event_id = event.split('"eventId": "')[1].split('"')[0]
    for _ in range(3):
        publish_raw("registration.accepted", event.encode(), message_id=event_id)
    view = wait_status(api, reg_b, FINAL)
    t.check("inscription confirmée malgré les doublons", view["status"] == "CONFIRMED", view.get("lastError"))
    compose("start", "outbox-dispatcher")  # le dispatcher publie l'original : 4e livraison du même eventId
    t.check("la publication par l'outbox est aussi dédupliquée",
            wait_for(lambda: sql(f"SELECT published_at IS NOT NULL FROM outbox_event WHERE event_id = '{event_id}'")
                     == "t", timeout=20))
    time.sleep(4)
    result = effects(ext, reg_b, email_b)
    t.check("1 seul utilisateur Moodle", result["moodleUsers"] == 1, result)
    t.check("1 seul Lead et 1 seul Sales Order", result["leads"] == 1 and result["salesOrders"] == 1, result)
    t.check("1 seul événement registration.confirmed", result["confirmedEvents"] == 1, result)
    history = sql(f"SELECT string_agg(status, ',' ORDER BY id) FROM registration_status_history "
                  f"WHERE registration_id = '{reg_b}'")
    t.check("une seule exécution de la saga dans l'historique",
            history == "ACCEPTED,PROCESSING,MOODLE_ENROLLED,ERP_RECORDED,CONFIRMED", history)
    logs = compose("logs", "--since", "2m", "registration-worker")
    workers = {line.split("|", 1)[0].strip() for line in logs.splitlines() if event_id in line}
    print(f"      workers ayant reçu l'événement : {sorted(workers)}")
    t.check("doublons écartés (déjà traité / en cours ailleurs)",
            "déjà traité" in logs or "en cours de traitement par un autre worker" in logs)

    compose("up", "-d", "--scale", "registration-worker=1", "registration-worker")
    t.done()


if __name__ == "__main__":
    main()
