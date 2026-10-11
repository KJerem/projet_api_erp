"""Bootstrap ERPNext via son API REST officielle (aucun accès à la base MariaDB).

1. Attend que le site réponde.
2. Termine le setup wizard (société, devise, exercice) — no-op s'il est déjà fait.
3. Génère une paire api_key/api_secret pour l'Integration Hub (stockée dans le volume secrets).
4. Crée les articles de formation et leurs prix de vente (catalog.json).
"""

import json
import os
from datetime import date

import httpx

from common import SECRETS_DIR, fail, load_catalog, log, secure_secret, wait_until, write_secret

SERVICE = "erpnext-bootstrap"
BASE_URL = os.environ.get("ERPNEXT_URL", "http://erpnext-frontend:8080")
ADMIN_PASSWORD = os.environ["ERPNEXT_ADMIN_PASSWORD"]
COMPANY_NAME = os.environ.get("ERPNEXT_COMPANY_NAME", "OpenTraining")
COMPANY_ABBR = os.environ.get("ERPNEXT_COMPANY_ABBR", "OT")
SECRET_FILE = "erpnext.json"
PRICE_LIST = "Standard Selling"


def message(resp: httpx.Response):
    resp.raise_for_status()
    return resp.json().get("message")


def complete_setup_wizard() -> None:
    year = date.today().year
    with httpx.Client(base_url=BASE_URL, timeout=600) as session:
        message(session.post("/api/method/login", json={"usr": "Administrator", "pwd": ADMIN_PASSWORD}))
        log(SERVICE, "setup wizard : lancement (peut prendre 1 à 3 minutes)")
        result = message(
            session.post(
                "/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
                json={
                    "args": {
                        "language": "English",
                        "country": "France",
                        "timezone": "Europe/Paris",
                        "currency": "EUR",
                        "company_name": COMPANY_NAME,
                        "company_abbr": COMPANY_ABBR,
                        "chart_of_accounts": "Standard",
                        "fy_start_date": f"{year}-01-01",
                        "fy_end_date": f"{year}-12-31",
                        "setup_demo": 0,
                        "enable_telemetry": 0,
                    }
                },
            )
        )
        log(SERVICE, "setup wizard terminé", result=result)


def token_header(creds: dict) -> dict:
    return {"Authorization": f"token {creds['api_key']}:{creds['api_secret']}"}


def load_valid_credentials() -> dict | None:
    path = SECRETS_DIR / SECRET_FILE
    if not path.exists():
        return None
    creds = json.loads(path.read_text(encoding="utf-8"))
    resp = httpx.get(f"{BASE_URL}/api/method/frappe.auth.get_logged_user", headers=token_header(creds), timeout=10)
    return creds if resp.status_code == 200 else None


def generate_credentials() -> dict:
    with httpx.Client(base_url=BASE_URL, timeout=30) as session:
        message(session.post("/api/method/login", json={"usr": "Administrator", "pwd": ADMIN_PASSWORD}))
        keys = message(
            session.post("/api/method/frappe.core.doctype.user.user.generate_keys", json={"user": "Administrator"})
        )
    creds = {"api_key": keys["api_key"], "api_secret": keys["api_secret"]}
    resp = httpx.get(f"{BASE_URL}/api/method/frappe.auth.get_logged_user", headers=token_header(creds), timeout=10)
    resp.raise_for_status()
    write_secret(SECRET_FILE, json.dumps(creds))
    log(SERVICE, "clés API générées et stockées dans le volume secrets")
    return creds


def ensure_item(client: httpx.Client, erp: dict) -> None:
    code = erp["itemCode"]
    resp = client.get(f"/api/resource/Item/{code}")
    if resp.status_code == 404:
        message_resp = client.post(
            "/api/resource/Item",
            json={
                "item_code": code,
                "item_name": erp["itemName"],
                "item_group": "Services",
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "include_item_in_manufacturing": 0,
                "description": erp["itemName"],
            },
        )
        message_resp.raise_for_status()
        log(SERVICE, "article créé", itemCode=code)
    else:
        resp.raise_for_status()
        log(SERVICE, "article déjà présent", itemCode=code)

    filters = json.dumps([["item_code", "=", code], ["price_list", "=", PRICE_LIST]])
    prices = client.get("/api/resource/Item Price", params={"filters": filters, "fields": '["name","price_list_rate"]'})
    prices.raise_for_status()
    rows = prices.json()["data"]
    if not rows:
        client.post(
            "/api/resource/Item Price",
            json={"item_code": code, "price_list": PRICE_LIST, "price_list_rate": erp["price"]},
        ).raise_for_status()
        log(SERVICE, "prix créé", itemCode=code, price=erp["price"])
    elif float(rows[0]["price_list_rate"]) != float(erp["price"]):
        client.put(f"/api/resource/Item Price/{rows[0]['name']}", json={"price_list_rate": erp["price"]}).raise_for_status()
        log(SERVICE, "prix mis à jour", itemCode=code, price=erp["price"])


def main() -> None:
    wait_until(
        SERVICE,
        "ERPNext",
        lambda: httpx.get(f"{BASE_URL}/api/method/ping", timeout=5).json().get("message") == "pong",
    )
    complete_setup_wizard()

    creds = load_valid_credentials() or generate_credentials()
    secure_secret(SECRETS_DIR / SECRET_FILE)

    with httpx.Client(base_url=BASE_URL, headers=token_header(creds), timeout=30) as client:
        for session in load_catalog():
            ensure_item(client, session["erp"])

    log(SERVICE, "bootstrap ERPNext terminé")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        fail(SERVICE, "appel API ERPNext en échec", status=exc.response.status_code, body=exc.response.text[:2000])
