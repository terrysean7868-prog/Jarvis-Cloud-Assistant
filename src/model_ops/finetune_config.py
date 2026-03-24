from __future__ import annotations

from typing import Any

from .utils import CONFIG_DIR, load_json, save_json


FINETUNE_PROFILES_PATH = CONFIG_DIR / "finetune_profiles.json"


DEFAULT_FINETUNE_PROFILES = {
    "schema_version": 1,
    "profiles": {
        "lightweight_lora_chat": {
            "target_data_types": ["instruction", "conversation"],
            "export_format": "lora",
            "recommended_model_classes": ["llama", "qwen", "mistral"],
            "batch_size_hint": 8,
            "sequence_length_hint": 2048,
            "epochs_hint": 2,
            "masking_rules": ["mask_secrets", "mask_tokens"],
            "evaluation_hooks": ["chat_quality", "latency"],
        },
        "lora_code_reasoning": {
            "target_data_types": ["instruction", "task", "error"],
            "export_format": "lora",
            "recommended_model_classes": ["qwen", "llama"],
            "batch_size_hint": 6,
            "sequence_length_hint": 3072,
            "epochs_hint": 3,
            "masking_rules": ["mask_secrets", "mask_paths"],
            "evaluation_hooks": ["debug_reasoning", "task_planning"],
        },
        "instruction_tuning_general": {
            "target_data_types": ["instruction"],
            "export_format": "jsonl",
            "recommended_model_classes": ["openai_compatible", "llama", "qwen"],
            "batch_size_hint": 16,
            "sequence_length_hint": 2048,
            "epochs_hint": 3,
            "masking_rules": ["mask_secrets"],
            "evaluation_hooks": ["chat_quality"],
        },
        "task_reasoning_tuning": {
            "target_data_types": ["task", "conversation", "error"],
            "export_format": "jsonl",
            "recommended_model_classes": ["llama", "qwen"],
            "batch_size_hint": 8,
            "sequence_length_hint": 4096,
            "epochs_hint": 3,
            "masking_rules": ["mask_secrets"],
            "evaluation_hooks": ["task_planning", "delegation"],
        },
        "response_style_tuning": {
            "target_data_types": ["conversation", "instruction"],
            "export_format": "jsonl",
            "recommended_model_classes": ["openai_compatible", "llama"],
            "batch_size_hint": 16,
            "sequence_length_hint": 1024,
            "epochs_hint": 2,
            "masking_rules": ["mask_secrets"],
            "evaluation_hooks": ["style_consistency"],
        },
    },
}


def seed_finetune_profiles_if_missing() -> None:
    if FINETUNE_PROFILES_PATH.exists():
        return
    save_json(FINETUNE_PROFILES_PATH, DEFAULT_FINETUNE_PROFILES)


def load_finetune_profiles() -> dict[str, Any]:
    seed_finetune_profiles_if_missing()
    data = load_json(FINETUNE_PROFILES_PATH, default=DEFAULT_FINETUNE_PROFILES)
    if not isinstance(data, dict):
        return DEFAULT_FINETUNE_PROFILES
    data.setdefault("profiles", {})
    return data


def get_finetune_profile(name: str) -> dict[str, Any] | None:
    p = load_finetune_profiles().get("profiles", {})
    if not isinstance(p, dict):
        return None
    return p.get(str(name or "").strip())
