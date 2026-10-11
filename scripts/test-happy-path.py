"""Tests 1, 2 et 12 : happy path complet, statut asynchrone, rejeu de l'Idempotency-Key sans doublon.

Vérifie les effets DANS Moodle et ERPNext (API officielles), pas seulement dans la base d'intégration.
Usage : .venv/Scripts/python scripts/test-happy-path.py
"""

import time
import uuid

import httpx

from _testlib import API_URL, Checks, Externals, audited_event_types, mailpit_messages, wait_for, wait_status

SESSION = "SESSION-PYTHON-01"


def main() -> None:
    t = Checks("Happy path — inscription de bout en bout")
    run = uuid.uuid4().hex[:8]
    email = f"alice.{run}@example.org"
    company = f"ACME {run.upper()}"
    key = f"happy-{run}"
    payload = {"student": {"email": email, "firstName": "Alice", "lastName": "Martin"},
               "company": {"name": company}, "sessionId": SESSION}
    api = httpx.Client(base_url=API_URL, timeout=15)
    ext = Externals()

    seats_before = api.get(f"/api/v1/sessions/{SESSION}").json()["remainingSeats"]
    resp = api.post("/api/v1/registrations", json=payload,
                    headers={"Idempotency-Key": key, "X-Correlation-ID": f"corr-happy-{run}"})
    t.check("POST -> 202 Accepted", resp.status_code == 202, resp.json())
    reg_id = resp.json()["registrationId"]

    # --- Test 12 : statut asynchrone et transitions ------------------------------------------------
    view = wait_status(api, reg_id, {"CONFIRMED", "COMPENSATED", "FAILED"})
    print(f"      statuts observés en polling : {' -> '.join(view['observedStatuses'])}")
    history = [h["status"] for h in view["history"]]
    print(f"      historique enregistré       : {' -> '.join(history)}")
    t.check("statut final CONFIRMED", view["status"] == "CONFIRMED", view.get("lastError"))
    t.check("historique complet ACCEPTED→PROCESSING→MOODLE_ENROLLED→ERP_RECORDED→CONFIRMED",
            history == ["ACCEPTED", "PROCESSING", "MOODLE_ENROLLED", "ERP_RECORDED", "CONFIRMED"], history)
    t.check("le GET a montré au moins un état intermédiaire", len(view["observedStatuses"]) >= 2,
            view["observedStatuses"])
    t.check("correlationId conservé", view["correlationId"] == f"corr-happy-{run}")

    # --- Vérifications dans les vrais systèmes ---------------------------------------------------
    users = ext.moodle_users(email)
    t.check("Moodle : 1 utilisateur avec cet email", len(users) == 1, [u.get("id") for u in users])
    t.check("Moodle : moodleUserId cohérent", users and users[0]["id"] == view["moodleUserId"], view["moodleUserId"])
    t.check("Moodle : inscrit au bon cours", users and view["courseId"] in ext.moodle_course_ids(users[0]["id"]),
            view["courseId"])
    leads = ext.erp_list("Lead", [["email_id", "=", email]])
    t.check("ERPNext : 1 Lead (CRM) pour l'apprenant", len(leads) == 1 and leads[0]["name"] == view["erpLeadId"],
            leads)
    customers = ext.erp_list("Customer", [["customer_name", "=", company]])
    t.check("ERPNext : 1 Customer pour l'entreprise", len(customers) == 1, customers)
    orders = ext.erp_list("Sales Order", [["po_no", "=", reg_id]], ["name", "docstatus", "grand_total", "customer"])
    t.check("ERPNext : 1 Sales Order soumis (docstatus=1) lié à l'inscription",
            len(orders) == 1 and orders[0]["docstatus"] == 1 and orders[0]["name"] == view["erpSalesOrderId"], orders)
    t.check("ERPNext : montant du Sales Order = prix catalogue (850)",
            orders and float(orders[0]["grand_total"]) == 850.0, orders and orders[0]["grand_total"])

    # --- Notification et audit ----------------------------------------------------------------------
    t.check("Mailpit : email de confirmation reçu", wait_for(lambda: any(
        "confirmée" in m["Subject"] for m in mailpit_messages(email)), timeout=30),
        [m["Subject"] for m in mailpit_messages(email)])
    t.check("audit : registration.accepted et registration.confirmed journalisés", wait_for(lambda: set(
        audited_event_types(reg_id)) >= {"registration.accepted", "registration.confirmed"}, timeout=30),
        audited_event_types(reg_id))

    # --- Test 2 : rejeu de la même Idempotency-Key après traitement ----------------------------------
    replay = api.post("/api/v1/registrations", json=payload, headers={"Idempotency-Key": key})
    t.check("rejeu même clé -> même registrationId", replay.json().get("registrationId") == reg_id, replay.json())
    time.sleep(3)
    t.check("après rejeu : toujours 1 utilisateur Moodle", len(ext.moodle_users(email)) == 1)
    t.check("après rejeu : toujours 1 Lead / 1 Customer / 1 Sales Order",
            len(ext.erp_list("Lead", [["email_id", "=", email]])) == 1
            and len(ext.erp_list("Customer", [["customer_name", "=", company]])) == 1
            and len(ext.erp_list("Sales Order", [["po_no", "=", reg_id]])) == 1)
    seats_after = api.get(f"/api/v1/sessions/{SESSION}").json()["remainingSeats"]
    t.check("une seule place consommée", seats_before - seats_after == 1, f"{seats_before} -> {seats_after}")
    t.check("toujours 1 seul email de confirmation", len(mailpit_messages(email)) == 1, len(mailpit_messages(email)))

    print(f"\n      Moodle  : http://localhost:8081/user/profile.php?id={view['moodleUserId']}")
    print(f"      ERPNext : http://localhost:8080/app/sales-order/{view['erpSalesOrderId']}")
    print(f"      Mailpit : http://localhost:8025/  (destinataire {email})")
    t.done()


if __name__ == "__main__":
    main()
