"""Catalogue agrégé GET /api/v1/catalog : agrégation synchrone Moodle + PostgreSQL + ERPNext.

A. Nominal : nom du cours (Moodle), places restantes (PostgreSQL), prix (ERPNext) corrects.
B. Moodle indisponible : 503 en mode strict, 200 dégradé avec ?partial=true.

Usage : .venv/Scripts/python scripts/test-catalog.py
"""

import time

import httpx

from _testlib import API_URL, Checks, compose


def main() -> None:
    t = Checks("Catalogue agrégé")
    api = httpx.Client(base_url=API_URL, timeout=30)

    # --- A. nominal --------------------------------------------------------------------------------
    start = time.perf_counter()
    resp = api.get("/api/v1/catalog")
    elapsed = round((time.perf_counter() - start) * 1000)
    t.check("GET /api/v1/catalog -> 200", resp.status_code == 200, f"{elapsed} ms")
    entries = {e["sessionId"]: e for e in resp.json()}
    python = entries.get("SESSION-PYTHON-01", {})
    print(f"      SESSION-PYTHON-01 : {python}")
    t.check("Moodle : nom du cours", python.get("courseName") == "Python avancé", python.get("courseName"))
    t.check("ERPNext : prix catalogue 850 EUR", python.get("price") == 850 and python.get("currency") == "EUR",
            (python.get("price"), python.get("currency")))
    seats = api.get("/api/v1/sessions/SESSION-PYTHON-01").json()["remainingSeats"]
    t.check("PostgreSQL : places restantes", python.get("remainingSeats") == seats, (python.get("remainingSeats"), seats))
    t.check("SESSION-CONCURRENCY : cours et prix 300",
            entries.get("SESSION-CONCURRENCY", {}).get("price") == 300
            and entries["SESSION-CONCURRENCY"]["courseName"] == "Concurrence et systèmes distribués")

    # --- B. Moodle indisponible --------------------------------------------------------------------
    print("\n-- B. Moodle gelé")
    compose("pause", "moodle")
    try:
        start = time.perf_counter()
        strict = api.get("/api/v1/catalog")
        elapsed = round(time.perf_counter() - start, 1)
        t.check("mode strict -> 503 CATALOG_DEPENDENCY_UNAVAILABLE",
                strict.status_code == 503 and strict.json().get("error") == "CATALOG_DEPENDENCY_UNAVAILABLE",
                strict.json().get("sources"))
        t.check("réponse bornée par le timeout (pas d'attente infinie)", elapsed < 10, f"{elapsed} s")
        partial = api.get("/api/v1/catalog", params={"partial": "true"})
        degraded = partial.json()
        t.check("?partial=true -> 200 + X-Catalog-Degraded: moodle",
                partial.status_code == 200 and partial.headers.get("x-catalog-degraded") == "moodle",
                partial.headers.get("x-catalog-degraded"))
        first = next(e for e in degraded if e["sessionId"] == "SESSION-PYTHON-01")
        t.check("mode dégradé : courseName null, prix et places toujours présents",
                first["courseName"] is None and first["price"] == 850 and first["remainingSeats"] is not None, first)
    finally:
        compose("unpause", "moodle")
    t.done()


if __name__ == "__main__":
    main()
