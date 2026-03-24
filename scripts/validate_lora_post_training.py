from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

from src.model_ops.runtime_router import resolve_route
from src.utils.db import db


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_text(v: Any) -> str:
    return str(v or "").strip()


def _empty_metrics(model_id: str) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "calls": 0,
        "quality": None,
        "latency_ms": None,
        "fallback_rate": None,
    }


def _summarize(rows: list[dict[str, Any]], model_id: str, task_types: set[str]) -> dict[str, Any]:
    filtered = [
        r
        for r in rows
        if _safe_text((r or {}).get("model_id")) == model_id
        and _safe_text((r or {}).get("task_type")) in task_types
    ]
    if not filtered:
        return _empty_metrics(model_id)

    calls = len(filtered)
    success = sum(1 for r in filtered if bool((r or {}).get("success")))
    latency = sum(float((r or {}).get("latency_ms") or 0.0) for r in filtered)
    fallback = sum(1 for r in filtered if bool((r or {}).get("fallback_used")))
    return {
        "model_id": model_id,
        "calls": calls,
        "quality": round(success / max(1, calls), 4),
        "latency_ms": round(latency / max(1, calls), 3),
        "fallback_rate": round(fallback / max(1, calls), 4),
    }


def _weighted_quality(chat: dict[str, Any], debug: dict[str, Any], task: dict[str, Any]) -> float | None:
    vals: list[float] = []
    for item in [chat, debug, task]:
        q = item.get("quality")
        if isinstance(q, float):
            vals.append(q)
    if not vals:
        return None
    return round(sum(vals) / max(1, len(vals)), 4)


def _recommendation(base: dict[str, Any], tuned: dict[str, Any], min_calls: int) -> str:
    tuned_calls = int(tuned.get("total_calls") or 0)
    if tuned_calls < max(1, int(min_calls)):
        return "keep base model"

    bq = base.get("weighted_quality")
    tq = tuned.get("weighted_quality")
    blat = base.get("overall_latency_ms")
    tlat = tuned.get("overall_latency_ms")
    bfb = base.get("overall_fallback_rate")
    tfb = tuned.get("overall_fallback_rate")

    if isinstance(bq, float) and isinstance(tq, float):
        quality_gain = tq - bq
    else:
        quality_gain = 0.0

    fallback_ok = isinstance(tfb, float) and isinstance(bfb, float) and tfb <= (bfb + 0.02)
    latency_ok = isinstance(tlat, float) and isinstance(blat, float) and tlat <= (blat * 1.2)

    if quality_gain >= 0.03 and fallback_ok and latency_ok:
        return "apply tuned model as primary"
    if quality_gain >= -0.02 and fallback_ok:
        return "use tuned model for limited traffic"
    return "keep base model"


def _route_checks(profile_name: str) -> dict[str, Any]:
    prompts = {
        "greeting": "Hello Jarvis",
        "capability_question": "What can you do?",
        "project_question": "Analyze this repository architecture and tradeoffs",
        "debug_question": "Debug this traceback and explain root cause",
        "repeated_query": "What is the current active profile?",
        "delegated_task_wording_quality": "Plan and delegate safe steps to open the editor and run checks",
    }

    checks: dict[str, Any] = {}
    for name, prompt in prompts.items():
        route = resolve_route(prompt, mode="chat", profile_name=profile_name)
        checks[name] = {
            "task_type": route.get("task_type"),
            "reasoning_level": route.get("reasoning_level"),
            "primary_model": ((route.get("primary") or {}).get("model_id") if isinstance(route.get("primary"), dict) else None),
            "fallback_model": ((route.get("fallback") or {}).get("model_id") if isinstance(route.get("fallback"), dict) else None),
        }

    r1 = resolve_route(prompts["repeated_query"], mode="chat", profile_name=profile_name)
    r2 = resolve_route(prompts["repeated_query"], mode="chat", profile_name=profile_name)
    checks["repeated_query_stability"] = {
        "stable_task_type": _safe_text(r1.get("task_type")) == _safe_text(r2.get("task_type")),
        "stable_primary": _safe_text(((r1.get("primary") or {}).get("model_id") if isinstance(r1.get("primary"), dict) else ""))
        == _safe_text(((r2.get("primary") or {}).get("model_id") if isinstance(r2.get("primary"), dict) else "")),
    }

    delegated_ok = checks["delegated_task_wording_quality"].get("task_type") == "delegated_task_planning"
    checks["delegated_task_wording_quality"]["pass"] = bool(delegated_ok)
    return checks


