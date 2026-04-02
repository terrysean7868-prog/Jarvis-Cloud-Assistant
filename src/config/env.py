from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Optional


"""Centralized environment access.

Goal: avoid scattered direct `os.getenv(...)` usage across the codebase.

- Use these helpers for values that genuinely come from the runtime environment
  (secrets, hosting provider config, OS-provided variables).
- Prefer in-code defaults in `src/config/runtime_defaults.py` for non-secret behavior.
"""

logger = logging.getLogger(__name__)


def _load_dotenv_once() -> None:
    """Best-effort .env loading for entrypoints that don't call load_dotenv()."""
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return

    try:
        repo_root = Path(__file__).resolve().parents[2]
        env_file = repo_root / ".env"
        if env_file.exists():
            load_dotenv(dotenv_path=env_file, override=False)
        else:
            load_dotenv(override=False)
    except Exception:
        return


_load_dotenv_once()

# Strict application env whitelist.
ALLOWED_ENV_KEYS: set[str] = {
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "JARVIS_ALLOWED_PATHS",
    "JARVIS_JWT_ISSUER",
    "JARVIS_JWT_SECRET",
    "JARVIS_REDIS_URL",
    "MONGODB_DB_NAME",
    "MONGODB_URI",
    "OPENAI_API_KEY",
    "OPENWEATHER_KEY",
    "TELEGRAM_TOKEN",
    "VOICE_MAX_SAMPLES",
    "VOICE_TEXT_SIMILARITY_THRESHOLD",
}

_blocked_env_once: set[str] = set()


def _resolve_env_key(name: str) -> str:
    return str(name or "").strip()


def _is_allowed_env_key(name: str) -> bool:
    k = str(name or "").strip()
    return k in ALLOWED_ENV_KEYS


def _warn_blocked_key(name: str) -> None:
    k = str(name or "").strip()
    if not k:
        return
    if k in _blocked_env_once:
        return
    _blocked_env_once.add(k)
    logger.warning("[env] blocked non-whitelist env key: %s", k)


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    key = _resolve_env_key(name)
    if not _is_allowed_env_key(key):
        _warn_blocked_key(key)
        return default
    return os.getenv(key, default)


def get_str(name: str, default: str = "") -> str:
    v = get(name)
    if v is None:
        return default
    return str(v)


def get_int(name: str, default: int) -> int:
    v = get(name)
    if v is None:
        return int(default)
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)


def get_float(name: str, default: float) -> float:
    v = get(name)
    if v is None:
        return float(default)
    try:
        return float(str(v).strip())
    except Exception:
        return float(default)


def get_bool(name: str, default: bool = False) -> bool:
    v = get(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
