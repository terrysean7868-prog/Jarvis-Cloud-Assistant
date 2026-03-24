from __future__ import annotations

from urllib.request import urlopen


class OllamaProvider:
    name = "ollama"

    def check_health(self, endpoint: str = "http://127.0.0.1:11434/api/tags", timeout_s: float = 1.5) -> dict:
        try:
            with urlopen(endpoint, timeout=timeout_s) as r:
                return {"ok": 200 <= int(getattr(r, "status", 0)) < 500, "endpoint": endpoint}
        except Exception as e:
            return {"ok": False, "endpoint": endpoint, "error": str(e)}