def run_validation(*, base_model: str, tuned_model: str, profile_name: str, min_calls: int) -> dict[str, Any]:
    db._ensure_connected()
    rows: list[dict[str, Any]] = []
    if db.db is not None:
        rows = list(
            db.db["model_performance_stats"]
            .find({}, {"_id": 0, "model_id": 1, "task_type": 1, "success": 1, "latency_ms": 1, "fallback_used": 1})
            .sort("recorded_at", -1)
            .limit(4000)
        )

    chat_types = {"greeting", "simple_chat", "capability_explanation"}
    debug_types = {"debug_reasoning"}
    task_types = {"delegated_task_planning", "project_analysis", "self_update_analysis"}

    base_chat = _summarize(rows, base_model, chat_types)
    base_debug = _summarize(rows, base_model, debug_types)
    base_task = _summarize(rows, base_model, task_types)

    tuned_chat = _summarize(rows, tuned_model, chat_types)
    tuned_debug = _summarize(rows, tuned_model, debug_types)
    tuned_task = _summarize(rows, tuned_model, task_types)

    def _overall_for_model(model_id: str) -> dict[str, Any]:
        own = [r for r in rows if _safe_text((r or {}).get("model_id")) == model_id]
        if not own:
            return {
                "total_calls": 0,
                "overall_latency_ms": None,
                "overall_fallback_rate": None,
            }
        calls = len(own)
        lat = sum(float((r or {}).get("latency_ms") or 0.0) for r in own)
        fb = sum(1 for r in own if bool((r or {}).get("fallback_used")))
        return {
            "total_calls": calls,
            "overall_latency_ms": round(lat / max(1, calls), 3),
            "overall_fallback_rate": round(fb / max(1, calls), 4),
        }

    base_overall = _overall_for_model(base_model)
    tuned_overall = _overall_for_model(tuned_model)

    base_summary = {
        **base_overall,
        "chat_quality": base_chat,
        "debug_quality": base_debug,
        "task_reasoning_quality": base_task,
        "weighted_quality": _weighted_quality(base_chat, base_debug, base_task),
    }
    tuned_summary = {
        **tuned_overall,
        "chat_quality": tuned_chat,
        "debug_quality": tuned_debug,
        "task_reasoning_quality": tuned_task,
        "weighted_quality": _weighted_quality(tuned_chat, tuned_debug, tuned_task),
    }

    route_checks = _route_checks(profile_name)
    rec = _recommendation(base_summary, tuned_summary, min_calls=min_calls)

    return {
        "status": "success",
        "created_at": _now_iso(),
        "profile_name": profile_name,
        "base_model": base_model,
        "tuned_model": tuned_model,
        "comparison": {
            "base": base_summary,
            "tuned": tuned_summary,
        },
        "route_validation": route_checks,
        "recommendation": rec,
        "notes": [
            "This validation is non-mutating and does not apply runtime profile changes.",
            "If tuned model has insufficient calls, recommendation defaults to keep base model.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Post-training LoRA validation and tuned-vs-base comparison.")
    p.add_argument("--base-model", default="ollama_llama3_1_8b", help="Runtime base model id.")
    p.add_argument("--tuned-model", required=True, help="Tuned model id to evaluate.")
    p.add_argument("--profile-name", default="local_primary_api_backup", help="Profile used for route checks.")
    p.add_argument("--min-calls", type=int, default=8, help="Minimum tuned calls required before stronger rollout recommendations.")
    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    out = run_validation(
        base_model=_safe_text(args.base_model),
        tuned_model=_safe_text(args.tuned_model),
        profile_name=_safe_text(args.profile_name) or "local_primary_api_backup",
        min_calls=int(args.min_calls),
    )
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
