from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ai_training.dataset_builder import DB_SOURCE_COLLECTIONS, build_datasets
from src.utils.db import db


DATASET_DIR = Path("data/ai_training/datasets")


def _db_collection_counts(database: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in DB_SOURCE_COLLECTIONS:
        try:
            out[name] = int(database[name].count_documents({}))
        except Exception:
            out[name] = -1
    return out


def run() -> dict[str, Any]:
    db._ensure_connected()
    connected = db.db is not None
    if not connected:
        return {
            "status": "error",
            "db_connected": False,
            "message": "MongoDB unavailable",
            "source_mode": "fallback_files",
            "collection_counts": {name: 0 for name in DB_SOURCE_COLLECTIONS},
            "export_counts": {},
        }

    result = build_datasets(db.db, output_dir=DATASET_DIR, limit_per_collection=50000)
    return {
        "status": str(result.get("status") or "unknown"),
        "db_connected": bool(result.get("db_connected")),
        "source_mode": str(result.get("source_mode") or "unknown"),
        "use_database_for_training": bool(result.get("use_database_for_training")),
        "collection_counts": result.get("collection_counts") or _db_collection_counts(db.db),
        "export_counts": result.get("counts") or {},
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
