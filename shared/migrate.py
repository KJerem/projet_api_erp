"""Applique les migrations SQL versionnées puis initialise les sessions du catalogue.

Usage : python -m shared.migrate (service one-shot `migrate` du docker-compose).
Idempotent et protégé par un verrou consultatif : plusieurs exécutions simultanées sont sans danger.
"""

import json
import logging
import time
from pathlib import Path

import psycopg

from shared.config import get_settings
from shared.logging_setup import setup_logging

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_LOCK_ID = 727_001

log = logging.getLogger("migrate")


def connect(dsn: str) -> psycopg.Connection:
    for attempt in range(1, 31):
        try:
            return psycopg.connect(dsn, autocommit=True)
        except psycopg.OperationalError as exc:
            log.info("PostgreSQL indisponible, nouvel essai", extra={"attempt": attempt, "error": str(exc)})
            time.sleep(2)
    raise SystemExit("PostgreSQL injoignable")


def apply_migrations(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(255) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            log.info("migration appliquée", extra={"version": path.name})


def seed_sessions(conn: psycopg.Connection, catalog_file: Path) -> None:
    """Crée les sessions absentes ; ne touche jamais aux places restantes d'une session existante."""
    sessions = json.loads(catalog_file.read_text(encoding="utf-8"))["sessions"]
    with conn.transaction():
        for s in sessions:
            cur = conn.execute(
                """
                INSERT INTO training_session (id, moodle_course_shortname, erp_item_code, capacity, remaining_seats)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (s["sessionId"], s["course"]["shortname"], s["erp"]["itemCode"], s["capacity"], s["capacity"]),
            )
            if cur.rowcount:
                log.info("session créée", extra={"sessionId": s["sessionId"], "capacity": s["capacity"]})


def main() -> None:
    settings = get_settings()
    setup_logging("migrate", settings.log_level)
    with connect(settings.db_dsn) as conn:
        apply_migrations(conn)
        seed_sessions(conn, settings.catalog_file)
    log.info("base d'intégration à jour")


if __name__ == "__main__":
    main()
