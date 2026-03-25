from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Any

try:
    from bson import ObjectId
except Exception:
    ObjectId = None

from .data_schemas import (
    REQUIRED_COLLECTIONS,
    ensure_collection_indexes,
    normalize_for_collection,
    sanitize_for_storage,
    validate_required_fields,
)


LEGACY_TO_TARGET = {
    "chat_history": "chat_logs",
    "tasks": "task_logs",
    "delegated_tasks": "task_logs",
    "requirements_audit": "requirement_logs",
    "system_events": "agent_logs",
    "voice_commands": "agent_logs",
    "module_changes": "agent_logs",
    "git_operations": "self_update_logs",
    "learning_examples": "learning_memory",
    "web_training_data": "training_events",
}

CHECKPOINT_COLLECTION = "migration_checkpoints"
CHECKPOINT_ID = "migration_checkpoint_legacy_v1"

# Safety guard: these collections are auth/session/device critical and must remain untouched.
AUTH_RUNTIME_CRITICAL_COLLECTIONS = {
    "auth_users",
    "sessions",
    "session_tokens",
    "user_device_links",
    "device_registry",
    "device_permissions",
    "agent_configs",
    "user_preferences",
}


@dataclass
class MigrationSummary:
    scanned: int = 0
    migrated: int = 0
    skipped: int = 0
    invalid: int = 0
    source_collection: str = ""
    target_collection: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_collection": self.source_collection,
            "target_collection": self.target_collection,
            "scanned": self.scanned,
            "migrated": self.migrated,
            "skipped": self.skipped,
            "invalid": self.invalid,
        }


def _target_for_legacy(source_collection: str, raw_doc: dict[str, Any]) -> str:
    if source_collection == "system_events":
        status = str(raw_doc.get("status") or "").strip().lower()
        event_type = str(raw_doc.get("event_type") or "").strip().lower()
        if status == "error" or "error" in event_type:
            return "error_logs"
        if "learning" in event_type:
            return "learning_memory"
        if "training" in event_type:
            return "training_events"
        if "update" in event_type or "rollback" in event_type:
            return "self_update_logs"
    return LEGACY_TO_TARGET.get(source_collection, "training_events")


def _as_object_id(value: Any):
    if ObjectId is None:
        return value
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _load_checkpoint(database: Any) -> dict[str, Any]:
    default = {
        "_id": CHECKPOINT_ID,
        "schema_version": 1,
        "type": "migration_checkpoint",
        "migration_name": "legacy_to_normalized_v1",
        "updated_at": _now_iso(),
        "status": "new",
        "collections": {},
    }
    try:
        row = database[CHECKPOINT_COLLECTION].find_one({"_id": CHECKPOINT_ID})
        if isinstance(row, dict):
            row.setdefault("collections", {})
            return row
    except Exception:
        pass
    return default


def _save_checkpoint(database: Any, checkpoint: dict[str, Any]) -> None:
    payload = dict(checkpoint or {})
    payload["_id"] = CHECKPOINT_ID
    payload["updated_at"] = _now_iso()
    database[CHECKPOINT_COLLECTION].update_one({"_id": CHECKPOINT_ID}, {"$set": payload}, upsert=True)


def _needs_migration(doc: dict[str, Any]) -> bool:
    if str(doc.get("migration_state") or "").strip().lower() == "normalized_v1":
        return False
    # Legacy collections may already contain normalized records; keep them untouched.
    if int(doc.get("schema_version") or 0) == 1 and str(doc.get("event_id") or "").strip():
        return False
    return True


