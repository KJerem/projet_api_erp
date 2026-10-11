"""Génère un fichier .env à partir de .env.example en remplaçant les "change-me" par des secrets aléatoires.

Usage : python scripts/init-env.py [--force]
"""

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"


def main() -> int:
    if TARGET.exists() and "--force" not in sys.argv:
        print(f"{TARGET} existe déjà (utiliser --force pour l'écraser).")
        return 1

    lines = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip().endswith("=change-me"):
            key = line.split("=", 1)[0]
            line = f"{key}={secrets.token_urlsafe(18)}"
        lines.append(line)

    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{TARGET} généré.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
