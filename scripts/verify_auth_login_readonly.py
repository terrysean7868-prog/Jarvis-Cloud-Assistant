from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from src.ai_training.data_migrator import AUTH_RUNTIME_CRITICAL_COLLECTIONS
from src.utils.db import db
from src.utils.voice_auth import (
    VOICE_HASH_PREFIX_MATCH,
    VOICE_TEXT_SIMILARITY_THRESHOLD,
    _hash_normalized_text,
    _hash_password,
    _normalize_voice_hash,
    _norm_text,
    _text_similarity,
)


REQUIRED_FIELDS = {
    "user_id",
    "username",
    "email",
    "role",
    "is_active",
    "created_at",
    "updated_at",
    "auth_provider",
    "metadata",
    "schema_version",
}


def _count_collections(database: Any, names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        try:
            out[name] = int(database[name].count_documents({}))
        except Exception:
            out[name] = -1
    return out


def _valid_iso(s: Any) -> bool:
    raw = str(s or "").strip()
    if not raw:
        return False
    raw = raw.replace("Z", "+00:00")
    try:
        from datetime import datetime

        datetime.fromisoformat(raw)
        return True
    except Exception:
        return False


def _compare_voice_hashes(stored_hash: str, provided_hash: str) -> bool:
    stored = _normalize_voice_hash(stored_hash)
    provided = _normalize_voice_hash(provided_hash)
    if not stored or not provided:
        return False
    if stored == provided:
        return True
    if VOICE_HASH_PREFIX_MATCH:
        return stored.startswith(provided) or provided.startswith(stored)
    return False


def _voice_match_readonly(user: dict[str, Any], voice_sample_hash: str, voice_sample_text: str | None) -> bool:
    if not user:
        return False

    provided_text = _norm_text(voice_sample_text or "")
    samples = list(user.get("voice_samples") or [])
    if not samples and user.get("voice_hash"):
        samples.append({"hash": user.get("voice_hash"), "text": None})

    if provided_text:
        best = 0.0
        for sample in samples:
            t = sample.get("text")
            if not t:
                continue
            best = max(best, _text_similarity(t, provided_text))
            if best >= VOICE_TEXT_SIMILARITY_THRESHOLD:
                return True

    provided_hash = _normalize_voice_hash(voice_sample_hash)
    if not provided_hash and provided_text:
        provided_hash = _hash_normalized_text(provided_text)

    for sample in samples:
        if _compare_voice_hashes(str(sample.get("hash") or ""), provided_hash or ""):
            return True
    return False


def _validate_schema(users: list[dict[str, Any]]) -> dict[str, Any]:
    missing_required: list[dict[str, Any]] = []
    invalid_records: list[dict[str, Any]] = []

    usernames = [str(u.get("username") or "").strip().lower() for u in users]
    counts = Counter([u for u in usernames if u])
    dup_usernames = sorted([u for u, c in counts.items() if c > 1])

    for doc in users:
        uname = str(doc.get("username") or "").strip()
        missing = sorted([k for k in REQUIRED_FIELDS if k not in doc])
        if missing:
            missing_required.append({"username": uname, "missing": missing})

        role = str(doc.get("role") or "").strip().lower()
        if role not in {"user", "admin"}:
            invalid_records.append({"username": uname, "reason": f"invalid_role:{role or 'empty'}"})

        if not _valid_iso(doc.get("created_at")):
            invalid_records.append({"username": uname, "reason": "invalid_created_at"})

        if not _valid_iso(doc.get("updated_at")):
            invalid_records.append({"username": uname, "reason": "invalid_updated_at"})

        auth_provider = str(doc.get("auth_provider") or "").strip().lower()
        if auth_provider not in {"local", "other"}:
            invalid_records.append({"username": uname, "reason": f"invalid_auth_provider:{auth_provider or 'empty'}"})

        if "password_hash" in doc and not str(doc.get("password_salt") or "").strip():
            invalid_records.append({"username": uname, "reason": "password_hash_without_salt"})

        has_voice_hash = bool(_normalize_voice_hash(doc.get("voice_hash")))
        has_voice_samples = isinstance(doc.get("voice_samples"), list) and any(
            bool(_normalize_voice_hash((s or {}).get("hash")))
            for s in (doc.get("voice_samples") or [])
            if isinstance(s, dict)
        )
        if not (has_voice_hash or has_voice_samples):
            invalid_records.append({"username": uname, "reason": "missing_voice_factor"})

    return {
        "missing_required_count": len(missing_required),
        "missing_required": missing_required,
        "duplicate_username_count": len(dup_usernames),
        "duplicate_usernames": dup_usernames,
        "invalid_record_count": len(invalid_records),
        "invalid_records": invalid_records,
    }


def _load_replay_fixtures(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return [c for c in data.get("cases") if isinstance(c, dict)]
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    return []


def _replay_readonly(users_by_name: dict[str, dict[str, Any]], fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0

    for case in fixtures:
        username = str(case.get("username") or "").strip().lower()
        if not username:
            failed += 1
            results.append({"username": "", "ok": False, "reason": "missing_username_in_fixture"})
            continue

        user = users_by_name.get(username)
        if not user:
            failed += 1
            results.append({"username": username, "ok": False, "reason": "user_not_found"})
            continue

        if user.get("password_hash"):
            password = case.get("password")
            if not isinstance(password, str) or not password:
                failed += 1
                results.append({"username": username, "ok": False, "reason": "password_required_by_user_but_missing_in_fixture"})
                continue
            _, hashed = _hash_password(password, salt=str(user.get("password_salt") or ""))
            if hashed != str(user.get("password_hash") or ""):
                failed += 1
                results.append({"username": username, "ok": False, "reason": "password_mismatch"})
                continue

        voice_hash = str(case.get("voice_sample_hash") or "")
        voice_text = case.get("voice_sample_text")
        ok_voice = _voice_match_readonly(user, voice_hash, voice_text if isinstance(voice_text, str) else None)
        if not ok_voice:
            failed += 1
            results.append({"username": username, "ok": False, "reason": "voice_mismatch"})
            continue

        passed += 1
        results.append({"username": username, "ok": True})

    return {
        "total_cases": len(fixtures),
        "passed": passed,
        "failed": failed,
        "all_passed": failed == 0,
        "results": results,
    }


def run(*, replay: bool = False, fixtures_path: str = "data/auth_login_replay_candidates.json") -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "error",
        "mode": "readonly",
        "schema_validation": {},
        "replay_validation": {},
        "safety": {},
        "summary": {},
    }

    db._ensure_connected()
    if db.db is None:
        report["message"] = "Database unavailable"
        return report

    users_col = db.db["auth_users"]
    users = list(users_col.find({}))

    critical = sorted(list(AUTH_RUNTIME_CRITICAL_COLLECTIONS - {"auth_users"}))
    pre_counts = _count_collections(db.db, critical)

    users_by_name: dict[str, dict[str, Any]] = {}
    for doc in users:
        uname = str(doc.get("username") or "").strip().lower()
        if uname:
            users_by_name[uname] = doc

    schema_validation = _validate_schema(users)

    replay_validation = {
        "enabled": bool(replay),
        "fixtures_path": fixtures_path,
        "total_cases": 0,
        "passed": 0,
        "failed": 0,
        "all_passed": True,
        "results": [],
        "note": "Replay checks are optional and read-only. Provide fixtures to enable credential-level verification.",
    }

    if replay:
        fixtures = _load_replay_fixtures(Path(fixtures_path))
        replay_validation = {
            "enabled": True,
            "fixtures_path": fixtures_path,
            **_replay_readonly(users_by_name, fixtures),
        }

    post_counts = _count_collections(db.db, critical)
    unchanged = {k: pre_counts.get(k) == post_counts.get(k) for k in critical}

    report["schema_validation"] = schema_validation
    report["replay_validation"] = replay_validation
    report["safety"] = {
        "critical_collections_pre": pre_counts,
        "critical_collections_post": post_counts,
        "critical_collections_unchanged": unchanged,
        "all_critical_collections_unchanged": all(unchanged.values()) if unchanged else True,
        "write_operations_performed": False,
    }

    hard_fail = (
        schema_validation.get("missing_required_count", 0) > 0
        or schema_validation.get("duplicate_username_count", 0) > 0
        or schema_validation.get("invalid_record_count", 0) > 0
        or not report["safety"]["all_critical_collections_unchanged"]
        or (replay and not replay_validation.get("all_passed", False))
    )

    report["summary"] = {
        "users_scanned": len(users),
        "admins": sum(1 for u in users if str(u.get("role") or "").strip().lower() == "admin"),
        "schema_pass": not (
            schema_validation.get("missing_required_count", 0)
            or schema_validation.get("duplicate_username_count", 0)
            or schema_validation.get("invalid_record_count", 0)
        ),
        "replay_pass": bool(replay_validation.get("all_passed", True)),
        "overall_pass": not hard_fail,
    }

    report["status"] = "success" if not hard_fail else "failed"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only auth login verifier (schema + optional credential replay fixtures)."
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="Enable credential replay checks using fixture file (read-only).",
    )
    parser.add_argument(
        "--fixtures",
        default="data/auth_login_replay_candidates.json",
        help="Path to replay fixture JSON file.",
    )
    args = parser.parse_args()

    result = run(replay=bool(args.replay), fixtures_path=str(args.fixtures or ""))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
