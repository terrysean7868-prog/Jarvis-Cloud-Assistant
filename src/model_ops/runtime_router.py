from __future__ import annotations

import re
from typing import Any

from .deployment_profiles import get_profile
from .model_catalog import get_model
from .model_registry import load_registry
from src.utils.db import db
from src.config import runtime_defaults as rd


def _complexity_score(text: str) -> int:
    t = str(text or "").strip().lower()
    wc = len(re.findall(r"[a-z0-9]+", t))
    score = 0
    if wc >= 18:
        score += 1
    if wc >= 35:
        score += 1
    if re.search(r"\b(step by step|analyze|debug|traceback|root cause|architecture|workflow|tradeoff|compare)\b", t):
        score += 1
    return min(3, score)


def _model_perf_summary(task_type: str, *, lookback: int = 220) -> dict[str, dict[str, Any]]:
    try:
        db._ensure_connected()
        if db.db is None:
            return {}
        rows = list(
            db.db["model_performance_stats"]
            .find({"task_type": str(task_type or "")}, {"model_id": 1, "success": 1, "latency_ms": 1, "fallback_used": 1})
            .sort("recorded_at", -1)
            .limit(max(20, min(int(lookback or 220), 1200)))
        )
        if not rows:
            return {}

        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            model_id = str((r or {}).get("model_id") or "unknown")
            item = out.setdefault(
                model_id,
                {
                    "model_id": model_id,
                    "calls": 0,
                    "failure": 0,
                    "latency_total": 0.0,
                    "fallback_calls": 0,
                    "failure_rate": 0.0,
                    "avg_latency_ms": 0.0,
                    "fallback_rate": 0.0,
                },
            )
            item["calls"] += 1
            ok = bool((r or {}).get("success"))
            item["failure"] += 0 if ok else 1
            item["latency_total"] += float((r or {}).get("latency_ms") or 0.0)
            item["fallback_calls"] += 1 if bool((r or {}).get("fallback_used")) else 0

        for v in out.values():
            calls = max(1, int(v.get("calls") or 1))
            v["failure_rate"] = float(v.get("failure") or 0) / calls
            v["avg_latency_ms"] = float(v.get("latency_total") or 0.0) / calls
            v["fallback_rate"] = float(v.get("fallback_calls") or 0) / calls
        return out
    except Exception:
        return {}


def _adaptive_override(task_type: str, model_id: str, fallback_model_id: str) -> tuple[str, str | None]:
    """Switch away from repeatedly failing model for this task type.

    Safety rule: only route choice changes; no autonomous code/actions are triggered.
    """
    try:
        summary = _model_perf_summary(task_type, lookback=240)
        current = summary.get(str(model_id))
        if not current or int(current.get("calls") or 0) < 6:
            return model_id, None

        current_fail = float(current.get("failure_rate") or 0.0)
        fallback = summary.get(str(fallback_model_id))
        if current_fail >= 0.45 and fallback and int(fallback.get("calls") or 0) >= 4:
            if float(fallback.get("failure_rate") or 1.0) + 0.08 < current_fail:
                return fallback_model_id, f"adaptive_switch:high_failure_rate={round(current_fail, 3)}"

        candidates = [v for v in summary.values() if int(v.get("calls") or 0) >= 4]
        if len(candidates) < 2:
            return model_id, None
        max_latency = max(1.0, max(float(c.get("avg_latency_ms") or 0.0) for c in candidates))

        def perf_score(row: dict[str, Any]) -> float:
            fail = float(row.get("failure_rate") or 0.0)
            fb = float(row.get("fallback_rate") or 0.0)
            lat = float(row.get("avg_latency_ms") or 0.0) / max_latency
            return (0.65 * fail) + (0.2 * fb) + (0.15 * lat)

        current_score = perf_score(current)
        best = min(candidates, key=perf_score)
        best_id = str(best.get("model_id") or "")
        best_score = perf_score(best)
        if best_id and best_id != str(model_id) and (current_score - best_score) >= 0.08:
            return best_id, f"adaptive_switch:perf_score_gain={round(current_score - best_score, 3)}"
    except Exception:
        return model_id, None
    return model_id, None


