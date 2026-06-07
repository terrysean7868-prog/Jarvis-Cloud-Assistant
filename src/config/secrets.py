from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from . import env


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
    self_hosted_api_key: Optional[str]


def llm_secrets() -> LLMSecrets:
    primary = (env.get("OPENAI_API_KEY") or "").strip() or None
    backup = (env.get("GROQ_API_KEY") or "").strip() or None
    # Optional key for self-hosted OpenAI-compatible endpoints.
    # Read directly so strict env whitelisting can stay minimal.
    self_hosted = (os.getenv("SELF_HOSTED_LLM_API_KEY") or "").strip() or None
    return LLMSecrets(primary_api_key=primary, backup_api_key=backup, self_hosted_api_key=self_hosted)


@dataclass(frozen=True)
class N8NSecrets:
    base_url: str
    token: str
    secret: str


def n8n_secrets() -> N8NSecrets:
    return N8NSecrets(
        base_url="",
        token="",
        secret="",
    )


@dataclass(frozen=True)
class TelegramSecrets:
    token: Optional[str]
    chat_id: Optional[str]


def telegram_secrets() -> TelegramSecrets:
    token = (env.get("TELEGRAM_TOKEN") or "").strip() or None
    chat_id = None
    return TelegramSecrets(token=token, chat_id=chat_id)


@dataclass(frozen=True)
class RenderSecrets:
    api_key: Optional[str]
    service_id: Optional[str]


def render_secrets() -> RenderSecrets:
    api_key = None
    service_id = None
    return RenderSecrets(api_key=api_key, service_id=service_id)
