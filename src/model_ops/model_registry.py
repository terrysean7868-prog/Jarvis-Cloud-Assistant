from __future__ import annotations

from typing import Any

from .utils import DATA_DIR, load_json, save_json, now_iso


REGISTRY_PATH = DATA_DIR / "registry" / "active_registry.json"


def _default_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": now_iso(),
        "active_profile": "cloud_only",
        "active_models": {
            "primary": "openai_compatible_primary",
            "fallback": "openai_compatible_backup",
            "code_debug": "openai_compatible_primary",
            "embedding": "remote_embedding_default",
        },
        "health": {},
        "last_benchmark": None,
        "known_failure_modes": [],
        "readiness": {},
        "tuned_models": {},
        "preferred_tuned_model": None,
    }


def load_registry() -> dict[str, Any]:
    data = load_json(REGISTRY_PATH, default=_default_registry())
    if not isinstance(data, dict):
        data = _default_registry()
    if not isinstance(data.get("tuned_models"), dict):
        data["tuned_models"] = {}
    if "preferred_tuned_model" not in data:
        data["preferred_tuned_model"] = None
    return data


def save_registry(data: dict[str, Any]) -> None:
    payload = dict(data or {})
    payload["updated_at"] = now_iso()
    save_json(REGISTRY_PATH, payload)


def update_profile(profile_name: str, active_models: dict[str, str] | None = None) -> dict[str, Any]:
    reg = load_registry()
    reg["active_profile"] = str(profile_name or "").strip()
    if isinstance(active_models, dict) and active_models:
        reg["active_models"] = {**(reg.get("active_models") or {}), **active_models}
    save_registry(reg)
    return reg


def update_health(health: dict[str, Any]) -> dict[str, Any]:
    reg = load_registry()
    reg["health"] = health
    save_registry(reg)
    return reg


def update_benchmark(result_path: str, summary: dict[str, Any]) -> dict[str, Any]:
    reg = load_registry()
    reg["last_benchmark"] = {
        "path": result_path,
        "summary": summary,
        "at": now_iso(),
    }
    save_registry(reg)
    return reg


def update_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    reg = load_registry()
    reg["readiness"] = readiness
    save_registry(reg)
    return reg


def register_tuned_model(
    *,
    tuned_model_id: str,
    base_model_id: str,
    dataset_version: str,
    training_run_id: str,
    training_manifest_path: str,
    preferred: bool = True,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reg = load_registry()
    tuned_map = reg.get("tuned_models") if isinstance(reg.get("tuned_models"), dict) else {}
    model_id = str(tuned_model_id or "").strip()
    if not model_id:
        return reg

    tuned_map[model_id] = {
        "tuned": True,
        "model_id": model_id,
        "base_model_id": str(base_model_id or "").strip() or None,
        "dataset_version": str(dataset_version or "").strip() or None,
        "training_run_id": str(training_run_id or "").strip() or None,
        "training_manifest_path": str(training_manifest_path or "").strip() or None,
        "registered_at": now_iso(),
        "metadata": metadata if isinstance(metadata, dict) else {},
    }
    reg["tuned_models"] = tuned_map

    if preferred:
        reg["preferred_tuned_model"] = model_id
        active = reg.get("active_models") if isinstance(reg.get("active_models"), dict) else {}
        active["primary"] = model_id
        active["code_debug"] = model_id
        reg["active_models"] = active

    save_registry(reg)
    return reg


def get_preferred_tuned_model() -> str | None:
    reg = load_registry()
    mid = str(reg.get("preferred_tuned_model") or "").strip()
    return mid or None
