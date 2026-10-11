"""Configuration commune de la suite de tests unitaires.

Tests UNITAIRES : aucun conteneur Docker, aucun accès réseau, aucune base de données.
Les appels HTTP vers Moodle/ERPNext passent par httpx.MockTransport, la base et RabbitMQ
sont remplacés par des doublures.

Lancement (depuis la racine du dépôt, dans le venv du projet) :
    .venv/Scripts/python -m pytest tests -q      (Windows)
    .venv/bin/python -m pytest tests -q          (Linux/macOS)
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# `shared` et `workers` sont importés depuis la racine, `app` depuis integration-api/ (comme dans l'image).
for path in (ROOT, ROOT / "integration-api"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# Variables exigées par shared/config.py (_require). Valeurs factices, propres aux tests.
os.environ.setdefault("INTEGRATION_DB_PASSWORD", "unit-test-db-password")
os.environ.setdefault("RABBITMQ_PASSWORD", "unit-test-rabbit-password")
