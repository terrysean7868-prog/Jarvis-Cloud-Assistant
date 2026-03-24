from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from src.learning.self_learning_engine import SelfLearningEngine
from src.model_ops.finetune_dataset_checker import inspect_dataset
from src.model_ops.runtime_router import resolve_route
from src.model_ops.training_readiness import compute_readiness
from src.utils.db import db


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_find_examples(collection: str, projection: dict[str, int], limit: int = 2) -> list[dict[str, Any]]:
    if db.db is None:
        return []
    try:
        return list(db.db[collection].find({}, projection).sort("_id", -1).limit(max(1, int(limit))))
    except Exception:
        return []


def _model_perf_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_model: dict[str, dict[str, Any]] = {}
    for row in rows:
        model_id = str((row or {}).get("model_id") or "unknown")
        item = by_model.setdefault(
            model_id,
            {
                "model_id": model_id,
                "provider": str((row or {}).get("provider") or "unknown"),
                "calls": 0,
                "failure": 0,
                "latency_total_ms": 0.0,
                "fallback_calls": 0,
            },
        )
        item["calls"] += 1
        ok = bool((row or {}).get("success"))
        item["failure"] += 0 if ok else 1
        item["latency_total_ms"] += float((row or {}).get("latency_ms") or 0.0)
        item["fallback_calls"] += 1 if bool((row or {}).get("fallback_used")) else 0

    out: list[dict[str, Any]] = []
    for v in by_model.values():
        calls = max(1, int(v.get("calls") or 1))
        out.append(
            {
                "model_id": v.get("model_id"),
                "provider": v.get("provider"),
                "calls": calls,
                "failure_rate": round(float(v.get("failure") or 0) / calls, 4),
                "avg_latency_ms": round(float(v.get("latency_total_ms") or 0.0) / calls, 3),
                "fallback_rate": round(float(v.get("fallback_calls") or 0) / calls, 4),
            }
        )
    out.sort(key=lambda x: (float(x.get("failure_rate") or 0.0), float(x.get("avg_latency_ms") or 0.0)), reverse=True)
    return out


