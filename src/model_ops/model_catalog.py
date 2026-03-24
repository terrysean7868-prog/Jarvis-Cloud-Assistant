from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import CONFIG_DIR, load_json, save_json


CATALOG_PATH = CONFIG_DIR / "model_catalog.json"


DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "model_id": "ollama_llama3_1_8b",
        "display_name": "Llama 3.1 8B (Ollama)",
        "provider_type": "ollama",
        "runtime_type": "chat",
        "local_or_remote": "local",
        "cost_profile": "free",
        "latency_profile": "medium",
        "memory_requirements": {"min_ram_gb": 16, "vram_gb": 8},
        "supports_tools": True,
        "supports_long_context": True,
        "supports_instruction_tuning": True,
        "supports_lora": True,
        "supports_embeddings": False,
        "supports_reasoning": True,
        "supports_chat": True,
        "supports_code": True,
        "supports_rag": True,
        "supports_function_call_style_routing": True,
        "recommended_use_cases": ["chat", "code", "task_reasoning", "fallback"],
        "deployment_constraints": ["requires_ollama"],
    },
    {
        "model_id": "ollama_qwen2_5_7b",
        "display_name": "Qwen2.5 7B (Ollama)",
        "provider_type": "ollama",
        "runtime_type": "chat",
        "local_or_remote": "local",
        "cost_profile": "free",
        "latency_profile": "fast",
        "memory_requirements": {"min_ram_gb": 12, "vram_gb": 6},
        "supports_tools": True,
        "supports_long_context": True,
        "supports_instruction_tuning": True,
        "supports_lora": True,
        "supports_embeddings": False,
        "supports_reasoning": True,
        "supports_chat": True,
        "supports_code": True,
        "supports_rag": True,
        "supports_function_call_style_routing": True,
        "recommended_use_cases": ["chat", "debug", "planning"],
        "deployment_constraints": ["requires_ollama"],
    },
    {
        "model_id": "local_tiny_fallback",
        "display_name": "Local Tiny Fallback",
        "provider_type": "local_model",
        "runtime_type": "fallback",
        "local_or_remote": "local",
        "cost_profile": "free",
        "latency_profile": "fast",
        "memory_requirements": {"min_ram_gb": 4, "vram_gb": 0},
        "supports_tools": False,
        "supports_long_context": False,
        "supports_instruction_tuning": False,
        "supports_lora": False,
        "supports_embeddings": False,
        "supports_reasoning": False,
        "supports_chat": True,
        "supports_code": False,
        "supports_rag": False,
        "supports_function_call_style_routing": False,
        "recommended_use_cases": ["fallback", "simple_chat"],
        "deployment_constraints": [],
    },
    {
        "model_id": "openai_compatible_primary",
        "display_name": "OpenAI-Compatible Primary",
        "provider_type": "openai_compatible",
        "runtime_type": "chat",
        "local_or_remote": "remote",
        "cost_profile": "paid",
        "latency_profile": "medium",
        "memory_requirements": {"min_ram_gb": 0, "vram_gb": 0},
        "supports_tools": True,
        "supports_long_context": True,
        "supports_instruction_tuning": True,
        "supports_lora": False,
        "supports_embeddings": True,
        "supports_reasoning": True,
        "supports_chat": True,
        "supports_code": True,
        "supports_rag": True,
        "supports_function_call_style_routing": True,
        "recommended_use_cases": ["chat", "code", "research", "self_update_reasoning"],
        "deployment_constraints": ["requires_api_key"],
    },
    {
        "model_id": "openai_compatible_backup",
        "display_name": "OpenAI-Compatible Backup",
        "provider_type": "fallback",
        "runtime_type": "fallback",
        "local_or_remote": "remote",
        "cost_profile": "paid",
        "latency_profile": "medium",
        "memory_requirements": {"min_ram_gb": 0, "vram_gb": 0},
        "supports_tools": True,
        "supports_long_context": True,
        "supports_instruction_tuning": True,
        "supports_lora": False,
        "supports_embeddings": True,
        "supports_reasoning": True,
        "supports_chat": True,
        "supports_code": True,
        "supports_rag": True,
        "supports_function_call_style_routing": True,
        "recommended_use_cases": ["fallback", "high_quality_chat"],
        "deployment_constraints": ["requires_api_key"],
    },
]


def seed_catalog_if_missing() -> None:
    if CATALOG_PATH.exists():
        return
    save_json(CATALOG_PATH, {"schema_version": 1, "models": DEFAULT_MODELS})


def load_catalog() -> list[dict[str, Any]]:
    seed_catalog_if_missing()
    data = load_json(CATALOG_PATH, default={"models": []})
    models = data.get("models") if isinstance(data, dict) else []
    return models if isinstance(models, list) else []


def get_model(model_id: str) -> dict[str, Any] | None:
    mid = str(model_id or "").strip().lower()
    for m in load_catalog():
        if str(m.get("model_id") or "").strip().lower() == mid:
            return m
    return None


def list_models() -> list[dict[str, Any]]:
    return load_catalog()
