from __future__ import annotations

from typing import Any

from .utils import CONFIG_DIR, load_json, save_json


PROFILES_PATH = CONFIG_DIR / "deployment_profiles.json"


DEFAULT_PROFILES: dict[str, Any] = {
    "schema_version": 1,
    "profiles": {
        "local_only": {
            "primary_chat_model": "ollama_llama3_1_8b",
            "fallback_model": "local_tiny_fallback",
            "code_debug_model": "ollama_qwen2_5_7b",
            "embedding_model": "local_embedding_default",
            "embedding_provider": "local",
            "rag_enabled": True,
            "timeout_seconds": 18,
            "fallback_routing": "deterministic_local",
            "required_env": ["JARVIS_LLM_PROVIDER=ollama"],
            "safe_defaults": {"offline": True, "free_only": True},
        },
        "cloud_only": {
            "primary_chat_model": "openai_compatible_primary",
            "fallback_model": "openai_compatible_backup",
            "code_debug_model": "openai_compatible_primary",
            "embedding_model": "remote_embedding_default",
            "embedding_provider": "openai_compatible",
            "rag_enabled": True,
            "timeout_seconds": 18,
            "fallback_routing": "provider_fallback",
            "required_env": ["PRIMARY_API_KEY", "PRIMARY_ENDPOINT"],
            "safe_defaults": {"offline": False, "free_only": False},
        },
        "hybrid_local_primary_cloud_fallback": {
            "primary_chat_model": "ollama_llama3_1_8b",
            "fallback_model": "openai_compatible_backup",
            "code_debug_model": "openai_compatible_primary",
            "embedding_model": "remote_embedding_default",
            "embedding_provider": "openai_compatible",
            "rag_enabled": True,
            "timeout_seconds": 16,
            "fallback_routing": "local_then_remote",
            "required_env": ["JARVIS_LLM_PROVIDER=ollama"],
            "safe_defaults": {"offline": "partial", "free_only": False},
        },
        "local_chat_cloud_reasoning": {
            "primary_chat_model": "ollama_qwen2_5_7b",
            "fallback_model": "openai_compatible_backup",
            "code_debug_model": "openai_compatible_primary",
            "embedding_model": "remote_embedding_default",
            "embedding_provider": "openai_compatible",
            "rag_enabled": True,
            "timeout_seconds": 16,
            "fallback_routing": "task_aware",
            "required_env": [],
            "safe_defaults": {"offline": "partial", "free_only": False},
        },
        "local_primary_api_backup": {
            "primary_chat_model": "ollama_llama3_1_8b",
            "fallback_model": "openai_compatible_backup",
            "code_debug_model": "ollama_qwen2_5_7b",
            "embedding_model": "local_embedding_default",
            "embedding_provider": "local",
            "rag_enabled": True,
            "timeout_seconds": 16,
            "fallback_routing": "provider_fallback",
            "required_env": [],
            "safe_defaults": {"offline": True, "free_only": False},
        },
        "free_only_local_stack": {
            "primary_chat_model": "ollama_qwen2_5_7b",
            "fallback_model": "local_tiny_fallback",
            "code_debug_model": "ollama_llama3_1_8b",
            "embedding_model": "local_embedding_default",
            "embedding_provider": "local",
            "rag_enabled": True,
            "timeout_seconds": 20,
            "fallback_routing": "deterministic_local",
            "required_env": ["JARVIS_LLM_PROVIDER=ollama"],
            "safe_defaults": {"offline": True, "free_only": True},
        },
    },
}


def seed_profiles_if_missing() -> None:
    if PROFILES_PATH.exists():
        return
    save_json(PROFILES_PATH, DEFAULT_PROFILES)


def load_profiles() -> dict[str, Any]:
    seed_profiles_if_missing()
    data = load_json(PROFILES_PATH, default=DEFAULT_PROFILES)
    if not isinstance(data, dict):
        return DEFAULT_PROFILES
    data.setdefault("profiles", {})
    return data


def get_profile(profile_name: str) -> dict[str, Any] | None:
    p = load_profiles().get("profiles", {})
    if not isinstance(p, dict):
        return None
    return p.get(str(profile_name or "").strip())


def list_profiles() -> dict[str, Any]:
    data = load_profiles()
    p = data.get("profiles", {})
    return p if isinstance(p, dict) else {}


def update_profile_models(
    profile_name: str,
    *,
    primary_chat_model: str | None = None,
    code_debug_model: str | None = None,
    fallback_model: str | None = None,
) -> dict[str, Any]:
    data = load_profiles()
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    key = str(profile_name or "").strip()
    if not key:
        return data
    profile = profiles.get(key) if isinstance(profiles.get(key), dict) else {}

    if primary_chat_model:
        profile["primary_chat_model"] = str(primary_chat_model).strip()
    if code_debug_model:
        profile["code_debug_model"] = str(code_debug_model).strip()
    if fallback_model:
        profile["fallback_model"] = str(fallback_model).strip()

    profiles[key] = profile
    data["profiles"] = profiles
    save_json(PROFILES_PATH, data)
    return data
