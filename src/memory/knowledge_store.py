from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from src.memory.vector_store import VectorStore
from src.utils.db import db


class KnowledgeStore:
    """Long-term memory for knowledge, recalls, and semantic lookup."""

    def __init__(self):
        self.vector_store = VectorStore(collection_name="jarvis_knowledge")

    def _collection(self):
        db._ensure_connected()
        if db.db is None:
            return None
        return db.db["knowledge_store"]

    def save_knowledge(self, *, topic: str, content: str, source: str, metadata: dict[str, Any] | None = None) -> str | None:
        doc = {
            "topic": topic,
            "content": content,
            "source": source,
            "metadata": metadata or {},
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        coll = self._collection()
        inserted_id = None
        if coll is not None:
            try:
                res = coll.insert_one(doc)
                inserted_id = str(res.inserted_id)
            except Exception:
                inserted_id = None

        item_id = inserted_id or f"mem-{int(datetime.now(UTC).timestamp() * 1000)}"
        try:
            self.vector_store.add(item_id=item_id, text=f"{topic}\n{content}", metadata={"source": source})
        except Exception:
            pass
        return item_id

    def semantic_search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        results = self.vector_store.query(query, k=k)
        return [
            {
                "id": r.get("id"),
                "content": r.get("text"),
                "metadata": r.get("metadata") or {},
                "score": r.get("score"),
            }
            for r in results
        ]

    def recall_conversation(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        coll = self._collection()
        rows: list[dict[str, Any]] = []

        if coll is not None:
            try:
                rows = list(
                    coll.find({"metadata.user_id": user_id})
                    .sort("created_at", -1)
                    .limit(max(1, limit))
                )
                for row in rows:
                    row["_id"] = str(row.get("_id"))
                if rows:
                    return rows
            except Exception:
                rows = []

        try:
            db._ensure_connected()
            if db.db is None:
                return []
            fallback = list(
                db.db["chat_history"]
                .find({"user_id": user_id}, {"_id": 1, "user_input": 1, "bot_response": 1, "timestamp": 1})
                .sort("timestamp", -1)
                .limit(max(1, limit))
            )
            out = []
            for row in fallback:
                out.append(
                    {
                        "_id": str(row.get("_id")),
                        "topic": "conversation",
                        "content": f"User: {row.get('user_input', '')}\nAssistant: {row.get('bot_response', '')}",
                        "source": "chat_history",
                        "created_at": row.get("timestamp"),
                    }
                )
            return out
        except Exception:
            return []
