"""Test 9 : crash du worker juste après l'inscription Moodle (CRASH_AFTER_MOODLE=true).

Le worker inscrit l'apprenant dans Moodle puis s'arrête brutalement (os._exit) avant mapping et ACK.
Docker le redémarre, RabbitMQ redélivre le message non acquitté : la saga reprend sans doublon.

Usage : .venv/Scripts/python scripts/test-crash-worker.py
"""

import json
import subprocess
import uuid

import httpx

from _testlib import API_URL, Checks, Externals, compose, wait_for, wait_status


def restart_count() -> int:
    container = compose("ps", "-q", "registration-worker").strip().splitlines()[0]
    out = subprocess.run(["docker", "inspect", container], capture_output=True, text=True).stdout
    return json.loads(out)[0]["RestartCount"]


def main() -> None:
    t = Checks("Crash du worker après l'effet Moodle")
    api = httpx.Client(base_url=API_URL, timeout=15)
    ext = Externals()
    run = uuid.uuid4().hex[:8]
    email = f"crash-{run}@example.org"

    print("-- worker redémarré avec CRASH_AFTER_MOODLE=true")
    compose("up", "-d", "--scale", "registration-worker=1", "registration-worker",
            env={"CRASH_AFTER_MOODLE": "true"})
    t.check("worker en écoute", wait_for(lambda: "en écoute" in compose("logs", "--since", "30s",
                                                                          "registration-worker"), timeout=30))
    restarts_before = restart_count()

    resp = api.post("/api/v1/registrations", headers={"Idempotency-Key": f"crash-{run}"}, json={
        "student": {"email": email, "firstName": "Crash", "lastName": "Test"},
        "company": {"name": f"CRASH CORP {run.upper()}"}, "sessionId": "SESSION-PYTHON-01"})
    reg_id = resp.json()["registrationId"]
    t.check("POST -> 202", resp.status_code == 202, reg_id)

    t.check("le worker a planté après l'inscription Moodle (redémarré par Docker)",
            wait_for(lambda: restart_count() > restarts_before, timeout=60), f"RestartCount={restart_count()}")
    view = wait_status(api, reg_id, {"CONFIRMED", "COMPENSATED", "FAILED"}, timeout=120)
    t.check("après redélivrance : statut final CONFIRMED", view["status"] == "CONFIRMED", view.get("lastError"))

    history = [h["status"] for h in view["history"]]
    print(f"      historique : {' -> '.join(history)}")
    t.check("deux tentatives visibles (crash puis reprise)", history.count("PROCESSING") == 2, history)
    users = ext.moodle_users(email)
    t.check("aucun deuxième utilisateur Moodle", len(users) == 1, len(users))
    t.check("inscrit une seule fois au cours",
            users and ext.moodle_course_ids(users[0]["id"]).count(view["courseId"]) == 1)
    t.check("1 seul Lead et 1 seul Sales Order",
            len(ext.erp_list("Lead", [["email_id", "=", email]])) == 1
            and len(ext.erp_list("Sales Order", [["po_no", "=", reg_id]])) == 1)

    print("-- retour du worker en mode normal")
    compose("up", "-d", "registration-worker", env={"CRASH_AFTER_MOODLE": "false"})
    t.done()


if __name__ == "__main__":
    main()
