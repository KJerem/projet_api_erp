"""Catalogue agrégé (agrégation synchrone) : PostgreSQL + Moodle + ERPNext interrogés en parallèle.

    PostgreSQL -> sessions, capacité restante
    Moodle     -> nom du cours (core_course_get_courses_by_field, un seul appel)
    ERPNext    -> tarif catalogue (Item Price, un seul appel)

Chaque appel externe a un timeout (HTTP_TIMEOUT_SECONDS) : la latence totale est bornée par le plus
lent des deux, pas par leur somme.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from psycopg_pool import ConnectionPool

from app.errors import ApiError
from shared.config import Settings
from shared.errors import IntegrationError
from shared.erpnext import ErpNextClient
from shared.logging_setup import correlation_id_var
from shared.moodle import MoodleClient
from shared.secrets import erpnext_token, moodle_token

log = logging.getLogger("integration-api.catalog")

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="catalog")


def _moodle_courses(settings: Settings, correlation_id: str) -> dict[str, dict]:
    client = MoodleClient(os.environ.get("MOODLE_URL", "http://moodle"), moodle_token(settings.secrets_dir),
                          settings.http_timeout_seconds, correlation_id)
    try:
        return client.list_courses()
    finally:
        client.close()


def _erp_prices(settings: Settings, item_codes: list[str], correlation_id: str) -> dict[str, dict]:
    client = ErpNextClient(os.environ.get("ERPNEXT_URL", "http://erpnext-frontend:8080"),
                           erpnext_token(settings.secrets_dir), settings.http_timeout_seconds,
                           os.environ.get("ERPNEXT_COMPANY_NAME", "OpenTraining"), correlation_id)
    try:
        return client.get_item_prices(item_codes)
    finally:
        client.close()


def get_catalog(pool: ConnectionPool, settings: Settings, partial: bool) -> tuple[list[dict], list[str]]:
    """Retourne (entrées du catalogue, sources dégradées). Lève ApiError 503 si une source manque
    et que le mode partiel n'est pas demandé."""
    start = time.perf_counter()
    with pool.connection() as conn:
        sessions = conn.execute(
            "SELECT id, capacity, remaining_seats, moodle_course_shortname, erp_item_code "
            "FROM training_session ORDER BY id"
        ).fetchall()

    correlation_id = correlation_id_var.get()
    item_codes = sorted({s["erp_item_code"] for s in sessions})
    moodle_future = _executor.submit(_moodle_courses, settings, correlation_id)
    erp_future = _executor.submit(_erp_prices, settings, item_codes, correlation_id)

    errors: dict[str, str] = {}
    courses: dict[str, dict] = {}
    prices: dict[str, dict] = {}
    try:
        courses = moodle_future.result()
    except IntegrationError as exc:
        errors["moodle"] = str(exc)
    try:
        prices = erp_future.result()
    except IntegrationError as exc:
        errors["erpnext"] = str(exc)

    log.info("catalogue agrégé", extra={"sessions": len(sessions), "durationMs":
                                        round((time.perf_counter() - start) * 1000, 1), "errors": errors})
    if errors and not partial:
        raise ApiError(503, "CATALOG_DEPENDENCY_UNAVAILABLE",
                       "Une source du catalogue est indisponible (réessayez, ou ?partial=true).", sources=errors)

    entries = []
    for s in sessions:
        course = courses.get(s["moodle_course_shortname"])
        price = prices.get(s["erp_item_code"])
        entries.append({
            "sessionId": s["id"],
            "courseName": course["fullname"] if course else None,
            "courseId": course["id"] if course else None,
            "remainingSeats": s["remaining_seats"],
            "capacity": s["capacity"],
            "price": price["price"] if price else None,
            "currency": price["currency"] if price else None,
            "moodleCourseShortname": s["moodle_course_shortname"],
            "erpItemCode": s["erp_item_code"],
        })
    return entries, sorted(errors)
