from __future__ import annotations

import json

from src.utils.db import db
from src.ai_training.data_migrator import CHECKPOINT_COLLECTION, CHECKPOINT_ID, LEGACY_TO_TARGET


def main() -> None:
    db._ensure_connected()
    if db.db is None:
        print(json.dumps({"status": "db_unavailable"}, indent=2))
        return

    cp = db.db[CHECKPOINT_COLLECTION].find_one({"_id": CHECKPOINT_ID}) or {}

    normalized_counts = {}
    for c in [
        "chat_logs",
        "task_logs",
        "agent_logs",
        "error_logs",
        "requirement_logs",
        "self_update_logs",
        "training_events",
        "datasets_metadata",
    ]:
        normalized_counts[c] = int(db.db[c].count_documents({}))

    legacy_remaining = {}
    for src in sorted(LEGACY_TO_TARGET.keys()):
        legacy_remaining[src] = int(db.db[src].count_documents({"migration_state": {"$ne": "normalized_v1"}}))

    migration_marked = {}
    total_migration_marked = 0
    for c in [
        "chat_logs",
        "task_logs",
        "agent_logs",
        "error_logs",
        "requirement_logs",
        "self_update_logs",
        "training_events",
    ]:
        cnt = int(db.db[c].count_documents({"migration.migrated_from": {"$exists": True}}))
        migration_marked[c] = cnt
        total_migration_marked += cnt

    print(
        json.dumps(
            {
                "checkpoint": {
                    "collection": CHECKPOINT_COLLECTION,
                    "id": CHECKPOINT_ID,
                    "status": cp.get("status"),
                    "updated_at": cp.get("updated_at"),
                    "collections": cp.get("collections", {}),
                },
                "normalized_counts": normalized_counts,
                "legacy_remaining": legacy_remaining,
                "migration_marked_counts": migration_marked,
                "total_migration_marked_docs": total_migration_marked,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
