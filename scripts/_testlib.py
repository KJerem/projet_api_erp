"""Petits utilitaires partagés par les scripts de test (affichage PASS/FAIL, URL de l'API)."""

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API_URL = os.environ.get("API_URL", "http://localhost:8000")


def load_env() -> dict[str, str]:
    """Lit le .env local (jamais commité) pour les identifiants d'infra utilisés par les tests."""
    values = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return {**values, **os.environ}


ENV = load_env()


def compose(*args: str, env: dict | None = None) -> str:
    """Exécute `docker compose ...` depuis la racine du projet (env : variables d'interpolation en plus)."""
    result = subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env={**os.environ, **(env or {})})
    if result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} : {result.stderr.strip()}")
    return result.stdout


def sql(query: str) -> str:
    """Requête en lecture sur la base d'intégration (psql dans le conteneur), sortie brute."""
    return compose("exec", "-T", "integration-db", "psql", "-U", ENV.get("INTEGRATION_DB_USER", "integration"),
                   "-d", ENV.get("INTEGRATION_DB_NAME", "integration"), "-At", "-c", query).strip()


def rabbit_queue(name: str) -> dict:
    """Compteurs d'une queue via l'API de management RabbitMQ."""
    port = ENV.get("RABBITMQ_MGMT_PORT", "15672")
    resp = httpx.get(f"http://localhost:{port}/api/queues/%2F/{name}",
                     auth=(ENV.get("RABBITMQ_USER", "opentraining"), ENV["RABBITMQ_PASSWORD"]), timeout=10)
    resp.raise_for_status()
    return resp.json()


def read_secret(name: str) -> str:
    """Secret généré par les bootstraps, lu dans le volume Docker (jamais copié sur disque)."""
    return compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "cat", "moodle-bootstrap",
                   f"/secrets/{name}").strip()


class Externals:
    """Vérifications indépendantes, directement dans Moodle et ERPNext via leurs API officielles."""

    def __init__(self) -> None:
        import json as _json
        creds = _json.loads(read_secret("erpnext.json"))
        self.erp = httpx.Client(base_url=f"http://localhost:{ENV.get('ERPNEXT_PORT', '8080')}", timeout=30,
                                headers={"Authorization": f"token {creds['api_key']}:{creds['api_secret']}"})
        self.moodle_url = f"http://localhost:{ENV.get('MOODLE_PORT', '8081')}/webservice/rest/server.php"
        self.moodle_token = read_secret("moodle_token")

    def moodle(self, function: str, **params):
        return httpx.post(self.moodle_url, data={"wstoken": self.moodle_token, "wsfunction": function,
                                                 "moodlewsrestformat": "json", **params}, timeout=30).json()

    def moodle_users(self, email: str) -> list[dict]:
        return self.moodle("core_user_get_users_by_field", field="email", **{"values[0]": email})

    def moodle_course_ids(self, user_id: int) -> list[int]:
        return [c["id"] for c in self.moodle("core_enrol_get_users_courses", userid=user_id)]

    def erp_list(self, doctype: str, filters: list, fields: list[str] | None = None) -> list[dict]:
        import json as _json
        resp = self.erp.get(f"/api/resource/{doctype}", params={
            "filters": _json.dumps(filters), "fields": _json.dumps(fields or ["name"]), "limit_page_length": 100})
        resp.raise_for_status()
        return resp.json()["data"]


def publish_raw(routing_key: str, body: bytes, exchange: str = "si.events", headers: dict | None = None,
                message_id: str | None = None) -> None:
    """Publie un message brut dans RabbitMQ (simulation de redélivrance / message invalide)."""
    import pika
    params = pika.ConnectionParameters(
        host="localhost", port=int(ENV.get("RABBITMQ_PORT", "5672")),
        credentials=pika.PlainCredentials(ENV.get("RABBITMQ_USER", "opentraining"), ENV["RABBITMQ_PASSWORD"]))
    with pika.BlockingConnection(params) as conn:
        channel = conn.channel()
        channel.confirm_delivery()
        channel.basic_publish(exchange, routing_key, body, mandatory=True, properties=pika.BasicProperties(
            content_type="application/json", delivery_mode=2, message_id=message_id, headers=headers or {}))


def outbox_event(registration_id: str, event_type: str = "registration.accepted") -> str:
    """Enveloppe JSON de l'événement tel qu'écrit dans l'outbox."""
    return sql(f"SELECT payload::text FROM outbox_event WHERE aggregate_id = '{registration_id}' "
               f"AND event_type = '{event_type}'")


def wait_status(client: httpx.Client, registration_id: str, statuses: set[str], timeout: float = 90) -> dict:
    """Interroge GET /registrations/{id} jusqu'à atteindre un des statuts ; retourne la vue + statuts vus."""
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    view: dict = {}
    while time.monotonic() < deadline:
        view = client.get(f"/api/v1/registrations/{registration_id}").json()
        if not seen or seen[-1] != view["status"]:
            seen.append(view["status"])
        if view["status"] in statuses:
            break
        time.sleep(0.2)
    view["observedStatuses"] = seen
    return view


def wait_for(predicate, timeout: float = 30, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - service en cours de (re)démarrage
            pass
        time.sleep(interval)
    return False

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class Checks:
    def __init__(self, title: str) -> None:
        self.failures = 0
        print(f"=== {title} ===")

    def check(self, label: str, condition: bool, detail: object = "") -> bool:
        print(f"{'PASS' if condition else 'FAIL'}  {label}" + (f"  -> {detail}" if detail != "" else ""))
        if not condition:
            self.failures += 1
        return condition

    def done(self) -> None:
        print(f"\nRésultat : {'OK' if self.failures == 0 else f'{self.failures} échec(s)'}")
        sys.exit(1 if self.failures else 0)
