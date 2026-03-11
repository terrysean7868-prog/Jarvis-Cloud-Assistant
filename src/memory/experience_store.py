from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from src.memory.vector_store import VectorStore
from src.utils.db import db


class ExperienceStore:
    """Stores execution experiences for retrieval during future autonomous runs."""

    def __init__(self):
        self.vectors = VectorStore(collection_name="jarvis_experiences")

    def add_experience(self, *, task_type: str, input_text: str, outcome: str, details: dict[str, Any] | None = None) -> str:
        payload = {
            "task_type": task_type,
            "input_text": input_text,
            "outcome": outcome,
            "details": details or {},
            "created_at": datetime.now(UTC),
        }

        exp_id = f"exp-{int(datetime.now(UTC).timestamp() * 1000)}"
        try:
            db._ensure_connected()
            if db.db is not None:
                res = db.db["experience_store"].insert_one(payload)
                exp_id = str(res.inserted_id)
        except Exception:
            pass

        try:
            self.vectors.add(
                item_id=exp_id,
                text=f"{task_type}\n{input_text}\n{outcome}",
                metadata={"task_type": task_type, "outcome": outcome},
            )
        except Exception:
            pass

        return exp_id

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        return self.vectors.query(query, k=k)
