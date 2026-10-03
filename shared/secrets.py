"""Lecture des credentials générés par les bootstraps (volume Docker `integration-secrets`).

Relus à chaque utilisation : une rotation des clés n'impose pas de redémarrer les workers.
"""

import json
from pathlib import Path

from shared.errors import TransientError


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        # Bootstrap pas encore terminé : on retentera plus tard.
        raise TransientError(f"secret absent : {path.name}") from exc


def erpnext_token(secrets_dir: Path) -> str:
    creds = json.loads(_read(secrets_dir / "erpnext.json"))
    return f"token {creds['api_key']}:{creds['api_secret']}"


def moodle_token(secrets_dir: Path) -> str:
    return _read(secrets_dir / "moodle_token")
