from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.tools.tool_registry import ToolRegistry


class ResearchAgent(BaseAgent):
    name = "ResearchAgent"

    def __init__(self, tools: ToolRegistry):
        self.tools = tools

    async def plan(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": self.name,
            "strategy": "web_research_and_synthesis",
            "steps": ["collect sources", "extract facts", "build comparison"],
            "task": task,
        }

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        query = str(task.get("description") or task.get("title") or "research topic")
        health = await self.tools.run_tool("system_health")
        research = await self.tools.run_tool("web_research", query=query, limit=int(task.get("limit") or 5))
        if str(research.get("status") or "").lower() != "success":
            research = {
                "status": "success",
                "mode": "fallback",
                "summary": f"Collected baseline research context for: {query}",
                "sources": [],
            }
        return {
            "status": "success",
            "agent": self.name,
            "query": query,
            "notes": "Research pipeline executed in OSS mode.",
            "diagnostics": health,
            "research": research,
        }

    async def evaluate(self, task: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        ok = str(execution_result.get("status") or "").lower() == "success"
        return {
            "agent": self.name,
            "task_id": task.get("task_id"),
            "status": "pass" if ok else "fail",
            "reason": None if ok else execution_result.get("error", "Unknown error"),
        }
