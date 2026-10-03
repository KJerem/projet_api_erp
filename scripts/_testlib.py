"""Petits utilitaires partagés par les scripts de test (affichage PASS/FAIL, URL de l'API)."""

import os
import sys

API_URL = os.environ.get("API_URL", "http://localhost:8000")

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
