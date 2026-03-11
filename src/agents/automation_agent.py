from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent


class AutomationAgent(BaseAgent):
    name = "AutomationAgent"

    async def plan(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": self.name,
            "strategy": "workflow_automation",
            "steps": ["identify trigger", "prepare workflow payload", "dispatch automation"],
            "task": task,
        }

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "success",
            "agent": self.name,
            "notes": "Automation workflow prepared (n8n-ready payload path).",
            "payload": task,
        }

    async def evaluate(self, task: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        ok = str(execution_result.get("status") or "").lower() == "success"
        return {"agent": self.name, "task_id": task.get("task_id"), "status": "pass" if ok else "fail"}
