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


def compose(*args: str) -> str:
    """Exécute `docker compose ...` depuis la racine du projet."""
    result = subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
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
