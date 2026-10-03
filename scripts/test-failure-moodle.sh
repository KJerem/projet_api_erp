#!/usr/bin/env bash
# Test 6 : Moodle indisponible (retry, pas de perte, état visible) — utilise le venv du projet.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python; [ -x "$PY" ] || PY=.venv/bin/python
exec "$PY" scripts/test-failure-moodle.py
