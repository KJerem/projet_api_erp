"""Client de l'API REST Frappe/ERPNext (CRM : Lead, Customer — ERP : Sales Order).

Seules les routes officielles /api/resource/{DocType} sont utilisées (GET, POST, PUT).
"""

import json
import logging
import re
import time
from datetime import date

import httpx

from shared.errors import PermanentError, TransientError

log = logging.getLogger("erpnext")

PRICE_LIST = "Standard Selling"
CUSTOMER_GROUP = "Commercial"
TERRITORY = "France"


def _frappe_message(resp: httpx.Response) -> str:
    """Extrait un message lisible d'une erreur Frappe (exc_type + _server_messages)."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    parts = [body.get("exc_type") or ""]
    raw = body.get("_server_messages")
    if raw:
        try:
            messages = [json.loads(m).get("message", "") for m in json.loads(raw)]
            parts.append(re.sub(r"<[^>]+>", " ", " ".join(messages)))
        except (ValueError, AttributeError):
            pass
    return " ".join(p for p in parts if p).strip()[:500] or resp.text[:300]


class ErpNextClient:
    def __init__(self, base_url: str, auth_header: str, timeout: float, company: str,
                 correlation_id: str | None = None) -> None:
        headers = {"Authorization": auth_header, "Accept": "application/json"}
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id  # visible dans les logs nginx/Frappe
        self.company = company
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    # -- transport + classification des erreurs ---------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict:
        start = time.perf_counter()
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientError(f"ERPNext timeout ({method} {path})") from exc
        except httpx.TransportError as exc:
            raise TransientError(f"ERPNext injoignable ({method} {path}) : {exc!r}") from exc
        finally:
            latency = round((time.perf_counter() - start) * 1000, 1)
        log.info("appel ERPNext", extra={"method": method, "path": path,
                                         "status": resp.status_code, "latencyMs": latency})
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"ERPNext HTTP {resp.status_code} ({method} {path}) : {_frappe_message(resp)}")
        if resp.status_code >= 400:
            raise PermanentError(f"ERPNext HTTP {resp.status_code} ({method} {path}) : {_frappe_message(resp)}")
        return resp.json()

    def _find(self, doctype: str, filters: list, fields: list[str] | None = None) -> list[dict]:
        return self._request(
            "GET",
            f"/api/resource/{doctype}",
            params={"filters": json.dumps(filters), "fields": json.dumps(fields or ["name"]),
                    "limit_page_length": 5, "order_by": "creation asc"},
        )["data"]

    def _create(self, doctype: str, doc: dict) -> dict:
        return self._request("POST", f"/api/resource/{doctype}", json=doc)["data"]

    # -- CRM -----------------------------------------------------------------------------------------
    def find_lead(self, email: str) -> str | None:
        rows = self._find("Lead", [["email_id", "=", email]])
        return rows[0]["name"] if rows else None

    def create_lead(self, email: str, first_name: str, last_name: str, company_name: str) -> str:
        return self._create("Lead", {"first_name": first_name, "last_name": last_name,
                                     "email_id": email, "company_name": company_name})["name"]

    def find_customer(self, customer_name: str) -> str | None:
        rows = self._find("Customer", [["customer_name", "=", customer_name]])
        return rows[0]["name"] if rows else None

    def create_customer(self, customer_name: str, lead_name: str | None) -> str:
        doc = {"customer_name": customer_name, "customer_type": "Company",
               "customer_group": CUSTOMER_GROUP, "territory": TERRITORY}
        if lead_name:
            doc["lead_name"] = lead_name
        return self._create("Customer", doc)["name"]

    # -- ERP -----------------------------------------------------------------------------------------
    def find_sales_order(self, registration_id: str) -> dict | None:
        """Le Sales Order porte l'identifiant d'inscription dans po_no : on le retrouve avant de recréer."""
        rows = self._find("Sales Order", [["po_no", "=", registration_id], ["docstatus", "<", 2]],
                          ["name", "docstatus"])
        return rows[0] if rows else None

    def create_sales_order(self, customer: str, item_code: str, registration_id: str) -> dict:
        today = date.today().isoformat()
        doc = self._create("Sales Order", {
            "customer": customer,
            "company": self.company,
            "transaction_date": today,
            "delivery_date": today,
            "po_no": registration_id,
            "selling_price_list": PRICE_LIST,
            "items": [{"item_code": item_code, "qty": 1, "delivery_date": today}],
        })
        return {"name": doc["name"], "docstatus": doc["docstatus"]}

    def submit_sales_order(self, name: str) -> None:
        self._request("PUT", f"/api/resource/Sales Order/{name}", json={"docstatus": 1})

    def get_item_price(self, item_code: str) -> float | None:
        rows = self._find("Item Price", [["item_code", "=", item_code], ["price_list", "=", PRICE_LIST]],
                          ["price_list_rate", "currency"])
        return float(rows[0]["price_list_rate"]) if rows else None
