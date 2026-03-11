from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from bson import ObjectId

from src.utils.db import db


class GoalManager:
    """Mongo-backed autonomous goal lifecycle manager."""

    def __init__(self):
        self._in_memory_goals: dict[str, dict[str, Any]] = {}
        self._ensure_indexes()

    def _collection(self):
        db._ensure_connected()
        if db.db is None:
            return None
        return db.db["autonomy_goals"]

    def _ensure_indexes(self) -> None:
        coll = self._collection()
        if coll is None:
            return
        try:
            coll.create_index("status")
            coll.create_index("priority")
            coll.create_index("updated_at")
            coll.create_index([("owner", 1), ("status", 1)])
        except Exception:
            return

    def create_goal(self, *, goal: str, owner: str = "system", priority: int = 5, metadata: dict[str, Any] | None = None) -> str:
        payload = {
            "goal": goal,
            "owner": owner,
            "priority": max(1, min(10, int(priority))),
            "status": "pending",
            "metadata": metadata or {},
            "reports": [],
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "last_error": None,
        }

        coll = self._collection()
        if coll is not None:
            try:
                res = coll.insert_one(payload)
                return str(res.inserted_id)
            except Exception:
                pass

        goal_id = str(ObjectId())
        payload["_id"] = goal_id
        self._in_memory_goals[goal_id] = payload
        return goal_id

    def list_goals(self, *, statuses: list[str] | None = None, limit: int = 25) -> list[dict[str, Any]]:
        statuses = statuses or []
        coll = self._collection()

        if coll is not None:
            query: dict[str, Any] = {}
            if statuses:
                query["status"] = {"$in": statuses}
            try:
                rows = list(coll.find(query).sort("updated_at", -1).limit(max(1, limit)))
                for row in rows:
                    row["_id"] = str(row.get("_id"))
                return rows
            except Exception:
                pass

        rows = list(self._in_memory_goals.values())
        if statuses:
            rows = [r for r in rows if r.get("status") in statuses]
        rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
        return rows[: max(1, limit)]

    def get_goal(self, goal_id: str) -> dict[str, Any] | None:
        coll = self._collection()
        if coll is not None:
            try:
                oid = ObjectId(goal_id)
                row = coll.find_one({"_id": oid})
                if row:
                    row["_id"] = str(row.get("_id"))
                return row
            except Exception:
                pass
        return self._in_memory_goals.get(goal_id)

    def update_goal_status(self, goal_id: str, status: str, *, last_error: str | None = None) -> bool:
        coll = self._collection()
        now = datetime.now(UTC)

        if coll is not None:
            try:
                oid = ObjectId(goal_id)
                res = coll.update_one(
                    {"_id": oid},
                    {
                        "$set": {
                            "status": status,
                            "updated_at": now,
                            "last_error": last_error,
                        }
                    },
                )
                return bool(res.modified_count or res.matched_count)
            except Exception:
                pass

        row = self._in_memory_goals.get(goal_id)
        if row is None:
            return False
        row["status"] = status
        row["updated_at"] = now
        row["last_error"] = last_error
        return True

    def append_report(self, goal_id: str, report: dict[str, Any]) -> bool:
        coll = self._collection()
        report = {**report, "at": datetime.now(UTC).isoformat()}
        if coll is not None:
            try:
                oid = ObjectId(goal_id)
                res = coll.update_one(
                    {"_id": oid},
                    {"$push": {"reports": report}, "$set": {"updated_at": datetime.now(UTC)}},
                )
                return bool(res.modified_count or res.matched_count)
            except Exception:
                pass

        row = self._in_memory_goals.get(goal_id)
        if row is None:
            return False
        row.setdefault("reports", []).append(report)
        row["updated_at"] = datetime.now(UTC)
        return True
