"""Test 10 : Outbox — l'événement survit à l'arrêt du dispatcher et à l'arrêt de RabbitMQ.

Scénario A : dispatcher arrêté -> POST 202, événement en outbox (published_at NULL), rien dans RabbitMQ ;
             redémarrage -> événement publié et présent dans les queues.
Scénario B : RabbitMQ arrêté -> l'API accepte toujours (202), l'événement attend dans l'outbox ;
             redémarrage de RabbitMQ -> le dispatcher se reconnecte et publie.

Usage : .venv/Scripts/python scripts/test-outbox.py
"""

import uuid
from collections import Counter

import httpx

from _testlib import API_URL, Checks, compose, rabbit_queue, sql, wait_for

QUEUE = "audit"  # reçoit tous les événements (binding "#") ; pas encore de consumer en Phase 2


def post_registration(client: httpx.Client, tag: str) -> str:
    resp = client.post(
        "/api/v1/registrations",
        headers={"Idempotency-Key": f"outbox-{tag}", "X-Correlation-ID": f"corr-outbox-{tag}"},
        json={
            "student": {"email": f"outbox-{tag}@example.org", "firstName": "Out", "lastName": "Box"},
            "company": {"name": "ACME"},
            "sessionId": "SESSION-PYTHON-01",
        },
    )
    resp.raise_for_status()
    return resp.json()["registrationId"]


def outbox_state(registration_id: str) -> str:
    """'pending' | 'published' | '' pour l'événement registration.accepted de l'inscription."""
    return sql(
        "SELECT CASE WHEN published_at IS NULL THEN 'pending' ELSE 'published' END FROM outbox_event "
        f"WHERE aggregate_id = '{registration_id}' AND event_type = 'registration.accepted'"
    )


def queue_has_message_for(registration_id: str) -> bool:
    """Lit (sans consommer : requeue) les messages de la queue d'audit et cherche l'inscription."""
    from _testlib import ENV
    port = ENV.get("RABBITMQ_MGMT_PORT", "15672")
    resp = httpx.post(
        f"http://localhost:{port}/api/queues/%2F/{QUEUE}/get",
        auth=(ENV.get("RABBITMQ_USER", "opentraining"), ENV["RABBITMQ_PASSWORD"]),
        json={"count": 1000, "ackmode": "ack_requeue_true", "encoding": "auto"},
        timeout=15,
    )
    resp.raise_for_status()
    return any(registration_id in m["payload"] for m in resp.json())


def main() -> None:
    t = Checks("Outbox — publication garantie malgré les pannes")
    client = httpx.Client(base_url=API_URL, timeout=15)
    run = uuid.uuid4().hex[:8]

    # Démarrage propre : le dispatcher a vidé l'outbox.
    compose("up", "-d", "outbox-dispatcher")
    t.check("outbox initialement vide", wait_for(lambda: sql(
        "SELECT count(*) FROM outbox_event WHERE published_at IS NULL") == "0", timeout=30))

    # --- Scénario A : dispatcher arrêté -----------------------------------------------------------
    print("\n-- A. dispatcher arrêté")
    compose("stop", "outbox-dispatcher")
    reg_a = post_registration(client, f"a-{run}")
    t.check("POST accepté (202) pendant l'arrêt du dispatcher", bool(reg_a), reg_a)
    t.check("événement dans l'outbox, published_at IS NULL", outbox_state(reg_a) == "pending", outbox_state(reg_a))
    pending = client.get("/ready").json()["pendingOutboxEvents"]
    t.check("/ready signale l'événement en attente", pending >= 1, pending)
    t.check("aucun message dans RabbitMQ pour cette inscription", not queue_has_message_for(reg_a))

    compose("start", "outbox-dispatcher")
    t.check("après redémarrage : événement marqué publié",
            wait_for(lambda: outbox_state(reg_a) == "published", timeout=60), outbox_state(reg_a))
    t.check("message présent dans la queue RabbitMQ", wait_for(lambda: queue_has_message_for(reg_a), timeout=15))

    # --- Scénario B : RabbitMQ arrêté --------------------------------------------------------------
    print("\n-- B. RabbitMQ arrêté")
    compose("stop", "rabbitmq")
    reg_b = post_registration(client, f"b-{run}")
    t.check("POST toujours accepté (202) : l'API ne dépend pas du broker", bool(reg_b), reg_b)
    ready = client.get("/ready")
    t.check("/ready -> 503 (rabbitmq DOWN)", ready.status_code == 503, ready.json().get("checks"))
    t.check("événement conservé dans l'outbox", outbox_state(reg_b) == "pending", outbox_state(reg_b))

    compose("start", "rabbitmq")
    t.check("RabbitMQ redémarré", wait_for(lambda: rabbit_queue(QUEUE) is not None, timeout=90))
    t.check("dispatcher reconnecté : événement publié",
            wait_for(lambda: outbox_state(reg_b) == "published", timeout=90), outbox_state(reg_b))
    t.check("message présent dans la queue RabbitMQ", wait_for(lambda: queue_has_message_for(reg_b), timeout=15))

    # --- Scénario C : 3 dispatchers concurrents (FOR UPDATE SKIP LOCKED) ---------------------------
    print("\n-- C. 3 dispatchers en parallèle, 100 inscriptions")
    n = 100
    session_id = f"SESSION-OUTBOX-{run.upper()}"
    client.post("/api/v1/sessions", json={"sessionId": session_id, "capacity": n,
                                         "moodleCourseShortname": "PYTHON-ADV", "erpItemCode": "FORMATION-PYTHON"})
    compose("up", "-d", "--scale", "outbox-dispatcher=3", "--no-recreate", "outbox-dispatcher")
    before = rabbit_queue("registration.orchestrator")["messages"]
    for i in range(n):
        client.post("/api/v1/registrations", headers={"Idempotency-Key": f"outbox-c-{run}-{i}"}, json={
            "student": {"email": f"outbox-c{i}-{run}@example.org", "firstName": "Multi", "lastName": str(i)},
            "company": {"name": "ACME"}, "sessionId": session_id,
        }).raise_for_status()
    t.check("toutes les inscriptions publiées", wait_for(lambda: sql(
        f"SELECT count(*) FROM outbox_event o JOIN registration r ON o.aggregate_id = r.id::text "
        f"WHERE r.session_id = '{session_id}' AND o.published_at IS NOT NULL") == str(n), timeout=60))
    wait_for(lambda: rabbit_queue("registration.orchestrator")["messages"] - before >= n, timeout=15)
    delta = rabbit_queue("registration.orchestrator")["messages"] - before
    t.check(f"exactement {n} messages dans registration.orchestrator (ni doublon ni perte)", delta == n, delta)
    publishers = Counter(
        line.split("|", 1)[0].strip()  # préfixe "outbox-dispatcher-N" ajouté par docker compose logs
        for line in compose("logs", "--since", "2m", "outbox-dispatcher").splitlines()
        if "publié" in line
    )
    t.check("travail réparti entre plusieurs dispatchers", len(publishers) >= 2, dict(publishers))
    compose("up", "-d", "--scale", "outbox-dispatcher=1", "outbox-dispatcher")

    t.done()


if __name__ == "__main__":
    main()
