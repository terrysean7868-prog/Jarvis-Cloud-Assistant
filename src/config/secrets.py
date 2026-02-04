from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.config import env


"""Centralized access to secrets/credentials.

This does NOT require a `.env` file.
If you run locally, you may still use `.env` (via python-dotenv) as a convenience,
otherwise set real env vars via your OS/hosting provider.

Non-secret behavior toggles should live in `src/config/runtime_defaults.py`.
"""


@dataclass(frozen=True)
class LLMSecrets:
    primary_api_key: Optional[str]
    backup_api_key: Optional[str]


def llm_secrets() -> LLMSecrets:
    primary = (env.get("PRIMARY_API_KEY") or env.get("OPENAI_API_KEY") or "").strip() or None
    backup = (env.get("BACKUP_API_KEY") or env.get("GROQ_API_KEY") or "").strip() or None
    return LLMSecrets(primary_api_key=primary, backup_api_key=backup)


@dataclass(frozen=True)
class N8NSecrets:
    base_url: str
    token: str
    secret: str


def n8n_secrets() -> N8NSecrets:
    return N8NSecrets(
        base_url=(env.get("JARVIS_N8N_WEBHOOK_BASE") or "").strip().rstrip("/"),
        token=(env.get("JARVIS_N8N_WEBHOOK_TOKEN") or "").strip(),
        secret=(env.get("JARVIS_N8N_WEBHOOK_SECRET") or "").strip(),
    )


@dataclass(frozen=True)
class TelegramSecrets:
    token: Optional[str]
    chat_id: Optional[str]


def telegram_secrets() -> TelegramSecrets:
    token = (env.get("TELEGRAM_TOKEN") or "").strip() or None
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip() or None
    return TelegramSecrets(token=token, chat_id=chat_id)


@dataclass(frozen=True)
class RenderSecrets:
    api_key: Optional[str]
    service_id: Optional[str]


def render_secrets() -> RenderSecrets:
    api_key = (env.get("RENDER_API_KEY") or "").strip() or None
    service_id = (env.get("RENDER_SERVICE_ID") or "").strip() or None
    return RenderSecrets(api_key=api_key, service_id=service_id)
