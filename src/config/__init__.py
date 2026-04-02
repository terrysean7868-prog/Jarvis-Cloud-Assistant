"""Configuration package.

Keep imports side-effect free so callers can import lightweight helpers like
``src.config.env`` without forcing full settings initialization.
"""

from __future__ import annotations

from typing import Any

__all__ = ["settings"]


def __getattr__(name: str) -> Any:
    if name == "settings":
        from src.config.settings import settings as _settings

        return _settings
    raise AttributeError(name)
