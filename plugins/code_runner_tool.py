from __future__ import annotations

from typing import Any


class Tool:
    name = "plugin_code_runner"
    description = "Returns a safe execution plan for local code runs."

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        language = str(kwargs.get("language") or "python").strip().lower()
        code = str(kwargs.get("code") or "").strip()
        if not code:
            return {"status": "error", "message": "code is required"}
        return {
            "status": "success",
            "language": language,
            "preview": code[:200],
            "note": "Execution should be delegated to existing executor/agent controls.",
        }


tool = Tool()
