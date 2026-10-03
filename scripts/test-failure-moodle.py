"""Test 6 : Moodle indisponible -> retry avec backoff, aucune perte de message, état visible.

Moodle est gelé (docker pause) : les appels Web Services partent en timeout (HTTP_TIMEOUT_SECONDS).
Le worker passe en RETRYING (2 s, 4 s, 8 s) ; Moodle est dégelé pendant la fenêtre de retry
et l'inscription aboutit sans doublon.

Usage : .venv/Scripts/python scripts/test-failure-moodle.py
"""

import uuid

import httpx

from _testlib import API_URL, FINAL_STATUSES, Checks, Externals, compose, register, wait_status, wait_until_status


def main() -> None:
    t = Checks("Moodle indisponible (timeout) — retry puis reprise")
    api = httpx.Client(base_url=API_URL, timeout=15)
    ext = Externals()
    tag = f"moodle-down-{uuid.uuid4().hex[:8]}"

    compose("pause", "moodle")
    try:
        reg_id, email = register(api, tag)
        t.check("POST accepté (202) malgré Moodle indisponible", bool(reg_id), reg_id)
        view = wait_until_status(api, reg_id, lambda v: v["status"] == "RETRYING" and v["attempts"] >= 2, timeout=60)
        t.check("état visible : RETRYING après 2 tentatives", view["status"] == "RETRYING" and view["attempts"] >= 2,
                f"{view['status']} attempts={view['attempts']}")
        t.check("erreur visible et classée temporaire (timeout Moodle)", "Moodle timeout" in (view["lastError"] or ""),
                view["lastError"])
    finally:
        compose("unpause", "moodle")

    view = wait_status(api, reg_id, FINAL_STATUSES, timeout=90)
    history = [h["status"] for h in view["history"]]
    print(f"      historique : {' -> '.join(history)}")
    t.check("après retour de Moodle : CONFIRMED (message non perdu)", view["status"] == "CONFIRMED",
            view.get("lastError"))
    t.check("retries tracés dans l'historique", history.count("RETRYING") >= 2, history.count("RETRYING"))
    t.check("1 seul utilisateur Moodle, inscrit au cours", len(ext.moodle_users(email)) == 1
            and view["courseId"] in ext.moodle_course_ids(view["moodleUserId"]))
    t.check("lastError effacé après succès", view["lastError"] is None, view["lastError"])
    t.done()


if __name__ == "__main__":
    main()
