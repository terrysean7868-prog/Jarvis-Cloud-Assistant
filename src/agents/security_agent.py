from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.safety.risk_engine import RiskEngine


class SecurityAgent(BaseAgent):
    name = "SecurityAgent"

    def __init__(self, risk_engine: RiskEngine | None = None):
        self.risk_engine = risk_engine or RiskEngine()

    async def plan(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": self.name,
            "strategy": "risk_audit",
            "steps": ["inspect requested actions", "score risk", "recommend safe decision"],
            "task": task,
        }

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        action = task.get("action") if isinstance(task.get("action"), dict) else {
            "type": task.get("action_type") or task.get("agent") or "agent_task",
            "command": task.get("command") or "",
        }
        score = self.risk_engine.score_action(action)
        return {
            "status": "success",
            "agent": self.name,
            "risk": {"score": score.score, "level": score.level, "reasons": score.reasons},
            "decision": self.risk_engine.decision_for(action),
        }

    async def evaluate(self, task: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        ok = str(execution_result.get("status") or "").lower() == "success"
        return {"agent": self.name, "task_id": task.get("task_id"), "status": "pass" if ok else "fail"}