def migrate_legacy_collections(
    database: Any,
    *,
    mark_source: bool = True,
    limit_per_collection: int = 0,
    dry_run: bool = False,
    batch_size: int = 500,
    max_batches_per_run: int = 20,
    resume_from_checkpoint: bool = True,
    ensure_indexes: bool = True,
) -> dict[str, Any]:
    if database is None:
        return {"status": "error", "message": "Database unavailable", "summaries": []}

    if ensure_indexes:
        ensure_collection_indexes(database)

    checkpoint = _load_checkpoint(database) if resume_from_checkpoint else {
        "_id": CHECKPOINT_ID,
        "collections": {},
        "status": "running",
        "migration_name": "legacy_to_normalized_v1",
    }
    checkpoint.setdefault("collections", {})
    checkpoint["status"] = "running"
    checkpoint["started_at"] = checkpoint.get("started_at") or _now_iso()

    if not dry_run:
        try:
            _save_checkpoint(database, checkpoint)
        except Exception:
            pass

    summaries: list[dict[str, Any]] = []
    batches_processed = 0
    fully_completed = True

    for source_collection in sorted(LEGACY_TO_TARGET.keys()):
        if source_collection in AUTH_RUNTIME_CRITICAL_COLLECTIONS:
            # Explicit skip safety guard even if mapping is changed in future.
            summaries.append(
                {
                    "source_collection": source_collection,
                    "target_collection": "",
                    "scanned": 0,
                    "migrated": 0,
                    "skipped": 0,
                    "invalid": 0,
                    "status": "skipped_auth_runtime_critical",
                }
            )
            continue

        source = database[source_collection]
        summary = MigrationSummary(source_collection=source_collection)

        ckp_state = checkpoint["collections"].get(source_collection, {}) if isinstance(checkpoint.get("collections"), dict) else {}
        last_id = _as_object_id(ckp_state.get("last_id")) if resume_from_checkpoint else None
        processed_for_collection = int(ckp_state.get("processed") or 0)

        try:
            query: dict[str, Any] = {
                "migration_state": {"$ne": "normalized_v1"},
            }
            if last_id is not None:
                query["_id"] = {"$gt": last_id}

            cursor = source.find(query).sort("_id", 1)
            if int(limit_per_collection) > 0:
                cursor = cursor.limit(max(1, int(limit_per_collection)))
        except Exception:
            summaries.append({**summary.to_dict(), "error": "failed_to_query_source"})
            fully_completed = False
            continue

        batch: list[dict[str, Any]] = []
        exhausted = False
        while True:
            batch.clear()
            try:
                for _ in range(max(1, int(batch_size))):
                    raw = next(cursor, None)
                    if raw is None:
                        exhausted = True
                        break
                    batch.append(dict(raw or {}))
            except Exception:
                exhausted = True

            if not batch:
                break

            for doc in batch:
                summary.scanned += 1
                source_id = doc.get("_id")
                if not _needs_migration(doc):
                    summary.skipped += 1
                    continue

                target_collection = _target_for_legacy(source_collection, doc)
                summary.target_collection = target_collection

                normalized = normalize_for_collection(target_collection, doc)
                ok, _missing = validate_required_fields(normalized)
                if not ok:
                    summary.invalid += 1
                    continue

                normalized = sanitize_for_storage(normalized)
                normalized["migration"] = {
                    "migrated_from": source_collection,
                    "source_id": str(source_id) if source_id is not None else None,
                    "migrated_at": _now_iso(),
                }
                target = database[target_collection]
                try:
                    if not dry_run:
                        target.update_one(
                            {"event_id": normalized["event_id"]},
                            {"$set": normalized},
                            upsert=True,
                        )
                        if mark_source and source_id is not None:
                            source.update_one(
                                {"_id": source_id},
                                {
                                    "$set": {
                                        "migration_state": "normalized_v1",
                                        "migration_target_collection": target_collection,
                                        "migration_event_id": normalized.get("event_id"),
                                    }
                                },
                                upsert=False,
                            )
                    summary.migrated += 1
                except BaseException:
                    summary.skipped += 1

            processed_for_collection += len(batch)
            batches_processed += 1

            if not dry_run:
                try:
                    latest = batch[-1] if batch else {}
                    checkpoint["collections"][source_collection] = {
                        "last_id": str(latest.get("_id")) if latest.get("_id") is not None else ckp_state.get("last_id"),
                        "processed": processed_for_collection,
                        "updated_at": _now_iso(),
                    }
                    checkpoint["status"] = "running"
                    _save_checkpoint(database, checkpoint)
                except Exception:
                    pass

            if batches_processed >= max(1, int(max_batches_per_run)):
                fully_completed = False
                exhausted = False
                break

            if exhausted:
                break

        if not exhausted and batches_processed >= max(1, int(max_batches_per_run)):
            summaries.append({**summary.to_dict(), "status": "paused_checkpointed"})
            break

        summaries.append(summary.to_dict())

    if not dry_run:
        try:
            checkpoint["status"] = "completed" if fully_completed else "partial"
            checkpoint["finished_at"] = _now_iso()
            _save_checkpoint(database, checkpoint)
        except Exception:
            pass

    return {
        "status": "success",
        "dry_run": bool(dry_run),
        "batch_size": int(max(1, int(batch_size))),
        "max_batches_per_run": int(max(1, int(max_batches_per_run))),
        "batches_processed": batches_processed,
        "completed": fully_completed,
        "checkpoint_id": CHECKPOINT_ID,
        "required_collections": list(REQUIRED_COLLECTIONS),
        "auth_runtime_critical_collections_preserved": sorted(AUTH_RUNTIME_CRITICAL_COLLECTIONS),
        "summaries": summaries,
    }
