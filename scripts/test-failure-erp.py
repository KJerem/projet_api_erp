"""Test 7 : ERPNext indisponible — distinction erreur temporaire / erreur permanente.

A. Temporaire : backend ERPNext gelé (docker pause) -> timeout -> RETRYING -> dégel -> CONFIRMED.
B. Permanente : article inexistant dans ERPNext -> HTTP 417 -> AUCUN retry (attempts = 1),
   abandon immédiat (compensation, cf. test 11).

Usage : .venv/Scripts/python scripts/test-failure-erp.py
"""

import uuid

import httpx

from _testlib import (API_URL, FINAL_STATUSES, Checks, compose, create_session, register, wait_status,
                      wait_until_status)


def main() -> None:
    t = Checks("ERPNext indisponible — temporaire vs permanent")
    api = httpx.Client(base_url=API_URL, timeout=15)
    run = uuid.uuid4().hex[:8]

    # --- A. erreur temporaire ----------------------------------------------------------------------
    print("-- A. backend ERPNext gelé (timeout)")
    compose("pause", "erpnext-backend")
    try:
        reg_a, _ = register(api, f"erp-down-{run}")
        view = wait_until_status(api, reg_a, lambda v: v["status"] == "RETRYING" and v["attempts"] >= 2, timeout=60)
        t.check("RETRYING après 2 tentatives", view["status"] == "RETRYING" and view["attempts"] >= 2,
                f"{view['status']} attempts={view['attempts']}")
        t.check("erreur classée temporaire (timeout ERPNext)", "ERPNext timeout" in (view["lastError"] or ""),
                view["lastError"])
    finally:
        compose("unpause", "erpnext-backend")
    view = wait_status(api, reg_a, FINAL_STATUSES, timeout=90)
    t.check("après retour d'ERPNext : CONFIRMED", view["status"] == "CONFIRMED", view.get("lastError"))

    # --- B. erreur permanente ----------------------------------------------------------------------
    print("\n-- B. erreur permanente (article ERPNext inexistant -> 417)")
    session = f"SESSION-ERR-{run.upper()}"
    create_session(api, session, 5, "PYTHON-ADV", "ARTICLE-INEXISTANT")
    reg_b, _ = register(api, f"erp-perm-{run}", session)
    view = wait_status(api, reg_b, FINAL_STATUSES, timeout=60)
    history = [h["status"] for h in view["history"]]
    print(f"      historique : {' -> '.join(history)}")
    t.check("erreur 4xx identifiée comme permanente", "HTTP 417" in (view["lastError"] or ""), view["lastError"])
    t.check("aucun retry sur erreur permanente (attempts = 1, pas de RETRYING)",
            view["attempts"] == 1 and "RETRYING" not in history, f"attempts={view['attempts']}")
    t.check("abandon immédiat géré (COMPENSATED)", view["status"] == "COMPENSATED", view["status"])
    t.done()


if __name__ == "__main__":
    main()
