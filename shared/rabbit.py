"""Connexion RabbitMQ, déclaration idempotente de la topologie et publication d'événements.

Topologie (déclarée en code par chaque service au démarrage — "topology as code") :

    si.events (topic) ──registration.accepted──────────────► registration.orchestrator ─┐
                      ├─registration.confirmed/compensated──► notifications              │ rejet / délai max
                      └─#───────────────────────────────────► audit                      │
                                                                                         ▼
    si.events.dlx (direct) ──<nom de queue>──► registration.dlq / notifications.dlq / audit.dlq

    si.events.retry (direct) ──retry.Ns──► registration.retry.Ns (TTL N s)
                                             └─ expiration ─► exchange par défaut ─► registration.orchestrator

Les retries reviennent directement dans la queue de l'orchestrateur (exchange par défaut) : ils ne
repassent pas par si.events, donc ne sont pas dupliqués vers l'audit ou les notifications.
"""

import json
import logging
import time
from datetime import UTC, datetime

import pika
from pika.adapters.blocking_connection import BlockingChannel

from shared.config import Settings

log = logging.getLogger("rabbit")

EXCHANGE = "si.events"
DLX = "si.events.dlx"
RETRY_EXCHANGE = "si.events.retry"

QUEUE_ORCHESTRATOR = "registration.orchestrator"
QUEUE_NOTIFICATIONS = "notifications"
QUEUE_AUDIT = "audit"
QUEUE_REGISTRATION_DLQ = "registration.dlq"

# Délais de backoff (secondes) : tentative 2 -> +2 s, 3 -> +4 s, 4 -> +8 s.
RETRY_DELAYS = (2, 4, 8)

# Filet de sécurité des quorum queues : au-delà, un message redélivré en boucle (ex. crash
# systématique du worker) part en DLQ au lieu de bloquer la queue.
DELIVERY_LIMIT = 10

_CONSUMER_QUEUES = {
    QUEUE_ORCHESTRATOR: ["registration.accepted"],
    QUEUE_NOTIFICATIONS: ["registration.confirmed", "registration.compensated", "notification.requested"],
    QUEUE_AUDIT: ["#"],
}
_DLQS = {
    QUEUE_ORCHESTRATOR: QUEUE_REGISTRATION_DLQ,
    QUEUE_NOTIFICATIONS: "notifications.dlq",
    QUEUE_AUDIT: "audit.dlq",
}


def retry_queue_name(delay: int) -> str:
    return f"registration.retry.{delay}s"


def connect(settings: Settings, connection_name: str, attempts: int | None = None) -> pika.BlockingConnection:
    """Connexion avec reconnexion patiente (RabbitMQ peut être en cours de démarrage ou arrêté)."""
    params = pika.URLParameters(settings.rabbitmq_url)
    params.heartbeat = 30
    params.blocked_connection_timeout = 30
    params.client_properties = {"connection_name": connection_name}
    attempt = 0
    while True:
        attempt += 1
        try:
            return pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError as exc:
            if attempts is not None and attempt >= attempts:
                raise
            delay = min(2 ** min(attempt, 4), 15)
            log.warning("RabbitMQ indisponible, nouvelle tentative", extra={"attempt": attempt, "retryIn": delay,
                                                                            "error": repr(exc)})
            time.sleep(delay)


def declare_topology(channel: BlockingChannel) -> None:
    channel.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
    channel.exchange_declare(DLX, exchange_type="direct", durable=True)
    channel.exchange_declare(RETRY_EXCHANGE, exchange_type="direct", durable=True)

    for queue, dlq in _DLQS.items():
        # DLQ en queue classique durable : pas de delivery-limit, un message mort inspecté (peek + requeue)
        # plusieurs fois n'est jamais supprimé (une quorum queue l'abandonnerait au-delà de sa limite).
        channel.queue_declare(dlq, durable=True, arguments={"x-queue-type": "classic"})
        channel.queue_bind(dlq, DLX, routing_key=queue)

    for queue, routing_keys in _CONSUMER_QUEUES.items():
        channel.queue_declare(
            queue,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": DLX,
                "x-dead-letter-routing-key": queue,
                "x-delivery-limit": DELIVERY_LIMIT,
            },
        )
        for key in routing_keys:
            channel.queue_bind(queue, EXCHANGE, routing_key=key)

    for delay in RETRY_DELAYS:
        name = retry_queue_name(delay)
        channel.queue_declare(
            name,
            durable=True,
            arguments={
                "x-message-ttl": delay * 1000,
                "x-dead-letter-exchange": "",  # exchange par défaut : routage direct par nom de queue
                "x-dead-letter-routing-key": QUEUE_ORCHESTRATOR,
            },
        )
        channel.queue_bind(name, RETRY_EXCHANGE, routing_key=name)


def event_properties(event: dict, headers: dict | None = None) -> pika.BasicProperties:
    return pika.BasicProperties(
        content_type="application/json",
        delivery_mode=pika.DeliveryMode.Persistent,
        message_id=event["eventId"],
        correlation_id=event["correlationId"],
        type=event["eventType"],
        timestamp=int(datetime.now(UTC).timestamp()),
        headers={"schemaVersion": event["schemaVersion"], "x-correlation-id": event["correlationId"], **(headers or {})},
    )


def publish_event(channel: BlockingChannel, exchange: str, routing_key: str, event: dict,
                  headers: dict | None = None) -> None:
    """Publie un message persistant. Avec confirm_delivery() actif sur le canal, l'appel est bloquant
    jusqu'à l'ack du broker et lève une exception si le message est refusé ou non routable."""
    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        properties=event_properties(event, headers),
        mandatory=True,
    )
