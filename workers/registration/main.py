"""Registration Worker — consomme registration.orchestrator.

Usage : python -m workers.registration.main
Variables : CRASH_AFTER_MOODLE=true pour le test de crash (test 9).
"""

import logging

from shared.config import get_settings
from shared.consumer import Consumer
from shared.db import create_pool
from shared.logging_setup import setup_logging
from shared.rabbit import QUEUE_ORCHESTRATOR
from workers.registration.processor import CONSUMER_NAME, RegistrationProcessor


def main() -> None:
    settings = get_settings()
    setup_logging(settings.service_name, settings.log_level)
    pool = create_pool(settings)
    pool.open(wait=True, timeout=60)
    logging.getLogger("registration-worker").info("démarrage")
    Consumer(CONSUMER_NAME, QUEUE_ORCHESTRATOR, settings, pool, RegistrationProcessor(settings, pool)).run()


if __name__ == "__main__":
    main()
