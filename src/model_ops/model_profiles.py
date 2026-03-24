from __future__ import annotations

from typing import Any

from .deployment_profiles import get_profile, list_profiles
from .finetune_config import get_finetune_profile, load_finetune_profiles


def get_runtime_profile(name: str) -> dict[str, Any] | None:
    return get_profile(name)


def get_all_runtime_profiles() -> dict[str, Any]:
    return list_profiles()


def get_training_profile(name: str) -> dict[str, Any] | None:
    return get_finetune_profile(name)


def get_all_training_profiles() -> dict[str, Any]:
    data = load_finetune_profiles()
    p = data.get("profiles", {})
    return p if isinstance(p, dict) else {}
