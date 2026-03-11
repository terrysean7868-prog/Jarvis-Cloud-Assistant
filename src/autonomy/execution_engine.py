from __future__ import annotations

from typing import Any

from src.agents.agent_controller import AgentController
from src.safety.risk_engine import RiskEngine


class ExecutionEngine:
    """Executes task graph nodes via routed agents under risk constraints."""

    def __init__(self, *, controller: AgentController, risk_engine: RiskEngine):
        self.controller = controller
        self.risk_engine = risk_engine

    async def run_node(self, node: Any) -> dict[str, Any]:
        task_payload = {
            "task_id": getattr(node, "task_id", None),
            "title": getattr(node, "title", ""),
            "description": getattr(node, "description", ""),
            "agent": (getattr(node, "metadata", {}) or {}).get("agent"),
        }

        risk = self.risk_engine.score_task(task_payload)
        if risk.level == "HIGH":
            return {
                "status": "blocked",
                "risk": {"level": risk.level, "score": risk.score, "reasons": risk.reasons},
                "requires_confirmation": False,
            }

        if risk.level == "MEDIUM":
            return {
                "status": "awaiting_confirmation",
                "risk": {"level": risk.level, "score": risk.score, "reasons": risk.reasons},
                "requires_confirmation": True,
            }

        result = await self.controller.dispatch(task_payload)
        execution = result.get("execution") if isinstance(result, dict) else {"status": "error", "error": "invalid_result"}
        return {
            "status": str((execution or {}).get("status") or "error").lower(),
            "result": result,
            "execution": execution,
            "risk": {"level": risk.level, "score": risk.score, "reasons": risk.reasons},
        }
