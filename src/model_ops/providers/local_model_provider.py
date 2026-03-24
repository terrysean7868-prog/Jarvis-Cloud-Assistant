from __future__ import annotations

import os


class LocalModelProvider:
    name = "local_model"

    def check_health(self) -> dict:
        # Deterministic fallback provider is always locally available in this codebase.
        return {"ok": True, "mode": "deterministic", "cwd": os.getcwd()}
