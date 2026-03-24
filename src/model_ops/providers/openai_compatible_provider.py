from __future__ import annotations

from urllib.request import Request, urlopen


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def check_health(self, endpoint: str, api_key: str | None = None, timeout_s: float = 1.5) -> dict:
        try:
            if not endpoint:
                return {"ok": False, "error": "missing_endpoint"}
            req = Request(endpoint, method="GET")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with urlopen(req, timeout=timeout_s) as r:
                code = int(getattr(r, "status", 0))
                return {"ok": code in {200, 401, 403, 404}, "status": code, "endpoint": endpoint}
        except Exception as e:
            return {"ok": False, "endpoint": endpoint, "error": str(e)}
