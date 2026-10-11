"""Notification Worker — consomme `notifications` et envoie les emails (SMTP Mailpit en démo).

Idempotence : l'eventId est vérifié dans processed_events avant l'envoi, puis l'email envoyé est
tracé (table notification) dans la même transaction que processed_events. Un crash entre l'envoi
SMTP et ce COMMIT peut produire un second email au rejeu (SMTP n'est pas transactionnel) : l'en-tête
Message-ID est dérivé de l'eventId pour que les clients mail puissent dédoublonner.

Usage : python -m workers.notifications.main
"""

import logging
import os
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr

from psycopg_pool import ConnectionPool

from shared.config import get_settings
from shared.consumer import Consumer
from shared.db import create_pool
from shared.errors import PermanentError, TransientError
from shared.logging_setup import setup_logging
from shared.rabbit import QUEUE_NOTIFICATIONS

CONSUMER_NAME = "notification-worker"
log = logging.getLogger(CONSUMER_NAME)


def render(event: dict) -> tuple[str, str]:
    """Retourne (sujet, corps texte) selon le type d'événement."""
    p = event["payload"]
    student = p.get("student") or {}
    hello = f"Bonjour {student.get('firstName', '')},".replace(" ,", ",")
    ref = f"Référence d'inscription : {p.get('registrationId')}\nSession : {p.get('sessionId')}"
    if event["eventType"] == "registration.confirmed":
        return (
            f"Inscription confirmée — {p.get('sessionId')}",
            f"{hello}\n\nVotre inscription est confirmée.\n\n{ref}\n"
            f"Cours Moodle : {p.get('courseShortname')} (identifiant {p.get('courseId')})\n"
            f"Bon de commande ERPNext : {p.get('erpSalesOrderId')}\n\n"
            "Vos identifiants Moodle vous seront communiqués séparément.\n\nL'équipe OpenTraining",
        )
    if event["eventType"] == "registration.compensated":
        return (
            f"Inscription annulée — {p.get('sessionId')}",
            f"{hello}\n\nVotre inscription n'a pas pu être finalisée côté administratif : "
            f"elle a été annulée et votre place libérée. Aucun montant ne vous sera facturé.\n\n{ref}\n\n"
            "Notre équipe va vous recontacter.\n\nL'équipe OpenTraining",
        )
    if event["eventType"] == "registration.failed":
        return (
            f"Inscription non aboutie — {p.get('sessionId')}",
            f"{hello}\n\nVotre demande d'inscription n'a pas pu être traitée.\n\n{ref}\n\n"
            "Notre équipe va vous recontacter.\n\nL'équipe OpenTraining",
        )
    if event["eventType"] == "notification.requested":
        return p.get("subject", "Notification OpenTraining"), p.get("body", "")
    raise PermanentError(f"type d'événement non notifiable : {event['eventType']}")


class NotificationHandler:
    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool
        self.smtp_host = os.environ.get("SMTP_HOST", "mailpit")
        self.smtp_port = int(os.environ.get("SMTP_PORT", "1025"))
        self.sender = os.environ.get("MAIL_FROM", "inscriptions@opentraining.local")
        self.timeout = get_settings().http_timeout_seconds

    def handle(self, event: dict, attempt: int) -> None:
        p = event["payload"]
        recipient = (p.get("student") or {}).get("email") or p.get("to")
        if not recipient:
            raise PermanentError("événement sans destinataire")
        subject, body = render(event)

        msg = EmailMessage()
        msg["From"] = formataddr(("OpenTraining", self.sender))
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Message-ID"] = f"<{event['eventId']}@opentraining.local>"
        msg["X-Correlation-ID"] = event.get("correlationId") or ""
        msg["X-Registration-ID"] = str(p.get("registrationId", ""))
        msg.set_content(body)

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as smtp:
                smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise PermanentError(f"destinataire refusé : {recipient}") from exc
        except (smtplib.SMTPException, OSError, socket.timeout) as exc:
            raise TransientError(f"SMTP indisponible : {exc!r}") from exc

        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO notification (event_id, event_type, registration_id, recipient, subject, correlation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING",
                (event["eventId"], event["eventType"], p.get("registrationId"), recipient, subject,
                 event.get("correlationId")),
            )
            conn.execute("INSERT INTO processed_events (consumer_name, event_id) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (CONSUMER_NAME, event["eventId"]))
        log.info("email envoyé", extra={"eventId": event["eventId"], "registrationId": p.get("registrationId"),
                                        "recipient": recipient, "subject": subject})

    def on_retry(self, event: dict, error: Exception, attempt: int, delay: int) -> None:
        pass

    def on_give_up(self, event: dict, error: Exception, attempts: int) -> str:
        return "dlq"  # email non envoyé : conservé en notifications.dlq pour renvoi manuel


def main() -> None:
    settings = get_settings()
    setup_logging(settings.service_name, settings.log_level)
    pool = create_pool(settings)
    pool.open(wait=True, timeout=60)
    Consumer(CONSUMER_NAME, QUEUE_NOTIFICATIONS, settings, pool, NotificationHandler(pool)).run()


if __name__ == "__main__":
    main()
