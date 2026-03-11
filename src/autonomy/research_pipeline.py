from __future__ import annotations

from typing import Any

from src.memory.knowledge_store import KnowledgeStore
from src.tools.tool_registry import ToolRegistry


class ResearchPipeline:
    """Autonomous research loop: search -> summarize -> store -> synthesize."""

    def __init__(self, *, tools: ToolRegistry, knowledge_store: KnowledgeStore):
        self.tools = tools
        self.knowledge_store = knowledge_store

    async def run(self, *, topic: str, owner: str = "system", max_sources: int = 5) -> dict[str, Any]:
        topic = (topic or "").strip()
        if not topic:
            return {"status": "error", "message": "topic is required"}

        search = await self.tools.run_tool("web_research", query=topic, limit=max(1, min(max_sources, 10)))
        if str(search.get("status") or "") != "success":
            return {
                "status": "error",
                "topic": topic,
                "message": str(search.get("message") or "research search failed"),
            }

        results = list(search.get("results") or [])
        summary_lines = []
        stored_ids = []
        for item in results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if not title and not url:
                continue

            summary_lines.append(f"- {title} ({url})")
            mem_id = self.knowledge_store.save_knowledge(
                topic=f"research:{topic}",
                content=f"Title: {title}\nURL: {url}",
                source="research_pipeline",
                metadata={"user_id": owner, "topic": topic, "url": url},
            )
            if mem_id:
                stored_ids.append(mem_id)

        synthesized = "\n".join(summary_lines) if summary_lines else f"No useful sources found for: {topic}"
        return {
            "status": "success",
            "topic": topic,
            "owner": owner,
            "sources": results,
            "knowledge_ids": stored_ids,
            "report": {
                "title": f"Research Report: {topic}",
                "summary": synthesized,
            },
        }
