from __future__ import annotations

from typing import Any, Callable

from src.agents.base_agent import BaseAgent
from src.utils.db import db


class MonitoringAgent(BaseAgent):
    name = "MonitoringAgent"

    def __init__(self, health_check: Callable[[], dict[str, Any]] | None = None):
        self.health_check = health_check

    async def plan(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": self.name,
            "strategy": "health_and_recovery",
            "steps": ["check API health", "scan runtime errors", "trigger recovery if needed"],
            "task": task,
        }

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        health = self.health_check() if self.health_check else {"status": "unknown"}
        failed_goals = []
        recent_errors = []
        try:
            db._ensure_connected()
            if db.db is not None:
                failed_goals = list(
                    db.db["autonomy_goals"]
                    .find({"status": {"$in": ["failed", "blocked"]}}, {"goal": 1, "status": 1, "updated_at": 1})
                    .sort("updated_at", -1)
                    .limit(10)
                )
                recent_errors = list(
                    db.db["system_events"]
                    .find({"status": {"$in": ["error", "critical"]}}, {"event_type": 1, "details": 1, "timestamp": 1})
                    .sort("timestamp", -1)
                    .limit(10)
                )
                for row in failed_goals:
                    if "_id" in row:
                        row["_id"] = str(row["_id"])
        except Exception:
            pass

        return {
            "status": "success",
            "agent": self.name,
            "health": health,
            "failed_goals": failed_goals,
            "recent_errors": recent_errors,
            "restart_candidates": [g.get("goal") for g in failed_goals if isinstance(g, dict)],
        }

    async def evaluate(self, task: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        ok = str(execution_result.get("status") or "").lower() == "success"
        return {"agent": self.name, "task_id": task.get("task_id"), "status": "pass" if ok else "fail"}