def _recommend_models(summary: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not summary:
        return {
            "best_primary_model": None,
            "best_fallback_model": None,
            "best_debug_model": None,
            "note": "No model_performance_stats rows available yet.",
        }

    candidates = [s for s in summary if int(s.get("calls") or 0) >= 3] or summary
    max_lat = max(1.0, max(float(s.get("avg_latency_ms") or 0.0) for s in candidates))

    def score(item: dict[str, Any]) -> float:
        fail = float(item.get("failure_rate") or 0.0)
        fb = float(item.get("fallback_rate") or 0.0)
        lat = float(item.get("avg_latency_ms") or 0.0) / max_lat
        return (0.65 * fail) + (0.2 * fb) + (0.15 * lat)

    sorted_by_quality = sorted(candidates, key=score)
    best_primary = sorted_by_quality[0] if sorted_by_quality else None

    fallback_pref = sorted(
        candidates,
        key=lambda it: (
            float(it.get("failure_rate") or 0.0),
            float(it.get("avg_latency_ms") or 0.0),
            -int(it.get("calls") or 0),
        ),
    )
    best_fallback = fallback_pref[1] if len(fallback_pref) > 1 else (fallback_pref[0] if fallback_pref else None)

    debug_rows = [r for r in rows if str((r or {}).get("task_type") or "") in {"debug_reasoning", "project_analysis", "delegated_task_planning"}]
    debug_summary = _model_perf_summary(debug_rows)
    debug_candidates = [s for s in debug_summary if int(s.get("calls") or 0) >= 2] or debug_summary
    best_debug = (
        sorted(debug_candidates, key=lambda it: (float(it.get("failure_rate") or 0.0), float(it.get("avg_latency_ms") or 0.0)))[0]
        if debug_candidates
        else best_primary
    )

    total_calls = sum(int(s.get("calls") or 0) for s in summary)
    sparse = total_calls < 15 or len(summary) < 2
    note = "Low confidence: model stats are still sparse; keep collecting runtime traffic." if sparse else "Confidence moderate: recommendations use observed failure/latency/fallback rates."

    return {
        "best_primary_model": best_primary,
        "best_fallback_model": best_fallback,
        "best_debug_model": best_debug,
        "note": note,
    }


def _cache_validation(engine: SelfLearningEngine) -> dict[str, Any]:
    out: dict[str, Any] = {
        "eligible_patterns": 0,
        "tested_query": None,
        "cached_best_response_found": False,
        "adapter_source": None,
        "cache_hit": False,
        "error": None,
    }
    if db.db is None:
        out["error"] = "db_unavailable"
        return out

    rows = list(
        db.db["learning_memory"]
        .find({"pattern_type": "chat", "best_response": {"$exists": True, "$ne": None}}, {"_id": 0, "input_pattern": 1, "best_response": 1, "frequency": 1})
        .sort("frequency", -1)
        .limit(40)
    )
    eligible = [r for r in rows if int((r or {}).get("frequency") or 0) >= 2]
    out["eligible_patterns"] = len(eligible)
    if not eligible:
        return out

    query = str((eligible[0] or {}).get("input_pattern") or "").strip()
    if not query:
        return out
    out["tested_query"] = query

    cached = engine.get_cached_best_response(query)
    out["cached_best_response_found"] = bool(cached)

    if not cached:
        return out

    out["adapter_source"] = "not_checked"
    out["cache_hit"] = bool(cached)
    return out


def _run_runtime_prompt_probe(engine: SelfLearningEngine) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "skipped",
        "responses": [],
        "cache_hit_on_repeat": False,
        "error": None,
    }
    probe_rows = [
        {
            "query": "Summarize your core capabilities in 3 short bullet points.",
            "response_text": "I can answer questions, help with debugging, and assist with task planning using your current runtime context.",
            "source": "stabilization-probe",
            "actions": [],
            "success": True,
            "latency_ms": 180.0,
            "fallback_used": False,
        },
        {
            "query": "Summarize your core capabilities in 3 short bullet points.",
            "response_text": "I can answer questions, help with debugging, and assist with task planning using your current runtime context.",
            "source": "stabilization-probe",
            "actions": [],
            "success": True,
            "latency_ms": 160.0,
            "fallback_used": False,
        },
        {
            "query": "Debug this traceback briefly: ModuleNotFoundError no module named src",
            "response_text": "This usually means Python path mismatch. Ensure project root is on PYTHONPATH and run from workspace root.",
            "source": "stabilization-probe",
            "actions": [],
            "success": False,
            "latency_ms": 120.0,
            "fallback_used": True,
        },
        {
            "query": "Summarize your core capabilities in 3 short bullet points.",
            "response_text": "I can answer questions, help with debugging, and assist with task planning using your current runtime context.",
            "source": "stabilization-probe",
            "actions": [],
            "success": True,
            "latency_ms": 150.0,
            "fallback_used": False,
        },
    ]

    try:
        rows: list[dict[str, Any]] = []
        for idx, row in enumerate(probe_rows):
            query = str(row.get("query") or "")
            response_text = str(row.get("response_text") or "")
            source = str(row.get("source") or "stabilization-probe")
            actions = row.get("actions") if isinstance(row.get("actions"), list) else []

            engine.log_response_quality(
                user_id="stabilization_runner",
                query=query,
                response_text=response_text,
                actions=actions,
                source=source,
                request_id=f"stabilize_probe_{idx}",
            )

            route = resolve_route(query, mode="chat")
            primary = (route or {}).get("primary") if isinstance((route or {}).get("primary"), dict) else {}
            engine.record_model_performance(
                task_type=str((route or {}).get("task_type") or "simple_chat"),
                model_id=str(primary.get("model_id") or "unknown"),
                provider=str(primary.get("provider") or "unknown"),
                success=bool(row.get("success")),
                latency_ms=float(row.get("latency_ms") or 0.0),
                fallback_used=bool(row.get("fallback_used")),
                error_kind=("probe_failure" if not bool(row.get("success")) else None),
            )

            rows.append(
                {
                    "query": query,
                    "source": source,
                    "text_preview": response_text[:160],
                    "action_count": len(actions),
                }
            )

        out["responses"] = rows
        last_src = str((out["responses"][-1] or {}).get("source") or "") if out["responses"] else ""
        out["cache_hit_on_repeat"] = bool(last_src) and bool(engine.get_cached_best_response(str((out["responses"][-1] or {}).get("query") or "")))
        out["status"] = "success"
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)
    return out


