from __future__ import annotations

import json

from src.learning.self_learning_engine import SelfLearningEngine
from src.utils.db import db


def run() -> dict:
    out = {
        "status": "error",
        "cycle": {},
        "learning_examples": [],
        "suggestion_examples": [],
        "model_perf_recent": [],
    }

    engine = SelfLearningEngine(cooldown_seconds=1)
    out["cycle"] = engine.run_controlled_learning_cycle(lookback_hours=48)

    db._ensure_connected()
    if db.db is None:
        out["status"] = "partial"
        out["reason"] = "db_unavailable"
        return out

    out["learning_examples"] = list(
        db.db["learning_memory"]
        .find(
            {},
            {
                "_id": 0,
                "pattern_type": 1,
                "input_pattern": 1,
                "best_response": 1,
                "failure_case": 1,
                "improvement_hint": 1,
                "frequency": 1,
                "updated_at": 1,
            },
        )
        .sort("updated_at", -1)
        .limit(5)
    )

    out["suggestion_examples"] = list(
        db.db["self_improvement_suggestions"]
        .find(
            {},
            {
                "_id": 0,
                "suggestion_id": 1,
                "issue": 1,
                "root_cause": 1,
                "suggested_fix": 1,
                "affected_module": 1,
                "priority": 1,
                "status": 1,
                "created_at": 1,
            },
        )
        .sort("created_at", -1)
        .limit(5)
    )

    out["model_perf_recent"] = list(
        db.db["model_performance_stats"]
        .find({}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(5)
    )

    out["status"] = "success"
    return out


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
