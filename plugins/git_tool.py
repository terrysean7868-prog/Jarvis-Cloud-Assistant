from __future__ import annotations

from typing import Any


class Tool:
    name = "plugin_git"
    description = "Creates git operation plans using local git only."

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        op = str(kwargs.get("operation") or "status").strip().lower()
        return {
            "status": "success",
            "operation": op,
            "commands": ["git status"] if op == "status" else ["git add -A", "git commit -m '<message>'"],
        }


tool = Tool()
