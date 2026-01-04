"""Canonical schema helpers for `web_training_data`.

Goal:
- Keep a single, versioned data shape for any web/Wikipedia-ingested knowledge.
- Include lightweight producer identity fields so it's easy to audit/maintain.
- Normalize empty strings to None and clamp text lengths to stay compact.

This module is intentionally dependency-light (no Pydantic) and can be used
from DB, jobs, and API layers.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any


WEB_TRAINING_SCHEMA_VERSION = 1
WEB_TRAINING_DOC_TYPE = "web_training_item"


def _now_utc_naive() -> datetime:
    # Existing codebase uses naive UTC datetimes (datetime.utcnow). Keep consistent.
    return datetime.utcnow()


def _clamp_text(value: str | None, max_len: int) -> str | None:
    s = (value or "").strip()
    if not s:
        return None
    if max_len > 0:
        s = s[:max_len]
    return s


def jarvis_identity() -> dict[str, str]:
    """Return a stable-ish identity blob for writes.

    Values are best-effort and safe to store:
    - assistant_id: logical assistant name (env override)
    - instance_id: machine/app instance identifier (env override)
    """

    assistant_id = (os.getenv("JARVIS_ASSISTANT_ID") or "jarvis").strip() or "jarvis"

    instance_id = (
        os.getenv("JARVIS_INSTANCE_ID")
        or os.getenv("COMPUTERNAME")  # Windows
        or os.getenv("HOSTNAME")  # Linux/macOS
        or "local"
    )
    instance_id = (instance_id or "local").strip() or "local"

    return {"assistant_id": assistant_id, "instance_id": instance_id}


def normalize_web_training_item(
    *,
    topic: str,
    title: str | None,
    snippet: str | None,
    summary: str | None,
    url: str | None,
    source: str = "web",
    fetched_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Normalize inputs into the canonical document shape.

    Returns None if required fields are missing.
    """

    topic_norm = (topic or "").strip()
    url_norm = (url or "").strip()
    if not topic_norm or not url_norm:
        return None

    ident = jarvis_identity()

    doc: dict[str, Any] = {
        "doc_type": WEB_TRAINING_DOC_TYPE,
        "schema_version": WEB_TRAINING_SCHEMA_VERSION,
        "producer_assistant_id": ident["assistant_id"],
        "producer_instance_id": ident["instance_id"],
        "topic": topic_norm,
        "title": _clamp_text(title, 300),
        "snippet": _clamp_text(snippet, 500),
        "summary": _clamp_text(summary, 1200),
        "url": url_norm,
        "source": _clamp_text(source, 80) or "web",
        "fetched_at": fetched_at or _now_utc_naive(),
        "updated_at": _now_utc_naive(),
    }

    # Remove None fields to keep docs compact.
    for k in ["title", "snippet", "summary"]:
        if doc.get(k) is None:
            doc.pop(k, None)

    if extra and isinstance(extra, dict):
        # Only merge safe primitive-ish fields; skip large/nested blobs.
        for k, v in list(extra.items()):
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                doc[k] = v
            elif isinstance(v, list) and len(v) <= 50:
                doc[k] = v

    return doc
