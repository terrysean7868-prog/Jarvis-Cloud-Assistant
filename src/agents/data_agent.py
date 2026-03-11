from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.memory.knowledge_store import KnowledgeStore


class DataAgent(BaseAgent):
    name = "DataAgent"

    def __init__(self, knowledge_store: KnowledgeStore | None = None):
        self.knowledge_store = knowledge_store or KnowledgeStore()

    async def plan(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": self.name,
            "strategy": "data_memory_retrieval",
            "steps": ["identify data intent", "query memory", "return structured insights"],
            "task": task,
        }

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        query = str(task.get("query") or task.get("description") or task.get("title") or "").strip()
        if not query:
            return {"status": "error", "agent": self.name, "error": "Missing query"}
        hits = self.knowledge_store.semantic_search(query, k=int(task.get("k") or 5))
        return {"status": "success", "agent": self.name, "query": query, "results": hits}

    async def evaluate(self, task: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        ok = str(execution_result.get("status") or "").lower() == "success"
        return {"agent": self.name, "task_id": task.get("task_id"), "status": "pass" if ok else "fail"}
