"""Consumer RabbitMQ générique : ACK manuel en fin de traitement, déduplication, retry avec backoff, DLQ.

    message ─► JSON/enveloppe valide ? ──non──► reject (DLX de la queue ─► DLQ)
                 │ oui
                 ▼
          eventId déjà dans processed_events ? ──oui──► ACK sans refaire l'effet
                 │ non
                 ▼
          handler.handle(event)  (thread dédié : les heartbeats AMQP continuent)
                 ├─ succès ─────────────────────────────► INSERT processed_events, ACK
                 ├─ DuplicateInFlight ──────────────────► ACK (un autre worker traite ce même événement)
                 ├─ TransientError, essais restants ───► republication retry.Ns (x-attempt+1), ACK
                 └─ PermanentError / essais épuisés ───► handler.on_give_up() ─► "handled" : ACK
                                                                               └─ "dlq" : publication DLQ, ACK

Les publications (retry, DLQ) sont confirmées par le broker AVANT l'ACK du message d'origine :
un crash entre les deux produit au pire un doublon, jamais une perte.
"""

import functools
import json
import logging
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import pika
from pika.exceptions import AMQPError
from psycopg_pool import ConnectionPool

from shared.config import Settings
from shared.errors import DuplicateInFlight, PermanentError, TransientError
from shared.events import SCHEMA_VERSION
from shared.logging_setup import correlation_id_var
from shared.rabbit import DLX, RETRY_DELAYS, RETRY_EXCHANGE, connect, declare_topology, retry_queue_name

log = logging.getLogger("consumer")

MAX_ATTEMPTS = 1 + len(RETRY_DELAYS)  # 1 tentative immédiate + 3 retries (2 s, 4 s, 8 s)


class Handler(Protocol):
    def handle(self, event: dict, attempt: int) -> None: ...

    def on_retry(self, event: dict, error: Exception, attempt: int, delay: int) -> None: ...

    def on_give_up(self, event: dict, error: Exception, attempts: int) -> str:
        """Retourne "handled" (issue métier gérée, ex. compensation) ou "dlq"."""
        ...


@dataclass
class Outcome:
    kind: str  # ack | retry | dlq | reject
    delay: int = 0
    headers: dict = field(default_factory=dict)


class InvalidMessage(Exception):
    pass


def parse_event(body: bytes) -> dict:
    try:
        event = json.loads(body)
        uuid.UUID(event["eventId"])
        if not isinstance(event["payload"], dict) or not event.get("eventType"):
            raise ValueError("enveloppe incomplète")
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidMessage(f"message non traitable : {exc!r}") from exc
    if event.get("schemaVersion") != SCHEMA_VERSION:
        raise InvalidMessage(f"schemaVersion non supportée : {event.get('schemaVersion')}")
    return event


