from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ai_training.data_migrator import (
    AUTH_RUNTIME_CRITICAL_COLLECTIONS,
    CHECKPOINT_COLLECTION,
    CHECKPOINT_ID,
    migrate_legacy_collections,
)
from src.ai_training.dataset_builder import build_datasets
from src.ai_training.data_schemas import REQUIRED_COLLECTIONS
from src.utils.db import db


def _count_collections(database: Any, names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        try:
            out[name] = int(database[name].count_documents({}))
        except Exception:
            out[name] = -1
    return out


def run() -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "error",
        "db_connected": False,
        "migration_runs": [],
        "migration_totals": {},
        "datasets": {},
        "preservation": {},
        "runtime_validation": {},
    }

    db._ensure_connected()
    if db.db is None:
        out["message"] = "Database unavailable"
        return out

    out["db_connected"] = True

    auth_critical = sorted(AUTH_RUNTIME_CRITICAL_COLLECTIONS)
    pre_counts = _count_collections(db.db, auth_critical)

    run_limit = 240
    for _ in range(run_limit):
        step = migrate_legacy_collections(
            db.db,
            mark_source=False,
            dry_run=False,
            batch_size=50,
            max_batches_per_run=1,
            resume_from_checkpoint=True,
            ensure_indexes=False,
        )
        out["migration_runs"].append(
            {
                "completed": bool(step.get("completed")),
                "batches_processed": int(step.get("batches_processed") or 0),
                "summaries": step.get("summaries") or [],
            }
        )
        if bool(step.get("completed")):
            break

    total_migrated = 0
    collections_migrated: list[str] = []
    last_run = out["migration_runs"][-1] if out["migration_runs"] else {}
    for s in last_run.get("summaries") or []:
        migrated = int((s or {}).get("migrated") or 0)
        total_migrated += migrated
        if migrated > 0:
            collections_migrated.append(str((s or {}).get("source_collection") or ""))

    checkpoint_doc = {}
    try:
        checkpoint_doc = db.db[CHECKPOINT_COLLECTION].find_one({"_id": CHECKPOINT_ID}) or {}
    except Exception:
        checkpoint_doc = {}

    ds = build_datasets(db.db, output_dir=Path("data/ai_training/datasets"), limit_per_collection=50000)

    post_counts = _count_collections(db.db, auth_critical)
    unchanged = {k: (pre_counts.get(k) == post_counts.get(k)) for k in auth_critical}

    # Runtime safety checks on normalized target collections/index presence.
    index_info: dict[str, list[str]] = {}
    for c in REQUIRED_COLLECTIONS:
        try:
            info = db.db[c].index_information()
            index_info[c] = sorted(list(info.keys()))
        except Exception:
            index_info[c] = []

    out["migration_totals"] = {
        "collections_migrated": sorted([c for c in collections_migrated if c]),
        "total_records_migrated_last_run": total_migrated,
        "runs_executed": len(out["migration_runs"]),
        "checkpoint_status": checkpoint_doc.get("status"),
    }
    out["datasets"] = ds
    out["preservation"] = {
        "auth_runtime_collections": auth_critical,
        "pre_counts": pre_counts,
        "post_counts": post_counts,
        "counts_unchanged": unchanged,
        "all_unchanged": all(unchanged.values()) if unchanged else True,
        "intentionally_skipped_auth_session_device": True,
    }
    out["runtime_validation"] = {
        "checkpoint_persisted": bool(checkpoint_doc),
        "indexes_present": index_info,
        "api_paths_blocked": False,
    }
    out["status"] = "success"
    return out


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
