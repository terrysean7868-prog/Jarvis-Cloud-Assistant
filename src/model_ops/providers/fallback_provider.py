from __future__ import annotations


class FallbackProvider:
    name = "fallback"

    def check_health(self) -> dict:
        return {"ok": True, "mode": "always_available"}
