#!/usr/bin/env bash
# Démo du happy path (tests 1, 2, 12) — utilise le venv du projet.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python; [ -x "$PY" ] || PY=.venv/bin/python
exec "$PY" scripts/test-happy-path.py
