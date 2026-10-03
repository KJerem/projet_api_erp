"""Notifications Mailpit + audit : email de confirmation, email d'annulation, idempotence du notifier.

A. Inscription confirmée -> 1 email « Inscription confirmée » (Mailpit), avec les références.
B. Événement registration.confirmed republié 2 fois -> toujours 1 seul email.
C. Inscription compensée -> email « Inscription annulée ».
D. Audit : tous les événements de l'inscription sont journalisés, avec le correlationId.
E. Mailpit arrêté : retry avec backoff (notifications.retry.*), email délivré à son retour.

Usage : .venv/Scripts/python scripts/test-notifications.py
"""

import time
import uuid

import httpx

from _testlib import (API_URL, FINAL_STATUSES, Checks, audited_event_types, compose, create_session,
                      mailpit_body, mailpit_messages, outbox_event, publish_raw, register, sql, wait_for,
                      wait_status)


def main() -> None:
    t = Checks("Notifications (Mailpit) et audit")
    api = httpx.Client(base_url=API_URL, timeout=15)
    run = uuid.uuid4().hex[:8]

    # --- A. confirmation -----------------------------------------------------------------------
    print("-- A. email de confirmation")
    reg_id, email = register(api, f"notif-{run}")
    view = wait_status(api, reg_id, FINAL_STATUSES)
    t.check("inscription confirmée", view["status"] == "CONFIRMED", view.get("lastError"))
    t.check("email reçu dans Mailpit", wait_for(lambda: len(mailpit_messages(email)) == 1, timeout=30),
            len(mailpit_messages(email)))
    msg = mailpit_messages(email)[0]
    t.check("sujet « Inscription confirmée »", msg["Subject"].startswith("Inscription confirmée"), msg["Subject"])
    body = mailpit_body(msg["ID"])
    t.check("corps : référence d'inscription et Sales Order ERPNext",
            reg_id in body["Text"] and view["erpSalesOrderId"] in body["Text"])
    t.check("en-tête X-Correlation-ID propagé jusqu'à l'email",
            sql(f"SELECT correlation_id FROM notification WHERE registration_id = '{reg_id}'") == f"corr-notif-{run}")

    # --- B. doublon ------------------------------------------------------------------------------
    print("\n-- B. registration.confirmed republié 2 fois")
    event = outbox_event(reg_id, "registration.confirmed")
    event_id = event.split('"eventId": "')[1].split('"')[0]
    for _ in range(2):
        publish_raw("registration.confirmed", event.encode(), message_id=event_id)
    wait_for(lambda: compose("logs", "--since", "1m", "notification-worker").count("déjà traité") >= 2, timeout=20)
    time.sleep(2)
    t.check("toujours 1 seul email", len(mailpit_messages(email)) == 1, len(mailpit_messages(email)))

    # --- C. compensation -----------------------------------------------------------------------
    print("\n-- C. email d'annulation après compensation")
    session = f"SESSION-NOTIF-{run.upper()}"
    create_session(api, session, 2, "PYTHON-ADV", "ARTICLE-INEXISTANT")
    reg_c, email_c = register(api, f"notif-comp-{run}", session)
    t.check("inscription compensée", wait_status(api, reg_c, FINAL_STATUSES)["status"] == "COMPENSATED")
    t.check("email « Inscription annulée » reçu", wait_for(lambda: any(
        m["Subject"].startswith("Inscription annulée") for m in mailpit_messages(email_c)), timeout=30),
        [m["Subject"] for m in mailpit_messages(email_c)])

    # --- D. audit --------------------------------------------------------------------------------
    print("\n-- D. journal d'audit")
    t.check("audit : accepted + confirmed pour l'inscription A", wait_for(
        lambda: audited_event_types(reg_id) == ["registration.accepted", "registration.confirmed"], timeout=20),
        audited_event_types(reg_id))
    t.check("audit : accepted + compensated pour l'inscription C", wait_for(
        lambda: audited_event_types(reg_c) == ["registration.accepted", "registration.compensated"], timeout=20),
        audited_event_types(reg_c))
    t.check("audit idempotent malgré les doublons publiés (1 ligne par eventId)",
            sql(f"SELECT count(*) FROM audit_log WHERE event_id = '{event_id}'") == "1")
    t.check("correlationId conservé dans l'audit", sql(
        f"SELECT count(DISTINCT correlation_id) FROM audit_log WHERE payload->'payload'->>'registrationId' = "
        f"'{reg_id}'") == "1")

    # --- E. serveur SMTP indisponible ----------------------------------------------------------
    print("\n-- E. Mailpit arrêté : l'email est retenté, pas perdu")
    compose("stop", "-t", "1", "mailpit")
    try:
        reg_e, email_e = register(api, f"notif-smtp-{run}")
        t.check("inscription confirmée malgré le SMTP arrêté",
                wait_status(api, reg_e, FINAL_STATUSES)["status"] == "CONFIRMED")
        t.check("le notification-worker programme un nouvel essai", wait_for(
            lambda: "nouvel essai programmé" in compose("logs", "--since", "1m", "notification-worker"), timeout=20))
    finally:
        compose("start", "mailpit")
    t.check("email délivré après le retour de Mailpit", wait_for(
        lambda: len(mailpit_messages(email_e)) == 1, timeout=30), len(mailpit_messages(email_e)))
    t.done()


if __name__ == "__main__":
    main()
