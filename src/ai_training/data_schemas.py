from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = 1
REQUIRED_COLLECTIONS = (
    "chat_logs",
    "task_logs",
    "agent_logs",
    "error_logs",
    "requirement_logs",
    "self_update_logs",
    "training_events",
    "learning_memory",
    "datasets_metadata",
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.astimezone(UTC).isoformat()
    s = str(value or "").strip()
    if not s:
        return _utc_now_iso()
    # Accept legacy Z format and plain datetime strings.
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(UTC).isoformat()
    except Exception:
        return _utc_now_iso()


def _stable_event_id(payload: dict[str, Any]) -> str:
    src = "|".join(
        [
            str(payload.get("timestamp") or ""),
            str(payload.get("user_id") or ""),
            str(payload.get("session_id") or ""),
            str(payload.get("source") or ""),
            str(payload.get("type") or payload.get("event_type") or ""),
            str(payload.get("message") or payload.get("description") or payload.get("user_input") or ""),
        ]
    )
    digest = hashlib.sha1(src.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"ev_{digest}"


def _normalize_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"cloud", "local"}:
        return v
    return "local"


def _normalize_source(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v in {"chat", "task", "agent", "system"}:
        return v
    return "system"


def _pick(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload and payload.get(key) not in {None, ""}:
            return payload.get(key)
    return default


def normalize_for_collection(collection: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    p = dict(payload or {})

    timestamp = normalize_timestamp(_pick(p, "timestamp", "ts", "created_at", "updated_at"))
    user_id = str(_pick(p, "user_id", "owner", "username", default="system") or "system").strip().lower()
    session_id = str(_pick(p, "session_id", default="unknown") or "unknown").strip()
    correlation_id = str(
        _pick(p, "correlation_id", "request_id", "task_id", "context_id", "id", default="") or ""
    ).strip()
    if not correlation_id:
        correlation_id = f"corr_{uuid.uuid4().hex[:12]}"

    lifecycle_state = str(
        _pick(p, "lifecycle_state", "status", "state", default="recorded") or "recorded"
    ).strip().lower()

    result_status = str(
        _pick(p, "result_status", "status", "result", default="") or ""
    ).strip().lower()
    if not result_status:
        if lifecycle_state in {"completed", "success", "done", "ok"}:
            result_status = "success"
        elif lifecycle_state in {"failed", "error", "stopped", "blocked", "denied"}:
            result_status = "failed"
        elif lifecycle_state in {"pending", "queued_for_agent", "awaiting_agent", "executing", "in_progress", "paused"}:
            result_status = "in_progress"
        else:
            result_status = "recorded"

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(_pick(p, "event_id", default="") or "").strip() or _stable_event_id({**p, "timestamp": timestamp}),
        "timestamp": timestamp,
        "user_id": user_id,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "source": _normalize_source(_pick(p, "source", "channel", default="system")),
        "mode": _normalize_mode(_pick(p, "mode", default="local")),
        "lifecycle_state": lifecycle_state,
        "result_status": result_status,
        "collection": collection,
        "type": str(_pick(p, "type", "event_type", "requirement_type", default="general") or "general"),
        "message": str(
            _pick(
                p,
                "message",
                "description",
                "requested_action",
                "command_text",
                "user_input",
                default="",
            )
            or ""
        ).strip(),
        "payload": p,
    }
    return normalized


def validate_required_fields(doc: dict[str, Any]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for key in [
        "schema_version",
        "event_id",
        "timestamp",
        "user_id",
        "session_id",
        "correlation_id",
        "source",
        "mode",
        "lifecycle_state",
        "result_status",
    ]:
        if key not in doc or doc.get(key) in {None, ""}:
            missing.append(key)
    return (len(missing) == 0, missing)


def sanitize_for_storage(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc)
    payload = out.get("payload")
    if isinstance(payload, dict):
        # Keep payload compact and avoid huge unbounded legacy blobs.
        if len(payload) > 200:
            out["payload"] = {k: payload[k] for k in list(payload.keys())[:200]}
    return out


def ensure_collection_indexes(database: Any) -> None:
    if database is None:
        return
    for name in REQUIRED_COLLECTIONS:
        col = database[name]
        try:
            col.create_index("event_id", unique=True)
        except Exception:
            pass
        try:
            col.create_index("timestamp")
        except Exception:
            pass
        try:
            col.create_index("session_id")
        except Exception:
            pass
        try:
            col.create_index("correlation_id")
        except Exception:
            pass
        try:
            col.create_index([("type", 1), ("source", 1)])
        except Exception:
            pass
