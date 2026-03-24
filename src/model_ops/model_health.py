from __future__ import annotations

from typing import Any

from src.config import runtime_defaults as rd
from src.config.secrets import llm_secrets

from .providers import FallbackProvider, LocalModelProvider, OllamaProvider, OpenAICompatibleProvider
from .utils import DATA_DIR, save_json, now_iso


HEALTH_PATH = DATA_DIR / "registry" / "health_status.json"


def check_health() -> dict[str, Any]:
    secrets = llm_secrets()
    ollama = OllamaProvider().check_health()
    openai_primary = OpenAICompatibleProvider().check_health(
        endpoint=str(getattr(rd, "PRIMARY_ENDPOINT", "") or ""),
        api_key=secrets.primary_api_key,
    )
    openai_backup = OpenAICompatibleProvider().check_health(
        endpoint=str(getattr(rd, "BACKUP_ENDPOINT", "") or ""),
        api_key=secrets.backup_api_key,
    )
    local_det = LocalModelProvider().check_health()
    fallback = FallbackProvider().check_health()

    report = {
        "checked_at": now_iso(),
        "providers": {
            "ollama": ollama,
            "openai_primary": openai_primary,
            "openai_backup": openai_backup,
            "local_model": local_det,
            "fallback": fallback,
        },
        "timeout_rate": 0.0,
        "fallback_rate": 0.0,
        "quota_failures": 0,
    }
    save_json(HEALTH_PATH, report)
    return report
