from __future__ import annotations

import json
from pathlib import Path

from src.ai_training.data_migrator import migrate_legacy_collections
from src.ai_training.dataset_builder import build_datasets
from src.core.llm_adapter import LLMAdapter
from src.model_ops import (
    check_health,
    list_models,
    recommend_with_mode,
    run_benchmark,
    compute_readiness,
    inspect_dataset,
)
from src.utils.db import db


def run() -> dict:
    out: dict = {
        "db": {},
        "migration": {},
        "datasets": {},
        "model_ops": {},
        "routing": {},
    }

    try:
        db._ensure_connected()
    except Exception as e:
        out["db"]["connect_error"] = str(e)

    out["db"]["connected"] = db.db is not None

    if db.db is not None:
        out["migration"] = migrate_legacy_collections(
            db.db,
            mark_source=False,
            limit_per_collection=120,
            dry_run=True,
        )
        out["datasets"] = build_datasets(db.db, output_dir=Path("data/ai_training/datasets"), limit_per_collection=600)
    else:
        out["migration"] = {"status": "skipped", "reason": "db_unavailable"}
        out["datasets"] = {"status": "skipped", "reason": "db_unavailable"}

    stats = inspect_dataset("data/ai_training/datasets")
    readiness = compute_readiness(stats, model_supports_finetune=True)
    benchmark = run_benchmark("local_primary_api_backup")

    out["model_ops"] = {
        "models": len(list_models()),
        "health": check_health(),
        "recommend_hybrid": recommend_with_mode("hybrid", {"ram_gb": 16}),
        "readiness": readiness,
        "benchmark_summary": benchmark.get("summary"),
    }

    adapter = LLMAdapter()
    out["routing"] = {
        "enabled": bool(getattr(adapter, "model_ops_routing_enabled", False)),
        "simple_chat": adapter._resolve_model_ops_route("hello there", "chat"),
        "debug_query": adapter._resolve_model_ops_route("debug this traceback", "chat"),
        "project_query": adapter._resolve_model_ops_route("analyze this repository architecture", "chat"),
    }

    return out


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
