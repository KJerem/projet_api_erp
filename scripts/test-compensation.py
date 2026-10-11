"""Test 11 : compensation (saga) — Moodle OK puis échec ERPNext.

A. Échec ERP permanent (article inexistant -> 417) juste après l'inscription Moodle :
   compensation immédiate -> désinscription Moodle + place libérée + COMPENSATED.
B. Échec ERP temporaire qui dure (backend arrêté APRÈS l'inscription Moodle) :
   retries épuisés -> même compensation.
   Le worker tourne avec ERP_STEP_DELAY_SECONDS pour laisser au test le temps d'arrêter ERPNext.

Usage : .venv/Scripts/python scripts/test-compensation.py
"""

import uuid

import httpx

from _testlib import (API_URL, FINAL_STATUSES, Checks, Externals, compose, create_session, register, seats, sql,
                      wait_for, wait_status, wait_until_status)


def check_no_sales_order(t: Checks, ext: Externals, reg_id: str) -> None:
    t.check("aucun Sales Order ERPNext créé", ext.erp_list("Sales Order", [["po_no", "=", reg_id]]) == [])


def check_compensated(t: Checks, api: httpx.Client, ext: Externals, reg_id: str, email: str, session: str,
                      seats_before: int) -> None:
    view = wait_status(api, reg_id, FINAL_STATUSES, timeout=120)
    history = [h["status"] for h in view["history"]]
    print(f"      historique : {' -> '.join(history)}")
    t.check("statut COMPENSATED", view["status"] == "COMPENSATED", view["status"])
    t.check("Moodle était inscrit avant la compensation",
            "MOODLE_ENROLLED" in history and history.index("MOODLE_ENROLLED") < history.index("COMPENSATING"))
    users = ext.moodle_users(email)
    t.check("Moodle : apprenant désinscrit du cours (compte conservé)",
            len(users) == 1 and view["courseId"] not in ext.moodle_course_ids(users[0]["id"]),
            users and ext.moodle_course_ids(users[0]["id"]))
    t.check("place locale libérée", seats(api, session) == seats_before, f"{seats_before} -> {seats(api, session)}")
    t.check("événement registration.compensated émis", sql(
        f"SELECT count(*) FROM outbox_event WHERE aggregate_id = '{reg_id}' "
        "AND event_type = 'registration.compensated'") == "1")


def main() -> None:
    t = Checks("Compensation — Moodle OK, ERPNext en échec")
    api = httpx.Client(base_url=API_URL, timeout=15)
    ext = Externals()
    run = uuid.uuid4().hex[:8]

    # --- A. échec ERP permanent ------------------------------------------------------------------
    print("-- A. échec ERPNext permanent (417)")
    session = f"SESSION-COMP-{run.upper()}"
    create_session(api, session, 3, "PYTHON-ADV", "ARTICLE-INEXISTANT")
    reg_id, email = register(api, f"comp-perm-{run}", session)
    check_compensated(t, api, ext, reg_id, email, session, seats_before=3)
    check_no_sales_order(t, ext, reg_id)

    # --- B. échec ERP temporaire jusqu'à épuisement ------------------------------------------------
    print("\n-- B. ERPNext arrêté après l'inscription Moodle, retries épuisés")
    compose("up", "-d", "registration-worker", env={"ERP_STEP_DELAY_SECONDS": "10"})
    wait_for(lambda: "ERP_STEP_DELAY_SECONDS actif" in compose("logs", "--since", "30s", "registration-worker"),
             timeout=30)
    session_b = f"SESSION-COMPB-{run.upper()}"
    create_session(api, session_b, 3, "PYTHON-ADV", "FORMATION-PYTHON")
    try:
        reg_id, email = register(api, f"comp-exhausted-{run}", session_b)
        view = wait_until_status(api, reg_id, lambda v: v["status"] == "MOODLE_ENROLLED", timeout=60)
        t.check("Moodle inscrit, ERPNext coupé avant le Sales Order", view["status"] == "MOODLE_ENROLLED",
                view["status"])
        compose("stop", "-t", "1", "erpnext-backend")
        check_compensated(t, api, ext, reg_id, email, session_b, seats_before=3)
        final = api.get(f"/api/v1/registrations/{reg_id}").json()
        t.check("compensation après épuisement des 4 tentatives", final["attempts"] == 4, final["attempts"])
    finally:
        compose("start", "erpnext-backend")
        compose("up", "-d", "registration-worker", env={"ERP_STEP_DELAY_SECONDS": "0"})
    # Vérification côté ERPNext une fois le backend redémarré.
    wait_for(lambda: ext.erp.get("/api/method/ping").status_code == 200, timeout=90)
    check_no_sales_order(t, ext, reg_id)
    t.done()


if __name__ == "__main__":
    main()
