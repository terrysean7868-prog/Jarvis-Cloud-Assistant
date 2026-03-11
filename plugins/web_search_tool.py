from __future__ import annotations

from typing import Any


class Tool:
    name = "plugin_web_search"
    description = "Plugin facade for web search using built-in registry tools."

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return {"status": "error", "message": "query is required"}
        return {
            "status": "success",
            "query": query,
            "note": "Use built-in web_research tool for full source retrieval.",
        }


tool = Tool()
