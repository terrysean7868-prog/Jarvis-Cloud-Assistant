from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from src.utils.db import db


class EvaluationEngine:
    """Evaluates completed autonomous goals and stores quality metrics."""

    def evaluate_goal(self, *, goal: dict[str, Any], graph: dict[str, Any], reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        nodes = list((graph or {}).get("nodes") or [])
        total_nodes = len(nodes)
        completed = len([n for n in nodes if str((n or {}).get("status") or "") == "completed"])
        failed = len([n for n in nodes if str((n or {}).get("status") or "") == "failed"])
        blocked = len([n for n in nodes if str((n or {}).get("status") or "") == "blocked"])

        success_rate = float(completed / total_nodes) if total_nodes else 0.0
        outcome = "completed" if completed == total_nodes and total_nodes > 0 else "partial"
        if failed > 0:
            outcome = "failed"
        if blocked > 0 and failed == 0:
            outcome = "blocked"

        evaluation = {
            "goal_id": str(goal.get("_id") or ""),
            "goal": str(goal.get("goal") or ""),
            "owner": str(goal.get("owner") or ""),
            "outcome": outcome,
            "success_rate": success_rate,
            "nodes_total": total_nodes,
            "nodes_completed": completed,
            "nodes_failed": failed,
            "nodes_blocked": blocked,
            "report_count": len(reports or []),
            "created_at": datetime.now(UTC),
        }

        try:
            db._ensure_connected()
            if db.db is not None:
                db.db["autonomy_evaluations"].insert_one(evaluation)
        except Exception:
            pass

        evaluation["created_at"] = evaluation["created_at"].isoformat()
        return evaluation