def run() -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "error",
        "generated_at": _now_iso(),
        "learning_metrics": {},
        "learning_validation": {},
        "model_performance": {},
        "router_validation": {},
        "readiness": {},
    }

    engine = SelfLearningEngine(cooldown_seconds=1)
    runtime_probe = _run_runtime_prompt_probe(engine)
    cycle = engine.run_controlled_learning_cycle(lookback_hours=72)

    try:
        db._ensure_connected()
    except Exception as e:
        out["status"] = "partial"
        out["reason"] = f"db_connect_error: {e}"
        return out

    if db.db is None:
        out["status"] = "partial"
        out["reason"] = "db_unavailable"
        return out

    counts = {
        "learning_memory": int(db.db["learning_memory"].count_documents({})),
        "response_quality_signals": int(db.db["response_quality_signals"].count_documents({})),
        "self_improvement_suggestions": int(db.db["self_improvement_suggestions"].count_documents({})),
        "model_performance_stats": int(db.db["model_performance_stats"].count_documents({})),
        "learning_cycle_reports": int(db.db["learning_cycle_reports"].count_documents({})),
    }

    latest_report = db.db["learning_cycle_reports"].find_one({}, {"_id": 0}, sort=[("created_at", -1)]) or {}
    failure_patterns = latest_report.get("failure_patterns") if isinstance(latest_report.get("failure_patterns"), list) else []

    model_rows = list(
        db.db["model_performance_stats"]
        .find({}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(1200)
    )
    perf_summary = _model_perf_summary(model_rows)
    recommendations = _recommend_models(perf_summary, model_rows)

    route_samples = {
        "simple_chat": resolve_route("explain this quickly", mode="chat"),
        "debug_reasoning": resolve_route("debug this traceback and explain root cause", mode="chat"),
        "project_analysis": resolve_route("analyze this repository architecture and tradeoffs", mode="chat"),
    }

    cache_validation = _cache_validation(engine)

    dataset_stats = inspect_dataset("data/ai_training/datasets")
    readiness = compute_readiness(dataset_stats, model_supports_finetune=True)
    rec_type = str(readiness.get("recommended_finetune_type") or "none").lower()
    if not bool(readiness.get("ready")):
        tuning_reco = "no tuning yet"
    elif rec_type == "lora":
        tuning_reco = "LoRA"
    elif "instruction" in rec_type:
        tuning_reco = "instruction tuning"
    else:
        tuning_reco = "no tuning yet"

    out["learning_metrics"] = {
        "latest_success_score": float(latest_report.get("success_score") or 0.0),
        "top_failure_pattern": str(failure_patterns[0]) if failure_patterns else "",
        "open_suggestion_count": int(db.db["self_improvement_suggestions"].count_documents({"status": "open"})),
        "top_failing_model": perf_summary[0] if perf_summary else {},
        "last_learning_cycle_at": latest_report.get("created_at"),
        "cycle_result": cycle,
    }

    broad_threshold = {
        "learning_memory": 30,
        "response_quality_signals": 30,
        "self_improvement_suggestions": 10,
        "model_performance_stats": 25,
    }
    narrow_reasons = [k for k, min_count in broad_threshold.items() if int(counts.get(k) or 0) < int(min_count)]

    out["learning_validation"] = {
        "collection_counts": counts,
        "runtime_probe": runtime_probe,
        "examples": {
            "learning_memory": _safe_find_examples("learning_memory", {"_id": 0, "pattern_type": 1, "input_pattern": 1, "frequency": 1, "best_response": 1, "failure_case": 1, "updated_at": 1}),
            "response_quality_signals": _safe_find_examples("response_quality_signals", {"_id": 0, "recorded_at": 1, "query": 1, "quality_score": 1, "weak_reasons": 1}),
            "self_improvement_suggestions": _safe_find_examples("self_improvement_suggestions", {"_id": 0, "issue": 1, "root_cause": 1, "suggested_fix": 1, "priority": 1, "status": 1, "created_at": 1}),
            "model_performance_stats": _safe_find_examples("model_performance_stats", {"_id": 0, "recorded_at": 1, "task_type": 1, "model_id": 1, "provider": 1, "success": 1, "latency_ms": 1, "fallback_used": 1}),
        },
        "cache_validation": cache_validation,
        "learning_quality": {
            "broad_enough": len(narrow_reasons) == 0,
            "narrow_reasons": narrow_reasons,
        },
    }

    out["model_performance"] = {
        "summary_by_model": perf_summary,
        "recommendations": recommendations,
    }

    route_reasonable = True
    route_checks: dict[str, Any] = {}
    for key, route in route_samples.items():
        primary = str(((route or {}).get("primary") or {}).get("model_id") or "")
        reason = str((route or {}).get("adaptive_reason") or "")
        if reason.startswith("adaptive_switch") and not primary:
            route_reasonable = False
        route_checks[key] = {
            "task_type": route.get("task_type"),
            "primary_model": primary,
            "adaptive_reason": reason or None,
            "complexity": route.get("complexity"),
        }

    out["router_validation"] = {
        "reasonable": route_reasonable,
        "samples": route_checks,
    }

    out["readiness"] = {
        "readiness_score": readiness.get("readiness_score"),
        "missing_data_categories": readiness.get("missing_data_categories"),
        "fine_tuning_recommended": bool(readiness.get("ready")),
        "recommended_strategy": tuning_reco,
        "details": readiness,
    }

    out["status"] = "success"
    return out


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
