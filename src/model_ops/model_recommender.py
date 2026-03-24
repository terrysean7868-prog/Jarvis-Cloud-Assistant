from __future__ import annotations

from typing import Any

from .model_selector import recommend_models
from .utils import DATA_DIR, save_json, now_iso


def recommend_local_only(constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    c = dict(constraints or {})
    c.update({"deployment_mode": "local_only", "offline_required": True, "hybrid_allowed": False})
    out = recommend_models(c)
    out["mode"] = "local_only"
    return out


def recommend_hybrid(constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    c = dict(constraints or {})
    c.update({"deployment_mode": "hybrid", "hybrid_allowed": True})
    out = recommend_models(c)
    out["mode"] = "hybrid"
    return out


def recommend_api_first(constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    c = dict(constraints or {})
    c.update({"deployment_mode": "cloud_only", "offline_required": False})
    out = recommend_models(c)
    out["mode"] = "api_first"
    return out


def recommend_with_mode(mode: str, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    m = str(mode or "hybrid").strip().lower()
    if m in {"local", "local_only"}:
        return recommend_local_only(constraints)
    if m in {"api", "api_first", "cloud", "cloud_only"}:
        return recommend_api_first(constraints)
    return recommend_hybrid(constraints)


def save_recommendation(result: dict[str, Any], prefix: str = "recommendation") -> str:
    p = DATA_DIR / "recommendations" / f"{prefix}_{now_iso().replace(':', '-').replace('.', '-')}.json"
    save_json(p, result)
    return str(p)
