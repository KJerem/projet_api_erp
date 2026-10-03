import json
import os
import sys
import time
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/secrets"))
CATALOG_FILE = Path(os.environ.get("CATALOG_FILE", "/config/catalog.json"))


def log(service: str, message: str, **fields) -> None:
    print(json.dumps({"level": "INFO", "service": service, "message": message, **fields}), flush=True)


def fail(service: str, message: str, **fields) -> None:
    print(json.dumps({"level": "ERROR", "service": service, "message": message, **fields}), flush=True)
    sys.exit(1)


def load_catalog() -> list[dict]:
    return json.loads(CATALOG_FILE.read_text(encoding="utf-8"))["sessions"]


def wait_until(service: str, what: str, probe, timeout: float = 900, interval: float = 5) -> None:
    """Rejoue `probe()` jusqu'à ce qu'il retourne True (les exceptions comptent comme un échec)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if probe():
                log(service, f"{what} disponible")
                return
        except Exception as exc:  # noqa: BLE001 - on attend un service qui démarre
            last = repr(exc)
        else:
            last = "pas encore prêt"
        if time.monotonic() > deadline:
            fail(service, f"timeout en attendant {what}", lastError=last)
        log(service, f"attente de {what}...", detail=last)
        time.sleep(interval)


def write_secret(name: str, content: str) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SECRETS_DIR / f"{name}.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(SECRETS_DIR / name)