def classify_task_type(text: str, mode: str = "chat") -> str:
    t = str(text or "").strip().lower()
    if not t:
        return "greeting"
    if re.search(r"\b(hi|hello|hey|good morning|good evening)\b", t):
        return "greeting"
    if re.search(r"\b(thanks|thank you|ok|okay|cool|great)\b", t) and len(t.split()) <= 8:
        return "simple_chat"
    if re.search(r"\b(what can you do|capabilities|help)\b", t):
        return "capability_explanation"
    if re.search(r"\b(code|bug|debug|traceback|exception|fix)\b", t):
        return "debug_reasoning"
    if re.search(r"\b(project|codebase|repository|repo|architecture)\b", t):
        return "project_analysis"
    if re.search(r"\b(task history|last task|previous task|why did task|task status)\b", t):
        return "delegated_task_planning"
    if re.search(r"\b(system behavior|runtime|routing|listener|state machine|lifecycle)\b", t):
        return "debug_reasoning"
    if re.search(r"\b(research|sources|citation|summarize|rag)\b", t):
        return "rag_summarization"
    if re.search(r"\b(delegate|plan|steps|workflow|task)\b", t):
        return "delegated_task_planning"
    if re.search(r"\b(update yourself|self update|improve yourself)\b", t):
        return "self_update_analysis"
    if len(re.findall(r"[a-z0-9]+", t)) <= 6 and not re.search(r"\b(debug|error|task|project|analy|plan|fix|research)\b", t):
        return "simple_chat"
    base = "simple_chat" if mode != "voice" else "fallback_reply"
    # Long/complex prompts should prefer deeper reasoning path.
    if _complexity_score(t) >= 2 and base in {"simple_chat", "capability_explanation"}:
        return "debug_reasoning"
    return base


def resolve_route(text: str, mode: str = "chat", profile_name: str | None = None) -> dict[str, Any]:
    task = classify_task_type(text, mode=mode)
    reg = load_registry()
    active_profile = profile_name or str(reg.get("active_profile") or "local_primary_api_backup")
    provider_mode = str(getattr(rd, "LLM_PROVIDER", "openai_compatible") or "openai_compatible").strip().lower()

    # If runtime provider is API-based, avoid stale local-only profiles from old registries.
    if provider_mode == "openai_compatible" and not profile_name:
        if active_profile in {
            "local_only",
            "local_primary_api_backup",
            "local_chat_cloud_reasoning",
            "free_only_local_stack",
            "hybrid_local_primary_cloud_fallback",
        }:
            active_profile = "cloud_only"

    profile = get_profile(active_profile) or {}
    complexity = _complexity_score(text)

    role_to_field = {
        "greeting": "primary_chat_model",
        "simple_chat": "primary_chat_model",
        "capability_explanation": "primary_chat_model",
        "project_analysis": "code_debug_model",
        "debug_reasoning": "code_debug_model",
        "rag_summarization": "primary_chat_model",
        "delegated_task_planning": "code_debug_model",
        "fallback_reply": "fallback_model",
        "self_update_analysis": "code_debug_model",
    }

    field = role_to_field.get(task, "primary_chat_model")
    if complexity >= 2 and task in {"simple_chat", "capability_explanation"}:
        field = "code_debug_model"
    model_id = str(profile.get(field) or (reg.get("active_models") or {}).get("primary") or "ollama_llama3_1_8b")
    fallback_model_id = str(profile.get("fallback_model") or (reg.get("active_models") or {}).get("fallback") or "local_tiny_fallback")

    # Provider guardrails: map stale local model ids to remote-compatible ids when
    # runtime is configured for OpenAI/Groq endpoints.
    if provider_mode == "openai_compatible":
        if model_id.startswith("ollama_") or model_id in {"local_tiny_fallback", ""}:
            model_id = "openai_compatible_primary"
        if fallback_model_id.startswith("ollama_") or fallback_model_id in {"local_tiny_fallback", ""}:
            fallback_model_id = "openai_compatible_backup"

    model_id, adaptive_reason = _adaptive_override(task, model_id, fallback_model_id)

    model = get_model(model_id) or {"provider_type": "openai_compatible", "model_id": model_id}
    fallback_model = get_model(fallback_model_id) or {"provider_type": "fallback", "model_id": fallback_model_id}

    return {
        "task_type": task,
        "complexity": complexity,
        "reasoning_level": "deep" if task in {"debug_reasoning", "project_analysis", "delegated_task_planning", "self_update_analysis"} else "light",
        "profile": active_profile,
        "adaptive_reason": adaptive_reason,
        "primary": {
            "model_id": model_id,
            "provider": str(model.get("provider_type") or "openai_compatible"),
        },
        "fallback": {
            "model_id": fallback_model_id,
            "provider": str(fallback_model.get("provider_type") or "fallback"),
        },
    }
