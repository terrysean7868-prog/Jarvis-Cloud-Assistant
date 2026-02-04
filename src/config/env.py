from __future__ import annotations

import os
from typing import Optional


"""Centralized environment access.

Goal: avoid scattered direct `os.getenv(...)` usage across the codebase.

- Use these helpers for values that genuinely come from the runtime environment
  (secrets, hosting provider config, OS-provided variables).
- Prefer in-code defaults in `src/config/runtime_defaults.py` for non-secret behavior.
"""


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name, default)


def get_str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v)


def get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return int(default)
    try:
        return int(str(v).strip())
    except Exception:
        return int(default)


def get_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return float(default)
    try:
        return float(str(v).strip())
    except Exception:
        return float(default)


def get_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
