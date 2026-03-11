from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from src.memory.knowledge_store import KnowledgeStore
from src.utils.db import db


class ReflectionEngine:
    """Post-task reflection loop to learn from outcomes."""

    def __init__(self, knowledge_store: KnowledgeStore | None = None):
        self.knowledge_store = knowledge_store or KnowledgeStore()

    def reflect(self, *, task: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
        status = str(outcome.get("status") or "unknown").lower()
        lessons: list[str] = []
        failure_reasons: list[str] = []

        if status in {"success", "completed", "ok"}:
            lessons.append("The selected agent strategy produced a successful result.")
        else:
            failure_reasons.append(str(outcome.get("error") or "Unknown failure"))
            lessons.append("Retry path should include tighter precondition checks.")

        reflection = {
            "task": task,
            "outcome": outcome,
            "status": status,
            "failure_reasons": failure_reasons,
            "lessons": lessons,
            "created_at": datetime.now(UTC),
        }

        try:
            db._ensure_connected()
            if db.db is not None:
                db.db["autonomy_reflections"].insert_one(reflection)
        except Exception:
            pass

        try:
            self.knowledge_store.save_knowledge(
                topic=f"reflection:{task.get('title') or task.get('task_id') or 'task'}",
                content="; ".join(lessons + failure_reasons),
                source="reflection_engine",
                metadata={"status": status},
            )
        except Exception:
            pass

        reflection["created_at"] = reflection["created_at"].isoformat()
        return reflection
