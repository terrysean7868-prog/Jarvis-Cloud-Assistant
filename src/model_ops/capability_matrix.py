from __future__ import annotations

from typing import Any

from .model_catalog import list_models


TASK_REQUIREMENTS = {
    "chat": ["supports_chat"],
    "project_analysis": ["supports_code", "supports_reasoning"],
    "delegated_planning": ["supports_reasoning", "supports_tools"],
    "permission_negotiation": ["supports_reasoning"],
    "self_update_reasoning": ["supports_code", "supports_reasoning"],
    "rag_synthesis": ["supports_rag", "supports_long_context"],
    "debug_suggestion": ["supports_code", "supports_reasoning"],
    "task_reasoning": ["supports_reasoning", "supports_tools"],
    "fallback_reply": ["supports_chat"],
}


def model_supports_task(model: dict[str, Any], task: str) -> bool:
    reqs = TASK_REQUIREMENTS.get(task, ["supports_chat"])
    return all(bool(model.get(k, False)) for k in reqs)


def score_model_for_task(model: dict[str, Any], task: str) -> float:
    score = 0.0
    if model_supports_task(model, task):
        score += 5.0
    if model.get("local_or_remote") == "local":
        score += 0.8
    if model.get("latency_profile") == "fast":
        score += 1.0
    elif model.get("latency_profile") == "medium":
        score += 0.5
    if model.get("cost_profile") == "free":
        score += 0.8
    if task in {"project_analysis", "debug_suggestion"} and model.get("supports_code"):
        score += 1.5
    if task in {"rag_synthesis"} and model.get("supports_rag"):
        score += 1.5
    if task in {"delegated_planning", "task_reasoning"} and model.get("supports_tools"):
        score += 1.0
    return score


def best_models_for_task(task: str, *, local_only: bool = False) -> list[dict[str, Any]]:
    out: list[tuple[float, dict[str, Any]]] = []
    for m in list_models():
        if local_only and m.get("local_or_remote") != "local":
            continue
        out.append((score_model_for_task(m, task), m))
    out.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in out]


def capability_summary() -> dict[str, Any]:
    models = list_models()
    return {
        "chat_best": [m.get("model_id") for m in best_models_for_task("chat")[:3]],
        "code_best": [m.get("model_id") for m in best_models_for_task("debug_suggestion")[:3]],
        "fallback_best": [m.get("model_id") for m in best_models_for_task("fallback_reply")[:3]],
        "fully_local": [m.get("model_id") for m in models if m.get("local_or_remote") == "local"],
        "hybrid_candidates": [m.get("model_id") for m in models if m.get("local_or_remote") in {"local", "remote"}],
    }