class Consumer:
    def __init__(self, name: str, queue: str, settings: Settings, pool: ConnectionPool, handler: Handler,
                 retry_enabled: bool = True) -> None:
        self.name = name
        self.queue = queue
        self.settings = settings
        self.pool = pool
        self.handler = handler
        self.retry_enabled = retry_enabled
        self._connection: pika.BlockingConnection | None = None
        self._channel = None
        self._running = True

    # -- déduplication ------------------------------------------------------------------------------
    def already_processed(self, event_id: str) -> bool:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT 1 FROM processed_events WHERE consumer_name = %s AND event_id = %s", (self.name, event_id)
            ).fetchone() is not None

    def mark_processed(self, event_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO processed_events (consumer_name, event_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (self.name, event_id),
            )

    # -- traitement (thread de travail) ------------------------------------------------------------
    def _process(self, body: bytes, headers: dict) -> Outcome:
        attempt = int(headers.get("x-attempt", 1))
        try:
            event = parse_event(body)
        except InvalidMessage as exc:
            log.error("message rejeté vers la DLQ", extra={"error": str(exc)})
            return Outcome("reject")

        correlation_id_var.set(event.get("correlationId"))
        ctx = {"eventId": event["eventId"], "eventType": event["eventType"], "attempt": attempt,
               "registrationId": event["payload"].get("registrationId")}

        try:
            if self.already_processed(event["eventId"]):
                log.info("événement déjà traité : ACK sans nouvel effet (doublon)", extra=ctx)
                return Outcome("ack")
            self.handler.handle(event, attempt)
            self.mark_processed(event["eventId"])
            log.info("événement traité", extra=ctx)
            return Outcome("ack")
        except DuplicateInFlight:
            log.info("même événement en cours de traitement par un autre worker : ACK du doublon", extra=ctx)
            return Outcome("ack")
        except PermanentError as exc:
            log.error("erreur permanente", extra={**ctx, "error": str(exc)})
            return self._give_up(event, exc, attempt, "permanent")
        except Exception as exc:  # TransientError ou erreur inattendue (base indisponible, bug…)
            if not isinstance(exc, TransientError):
                log.exception("erreur inattendue traitée comme temporaire", extra=ctx)
            if self.retry_enabled and attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS[attempt - 1]
                log.warning("erreur temporaire : nouvel essai programmé",
                            extra={**ctx, "error": str(exc), "retryInSeconds": delay})
                try:
                    self.handler.on_retry(event, exc, attempt, delay)
                except Exception:  # noqa: BLE001 - l'état RETRYING est informatif
                    log.exception("impossible d'enregistrer l'état RETRYING", extra=ctx)
                return Outcome("retry", delay=delay, headers={"x-attempt": attempt + 1, "x-last-error": str(exc)[:500]})
            return self._give_up(event, exc, attempt, "retries exhausted")

    def _give_up(self, event: dict, error: Exception, attempt: int, reason: str) -> Outcome:
        ctx = {"eventId": event["eventId"], "registrationId": event["payload"].get("registrationId")}
        dlq_headers = {"x-error": str(error)[:1000], "x-error-kind": reason, "x-attempts": attempt,
                       "x-consumer": self.name, "x-failed-at": datetime.now(UTC).isoformat()}
        try:
            decision = self.handler.on_give_up(event, error, attempt)
        except Exception as exc:  # noqa: BLE001 - ex. compensation impossible (Moodle indisponible)
            log.exception("abandon non finalisé : message conservé en DLQ pour intervention", extra=ctx)
            dlq_headers["x-give-up-error"] = str(exc)[:500]
            return Outcome("dlq", headers=dlq_headers)
        if decision == "handled":
            self.mark_processed(event["eventId"])
            log.warning("abandon géré par le handler (compensation)", extra={**ctx, "reason": reason})
            return Outcome("ack")
        log.error("message envoyé en DLQ", extra={**ctx, "reason": reason, "error": str(error)})
        return Outcome("dlq", headers=dlq_headers)

    # -- finalisation (thread de la connexion AMQP) --------------------------------------------------
    def _finish(self, delivery_tag: int, body: bytes, props: pika.BasicProperties, outcome: Outcome) -> None:
        channel = self._channel
        if channel is None or not channel.is_open:
            log.warning("canal fermé avant l'ACK : le message sera redélivré par RabbitMQ")
            return
        headers = dict(props.headers or {})
        if outcome.kind == "retry":
            channel.basic_publish(RETRY_EXCHANGE, retry_queue_name(self.queue, outcome.delay), body,
                                  properties=self._copy_props(props, {**headers, **outcome.headers}), mandatory=True)
        elif outcome.kind == "dlq":
            channel.basic_publish(DLX, self.queue, body,
                                  properties=self._copy_props(props, {**headers, **outcome.headers}), mandatory=True)
        if outcome.kind == "reject":
            channel.basic_reject(delivery_tag, requeue=False)
        else:
            channel.basic_ack(delivery_tag)

    @staticmethod
    def _copy_props(props: pika.BasicProperties, headers: dict) -> pika.BasicProperties:
        return pika.BasicProperties(
            content_type=props.content_type, delivery_mode=pika.DeliveryMode.Persistent,
            message_id=props.message_id, correlation_id=props.correlation_id, type=props.type,
            timestamp=props.timestamp, headers=headers,
        )

    def _on_message(self, channel, method, props, body) -> None:
        def work() -> None:
            outcome = self._process(body, dict(props.headers or {}))
            self._connection.add_callback_threadsafe(
                functools.partial(self._finish, method.delivery_tag, body, props, outcome)
            )
            correlation_id_var.set(None)

        threading.Thread(target=work, name=f"{self.name}-work", daemon=True).start()

    # -- boucle principale ------------------------------------------------------------------------
    def _stop(self, signum, _frame) -> None:
        log.info("arrêt demandé", extra={"signal": signum})
        self._running = False
        if self._connection is not None:
            self._connection.add_callback_threadsafe(self._channel.stop_consuming)

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        while self._running:
            try:
                self._connection = connect(self.settings, connection_name=self.name)
                self._channel = self._connection.channel()
                declare_topology(self._channel)
                self._channel.confirm_delivery()
                self._channel.basic_qos(prefetch_count=1)  # un message à la fois : ACK = fin de traitement
                self._channel.basic_consume(self.queue, self._on_message, auto_ack=False)
                log.info("en écoute", extra={"queue": self.queue})
                self._channel.start_consuming()
            except AMQPError as exc:
                log.warning("connexion RabbitMQ perdue, reconnexion", extra={"error": repr(exc)})
                time.sleep(2)
            finally:
                try:
                    if self._connection is not None and self._connection.is_open:
                        self._connection.close()
                except Exception:  # noqa: BLE001
                    pass
        log.info("consumer arrêté")
