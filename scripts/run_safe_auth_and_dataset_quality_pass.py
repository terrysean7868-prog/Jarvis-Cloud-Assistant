from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ai_training.data_migrator import AUTH_RUNTIME_CRITICAL_COLLECTIONS
from src.ai_training.dataset_builder import build_datasets
from src.model_ops.finetune_dataset_checker import inspect_dataset
from src.utils.db import db

STANDARD_AUTH_FIELDS = {
    "user_id",
    "username",
    "email",
    "role",
    "is_active",
    "created_at",
    "updated_at",
    "last_login_at",
    "auth_provider",
    "metadata",
    "schema_version",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _safe_iso(value: Any, *, fallback: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).astimezone(UTC).isoformat()
    except Exception:
        return fallback


def _dataset_counts_from_disk(base_dir: Path) -> dict[str, int]:
    names = {
        "instruction_dataset": "instruction_dataset.jsonl",
        "conversation_dataset": "conversation_dataset.jsonl",
        "task_dataset": "task_dataset.jsonl",
        "error_dataset": "error_dataset.jsonl",
        "rag_documents": "rag_documents.jsonl",
    }
    out: dict[str, int] = {}
    for key, fname in names.items():
        p = base_dir / fname
        if not p.exists():
            out[key] = 0
            continue
        try:
            with p.open("r", encoding="utf-8") as fh:
                out[key] = sum(1 for line in fh if line.strip())
        except Exception:
            out[key] = 0
    return out


def _count_collections(database: Any, names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        try:
            out[name] = int(database[name].count_documents({}))
        except Exception:
            out[name] = -1
    return out


def _resolve_username(doc: dict[str, Any]) -> str:
    existing = str(doc.get("username") or "").strip()
    if existing:
        return existing
    from_name = str(doc.get("name") or "").strip().lower()
    if from_name:
        return from_name
    from_user_id = str(doc.get("user_id") or doc.get("id") or "").strip().lower()
    if from_user_id:
        return from_user_id
    if doc.get("_id") is not None:
        return str(doc.get("_id")).strip().lower()
    return ""


def _resolve_user_id(doc: dict[str, Any], username: str) -> str:
    candidate = str(doc.get("user_id") or doc.get("id") or "").strip()
    if candidate:
        return candidate
    if doc.get("_id") is not None:
        return str(doc.get("_id")).strip()
    return username.strip().lower()


def _normalize_auth_users(database: Any) -> dict[str, Any]:
    users_col = database["auth_users"]
    docs = list(users_col.find({}))

    existing_username_owner: dict[str, str] = {}
    for d in docs:
        uname = str(d.get("username") or "").strip().lower()
        if uname:
            existing_username_owner[uname] = str(d.get("_id"))

    now = _now_iso()
    normalized = 0
    skipped = 0
    risky_skipped: list[dict[str, str]] = []
    added_defaults: dict[str, int] = {k: 0 for k in STANDARD_AUTH_FIELDS}

    for d in docs:
        doc = dict(d or {})
        oid = doc.get("_id")
        oid_s = str(oid)

        username = _resolve_username(doc)
        if not username:
            skipped += 1
            risky_skipped.append({"_id": oid_s, "reason": "missing_username_and_name"})
            continue

        role_raw = str(doc.get("role") or "").strip().lower()
        if role_raw and role_raw not in {"user", "admin"}:
            skipped += 1
            risky_skipped.append({"_id": oid_s, "username": username, "reason": f"unsupported_role:{role_raw}"})
            continue

        owner = existing_username_owner.get(username.strip().lower())
        if owner and owner != oid_s:
            skipped += 1
            risky_skipped.append({"_id": oid_s, "username": username, "reason": "username_collision"})
            continue

        created_source = doc.get("created_at") or doc.get("createdAt") or doc.get("registered_at") or doc.get("last_login")
        created_at = _safe_iso(created_source, fallback=now)
        updated_at = _safe_iso(doc.get("updated_at") or doc.get("updatedAt") or now, fallback=now)

        last_login_at_raw = doc.get("last_login_at") or doc.get("last_login")
        last_login_at = _safe_iso(last_login_at_raw, fallback=updated_at) if last_login_at_raw else None

        email_raw = doc.get("email")
        email = str(email_raw).strip() if email_raw not in {None, ""} else None

        auth_provider = str(doc.get("auth_provider") or "").strip().lower() or "local"
        if auth_provider not in {"local", "other"}:
            auth_provider = "other"

        existing_meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        unmapped_fields: dict[str, Any] = {}
        for key, value in doc.items():
            if key in STANDARD_AUTH_FIELDS or key == "_id":
                continue
            if key == "metadata":
                continue
            if "password" in key.lower():
                unmapped_fields[key] = "<redacted>"
            else:
                unmapped_fields[key] = value

        merged_metadata = dict(existing_meta)
        if unmapped_fields:
            merged_metadata["unmapped_fields"] = unmapped_fields
        merged_metadata["normalization"] = {
            "status": "normalized_safe_v1",
            "normalized_at": now,
            "source": "run_safe_auth_and_dataset_quality_pass",
        }

        user_id = _resolve_user_id(doc, username)
        role = role_raw or "user"
        is_active = bool(doc.get("is_active")) if "is_active" in doc else True

        set_fields: dict[str, Any] = {
            "user_id": user_id,
            "username": username,
            "email": email,
            "role": role,
            "is_active": is_active,
            "created_at": created_at,
            "updated_at": now,
            "auth_provider": auth_provider,
            "metadata": merged_metadata,
            "schema_version": int(doc.get("schema_version") or 1),
        }
        if last_login_at:
            set_fields["last_login_at"] = last_login_at

        for field in STANDARD_AUTH_FIELDS:
            if field not in doc:
                added_defaults[field] += 1

        users_col.update_one({"_id": oid}, {"$set": set_fields}, upsert=False)
        normalized += 1

    return {
        "total_users": len(docs),
        "normalized_users": normalized,
        "skipped_users": skipped,
        "added_or_defaulted_fields": {k: v for k, v in added_defaults.items() if v > 0},
        "risky_records_skipped": risky_skipped,
    }


def _safe_auth_validation(database: Any, auth_before: dict[str, Any], auth_after: dict[str, Any]) -> dict[str, Any]:
    users_col = database["auth_users"]
    before_usernames = set(auth_before.get("usernames") or [])
    after_usernames = set(auth_after.get("usernames") or [])
    before_admins = set(auth_before.get("admins") or [])
    after_admins = set(auth_after.get("admins") or [])

    sessions_collections = sorted(list(AUTH_RUNTIME_CRITICAL_COLLECTIONS - {"auth_users"}))
    pre_counts = auth_before.get("critical_counts", {})
    post_counts = _count_collections(database, sessions_collections)
    unchanged = {k: pre_counts.get(k) == post_counts.get(k) for k in sessions_collections}

    docs_missing_username = int(users_col.count_documents({"$or": [{"username": {"$exists": False}}, {"username": ""}, {"username": None}]}))

    return {
        "all_existing_users_still_present": before_usernames.issubset(after_usernames),
        "admin_users_preserved": before_admins.issubset(after_admins),
        "missing_username_docs": docs_missing_username,
        "session_and_runtime_critical_collections_unchanged": unchanged,
        "all_critical_collections_unchanged": all(unchanged.values()) if unchanged else True,
        "login_compatibility_proxy": {
            "user_count_unchanged": auth_before.get("count") == auth_after.get("count"),
            "all_usernames_resolvable": docs_missing_username == 0,
            "note": "Credential-level login replay is intentionally not executed in this safe data pass.",
        },
    }


def run() -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "error",
        "auth_users": {},
        "dataset_quality": {},
        "validation": {},
        "runtime_regression": {},
    }

    db._ensure_connected()
    if db.db is None:
        report["message"] = "Database unavailable"
        return report

    users_col = db.db["auth_users"]
    before_users = list(users_col.find({}))
    sessions_collections = sorted(list(AUTH_RUNTIME_CRITICAL_COLLECTIONS - {"auth_users"}))

    auth_before = {
        "count": len(before_users),
        "usernames": sorted(list({str(d.get("username") or "").strip() for d in before_users if str(d.get("username") or "").strip()})),
        "admins": sorted(list({str(d.get("username") or "").strip() for d in before_users if str(d.get("role") or "").strip().lower() == "admin"})),
        "critical_counts": _count_collections(db.db, sessions_collections),
    }

    auth_changes = _normalize_auth_users(db.db)

    after_users = list(users_col.find({}))
    auth_after = {
        "count": len(after_users),
        "usernames": sorted(list({str(d.get("username") or "").strip() for d in after_users if str(d.get("username") or "").strip()})),
        "admins": sorted(list({str(d.get("username") or "").strip() for d in after_users if str(d.get("role") or "").strip().lower() == "admin"})),
    }

    datasets_dir = Path("data/ai_training/datasets")
    before_counts = _dataset_counts_from_disk(datasets_dir)
    before_stats = inspect_dataset(str(datasets_dir))

    build_result = build_datasets(db.db, output_dir=datasets_dir, limit_per_collection=50000)

    after_counts = _dataset_counts_from_disk(datasets_dir)
    after_stats = inspect_dataset(str(datasets_dir))

    before_dup = float(before_stats.get("duplicate_rate") or 0.0)
    after_dup = float(after_stats.get("duplicate_rate") or 0.0)
    reduction_pct = 0.0
    if before_dup > 0:
        reduction_pct = round(((before_dup - after_dup) / before_dup) * 100.0, 2)

    report["auth_users"] = {
        "schema": {
            "required_fields": [
                "user_id",
                "username",
                "email",
                "role",
                "is_active",
                "created_at",
                "updated_at",
                "last_login_at",
                "auth_provider",
                "metadata",
                "schema_version",
            ],
            "normalization_policy": "additive_non_destructive",
        },
        "changes": auth_changes,
        "before": {
            "user_count": auth_before["count"],
            "admin_count": len(auth_before["admins"]),
        },
        "after": {
            "user_count": auth_after["count"],
            "admin_count": len(auth_after["admins"]),
        },
    }

    report["dataset_quality"] = {
        "counts_before": before_counts,
        "counts_after": after_counts,
        "duplicate_rate_before": before_dup,
        "duplicate_rate_after": after_dup,
        "duplicate_reduction_percent": reduction_pct,
        "builder_counts": build_result.get("counts", {}),
    }

    report["validation"] = _safe_auth_validation(db.db, auth_before, auth_after)

    # Lightweight runtime safety signal after normalization/build (non-invasive).
    try:
        from src.core.llm_adapter import LLMAdapter

        adapter = LLMAdapter()
        route = adapter._resolve_model_ops_route("hello there", "chat")
        report["runtime_regression"] = {
            "status": "ok",
            "db_connected": db.db is not None,
            "routing_enabled": bool(getattr(adapter, "model_ops_routing_enabled", False)),
            "route_available": isinstance(route, dict),
        }
    except Exception as exc:
        report["runtime_regression"] = {
            "status": "not_verified",
            "error": str(exc),
        }

    report["status"] = "success"
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
