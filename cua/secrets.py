"""Secret store. Values come from environment variables (or a local .env that is gitignored).

Artifacts and logs only ever contain the secret's NAME. In production this would be a vault
(per-tenant operator credentials with rotation); the interface stays the same.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# secret name -> env var. Defaults are the mock app's FAKE demo operator, never real credentials.
_ENV = {
    "operator_id": ("CORELINK_OPERATOR_ID", "teller01"),
    "operator_password": ("CORELINK_OPERATOR_PASSWORD", "demo123"),
}


class SecretStore:
    def names(self) -> list[str]:
        return list(_ENV)

    def get(self, name: str) -> str:
        if name not in _ENV:
            raise KeyError(f"unknown secret '{name}'")
        env, default = _ENV[name]
        return os.environ.get(env, default)
