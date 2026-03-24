from __future__ import annotations

from typing import Any

from .capability_matrix import score_model_for_task
from .deployment_profiles import list_profiles
from .model_catalog import list_models


REQUIRED_MEMORY_GB_BY_TASK = {
    "chat": 8,
    "code": 12,
    "debug": 12,
    "reasoning": 12,
}


def _eligible_models(constraints: dict[str, Any]) -> list[dict[str, Any]]:
    budget = str(constraints.get("budget") or "low").lower()
    free_only = bool(constraints.get("free_only", False))
    offline_required = bool(constraints.get("offline_required", False))
    deployment_mode = str(constraints.get("deployment_mode") or "hybrid").lower()
    hw_ram_gb = int(constraints.get("ram_gb") or 8)
    provider_availability = constraints.get("provider_availability") or {}

    out: list[dict[str, Any]] = []
    for m in list_models():
        if free_only and m.get("cost_profile") != "free":
            continue
        if offline_required and m.get("local_or_remote") != "local":
            continue
        if deployment_mode in {"cloud", "cloud_only", "api", "api_first"} and m.get("local_or_remote") != "remote":
            continue
        if deployment_mode in {"local", "local_only"} and m.get("local_or_remote") != "local":
            continue
        if budget == "0" and m.get("cost_profile") != "free":
            continue

        mem = m.get("memory_requirements") if isinstance(m.get("memory_requirements"), dict) else {}
        min_ram = int(mem.get("min_ram_gb") or 0)
        if min_ram and hw_ram_gb < min_ram:
            continue

        provider = str(m.get("provider_type") or "").strip().lower()
        if isinstance(provider_availability, dict) and provider in provider_availability:
            if not bool(provider_availability.get(provider)):
                continue

        out.append(m)
    return out


def _pick_best(candidates: list[dict[str, Any]], task: str, *, prefer_local: bool = False) -> dict[str, Any] | None:
    scored: list[tuple[float, dict[str, Any]]] = []
    for m in candidates:
        s = score_model_for_task(m, task)
        if prefer_local and m.get("local_or_remote") == "local":
            s += 0.8
        scored.append((s, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored else None


def recommend_models(constraints: dict[str, Any]) -> dict[str, Any]:
    candidates = _eligible_models(constraints)
    deployment_mode = str(constraints.get("deployment_mode") or "hybrid").lower()
    hybrid_allowed = bool(constraints.get("hybrid_allowed", True))
    prefer_local = deployment_mode in {"local", "local_only"} or bool(constraints.get("offline_required", False))

    chat_priority = int(constraints.get("chat_quality_priority") or 5)
    code_priority = int(constraints.get("code_debug_priority") or 5)

    primary_task = "debug_suggestion" if code_priority > chat_priority else "chat"
    primary = _pick_best(candidates, primary_task, prefer_local=prefer_local)

    fallback_candidates = [m for m in candidates if not primary or m.get("model_id") != primary.get("model_id")]
    fallback = _pick_best(fallback_candidates, "fallback_reply", prefer_local=prefer_local)

    embedding = "local_embedding_default" if prefer_local else "remote_embedding_default"

    profiles = list_profiles()
    profile_name = "local_primary_api_backup"
    if prefer_local and not hybrid_allowed:
        profile_name = "local_only"
    elif deployment_mode in {"cloud", "cloud_only"}:
        profile_name = "cloud_only"
    elif hybrid_allowed and prefer_local:
        profile_name = "hybrid_local_primary_cloud_fallback"
    if profile_name not in profiles:
        profile_name = next(iter(profiles.keys()), "local_primary_api_backup")

    reasoning = [
        f"candidates_considered={len(candidates)}",
        f"primary_task={primary_task}",
        f"prefer_local={prefer_local}",
        f"profile={profile_name}",
    ]

    return {
        "primary_model": (primary or {}).get("model_id"),
        "fallback_model": (fallback or {}).get("model_id"),
        "embedding_strategy": {
            "embedding_model": embedding,
            "provider": "local" if embedding.startswith("local") else "openai_compatible",
        },
        "deployment_profile": profile_name,
        "reasoning": reasoning,
        "candidate_count": len(candidates),
    }
