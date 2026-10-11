"""Affiche le contenu d'une DLQ sans la consommer (diagnostic / soutenance « que contient votre DLQ ? »).

Usage : .venv/Scripts/python scripts/inspect-dlq.py [registration.dlq|notifications.dlq|audit.dlq]
"""

import json
import sys
from datetime import UTC, datetime

from _testlib import dlq_messages


def main() -> None:
    queue = sys.argv[1] if len(sys.argv) > 1 else "registration.dlq"
    messages = dlq_messages(queue)
    print(f"=== {queue} : {len(messages)} message(s) ===")
    for i, msg in enumerate(messages, 1):
        h = msg["headers"]
        death = (h.get("x-death") or [{}])[0]
        try:
            event = json.loads(msg["payload"])
            summary = f"{event.get('eventType')} eventId={event.get('eventId')} " \
                      f"registrationId={event.get('payload', {}).get('registrationId')}"
        except (ValueError, AttributeError):
            summary = f"payload non JSON : {msg['payload'][:80]!r}"
        print(f"\n#{i} {summary}")
        print(f"   motif     : {h.get('x-error-kind') or death.get('reason', '?')}")
        when = h.get("x-failed-at") or death.get("time", "-")
        if isinstance(when, int):  # x-death.time : timestamp AMQP (secondes epoch)
            when = datetime.fromtimestamp(when, UTC).isoformat()
        print(f"   tentatives: {h.get('x-attempts', '-')}   consumer: {h.get('x-consumer', '-')}   le {when}")
        print(f"   erreur    : {h.get('x-error') or '(rejet : message non traitable)'}")


if __name__ == "__main__":
    main()
