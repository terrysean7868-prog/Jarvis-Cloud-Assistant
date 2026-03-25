from __future__ import annotations

import hashlib
import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .data_schemas import normalize_for_collection


logger = logging.getLogger(__name__)


DB_SOURCE_COLLECTIONS = (
    "chat_logs",
    "task_logs",
    "agent_logs",
    "error_logs",
    "training_events",
    "learning_memory",
)
def _resolve_runtime_database(database: Any) -> tuple[Any, bool]:
    if database is not None:
        return database, True
    try:
        from src.utils.db import db as runtime_db

        runtime_db._ensure_connected()
        live_db = getattr(runtime_db, "db", None)
        if live_db is not None:
            return live_db, True
    except Exception:
        pass
    return None, False


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _iter_normalized(db: Any, collection: str, *, limit: int = 10000) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for row in db[collection].find({}).sort("timestamp", -1).limit(max(1, int(limit))):
            d = dict(row or {})
            d.pop("_id", None)
            if int(d.get("schema_version") or 0) != 1:
                # Backward compatibility: normalize legacy docs on read.
                d = normalize_for_collection(collection, d)
            out.append(d)
    except Exception:
        return []
    return out


def _iter_learning_memory_raw(db: Any, *, limit: int = 10000) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        for row in db["learning_memory"].find({}).sort("updated_at", -1).limit(max(1, int(limit))):
            d = dict(row or {})
            d.pop("_id", None)
            out.append(d)
    except Exception:
        return []
    return out


def _dedupe_learning_memory(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r.get("pattern_key") or "").strip()
        if not key:
            key = hashlib.sha1(
                f"{str(r.get('pattern_type') or '')}|{_norm_text(str(r.get('input_pattern') or r.get('message') or ''))}".encode(
                    "utf-8", errors="ignore"
                )
            ).hexdigest()[:24]
        prev = best.get(key)
        if prev is None:
            best[key] = r
            continue
        prev_score = float(prev.get("priority_score") or 0.0) + (float(prev.get("quality_score") or 0.0) * 0.5)
        curr_score = float(r.get("priority_score") or 0.0) + (float(r.get("quality_score") or 0.0) * 0.5)
        if curr_score >= prev_score:
            best[key] = r
    return list(best.values())


def _guess_collection_from_filename(path: Path) -> str:
    name = path.name.lower()
    stem = path.stem.lower()
    text = f"{name} {stem}"
    if "chat" in text or "conversation" in text:
        return "chat_logs"
    if "task" in text or "delegate" in text or "action" in text:
        return "task_logs"
    if "error" in text or "exception" in text or "fail" in text:
        return "error_logs"
    if "agent" in text or "device" in text:
        return "agent_logs"
    if "learn" in text or "memory" in text:
        return "learning_memory"
    return "training_events"


def _read_json_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return out
    if isinstance(raw, dict):
        out.append(raw)
    elif isinstance(raw, list):
        for row in raw:
            if isinstance(row, dict):
                out.append(row)
    return out


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except Exception:
        return []
    return out


def _iter_file_normalized(
    collection: str,
    *,
    limit: int,
    fallback_root: Path | None,
) -> list[dict[str, Any]]:
    root = fallback_root or Path(__file__).resolve().parents[2]
    candidate_dirs = [
        root / "data" / "ai_training" / "raw_logs",
        root / "data" / "ai_training" / "datasets",
        root / "data" / "ai_training" / "exports",
        root / "data",
    ]
    explicit_candidates = [
        root / "train.jsonl",
        root / "eval.jsonl",
        root / "data" / "train.jsonl",
        root / "data" / "eval.jsonl",
    ]

    out: list[dict[str, Any]] = []

    def _append_rows(rows: list[dict[str, Any]], source_collection: str) -> None:
        for row in rows:
            normalized = normalize_for_collection(source_collection, row)
            out.append(normalized)
            if len(out) >= max(1, int(limit)):
                return

    for path in explicit_candidates:
        if len(out) >= max(1, int(limit)):
            break
        if not path.exists() or not path.is_file():
            continue
        src_collection = _guess_collection_from_filename(path)
        if src_collection != collection:
            continue
        rows = _read_jsonl_rows(path) if path.suffix.lower() == ".jsonl" else _read_json_rows(path)
        _append_rows(rows, src_collection)

    for base in candidate_dirs:
        if len(out) >= max(1, int(limit)):
            break
        if not base.exists() or not base.is_dir():
            continue
        for path in base.rglob("*"):
            if len(out) >= max(1, int(limit)):
                break
            if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl"}:
                continue
            src_collection = _guess_collection_from_filename(path)
            if src_collection != collection:
                continue
            rows = _read_jsonl_rows(path) if path.suffix.lower() == ".jsonl" else _read_json_rows(path)
            _append_rows(rows, src_collection)

    return out


def _norm_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    # Remove volatile values so similar operational logs can be deduped safely.
    text = re.sub(r"\b[0-9a-f]{8,}\b", " ", text)
    text = re.sub(r"\b\d{3,}\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _content_hash(row: dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    source = " | ".join(
        [
            _norm_text(row.get("type") or ""),
            _norm_text(row.get("message") or ""),
            _norm_text(payload.get("source_text") or ""),
            _norm_text(payload.get("description") or ""),
            _norm_text(payload.get("bot_response") or payload.get("result") or ""),
        ]
    )
    return hashlib.sha1(source.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _semantic_signature(row: dict[str, Any]) -> str:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    action_types = []
    if isinstance(payload.get("actions"), list):
        action_types = [str(a.get("type") or "").strip().lower() for a in payload.get("actions") if isinstance(a, dict)]
    return "|".join(
        [
            str(row.get("type") or "").strip().lower(),
            str(row.get("result_status") or row.get("lifecycle_state") or payload.get("status") or "").strip().lower(),
            ",".join(sorted([a for a in action_types if a])),
        ]
    )


def _row_quality_score(row: dict[str, Any]) -> float:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    message = str(row.get("message") or "").strip()
    response = str(payload.get("bot_response") or payload.get("result") or payload.get("description") or "").strip()
    score = 0.0
    if str(row.get("event_id") or "").strip():
        score += 1.0
    if message:
        score += 1.0 + min(len(message), 220) / 220.0
    if response:
        score += 1.0 + min(len(response), 220) / 220.0
    if isinstance(payload.get("actions"), list) and payload.get("actions"):
        score += 0.75
    if isinstance(payload.get("results"), list) and payload.get("results"):
        score += 0.75
    status = str(row.get("result_status") or row.get("lifecycle_state") or payload.get("status") or "").strip().lower()
    if status in {"completed", "success", "failed", "error", "blocked", "denied"}:
        score += 0.5
    return score


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_event: dict[str, dict[str, Any]] = {}
    no_event: list[dict[str, Any]] = []
    for r in rows:
        key = str(r.get("event_id") or "").strip()
        if not key:
            no_event.append(r)
            continue
        existing = by_event.get(key)
        if existing is None or _row_quality_score(r) > _row_quality_score(existing):
            by_event[key] = r

    out: list[dict[str, Any]] = list(by_event.values()) + no_event

    # Exact and near-exact dedupe by normalized content while preserving task/error diversity.
    by_content: dict[tuple[str, str], dict[str, Any]] = {}
    recent_text_by_sig: dict[str, list[str]] = {}
    filtered: list[dict[str, Any]] = []
    for r in out:
        sig = _semantic_signature(r)
        c_hash = _content_hash(r)
        compound = (c_hash, sig)
        existing = by_content.get(compound)
        if existing is not None:
            if _row_quality_score(r) > _row_quality_score(existing):
                by_content[compound] = r
            continue

        norm = _norm_text(r.get("message") or "")
        near_dup = False
        for prev in recent_text_by_sig.get(sig, [])[-25:]:
            if norm and prev and SequenceMatcher(None, norm, prev).ratio() >= 0.975:
                near_dup = True
                break
        if near_dup:
            continue

        by_content[compound] = r
        recent_text_by_sig.setdefault(sig, []).append(norm)
        filtered.append(r)

    return filtered


def _base_meta(r: dict[str, Any], *, row_type: str) -> dict[str, Any]:
    return {
        "event_id": str(r.get("event_id") or ""),
        "type": str(r.get("type") or row_type),
        "source": str(r.get("source") or "system"),
        "user_id": str(r.get("user_id") or "system"),
        "session_id": str(r.get("session_id") or ""),
        "timestamp": str(r.get("timestamp") or ""),
    }


def _delegation_patterns(payload: dict[str, Any], action_types: list[str]) -> list[str]:
    patterns: list[str] = []
    delegated_to = str(payload.get("delegated_to") or payload.get("assigned_to") or payload.get("delegate") or "").strip()
    if delegated_to:
        patterns.append(f"delegate_to:{delegated_to.lower()}")
    for a in action_types:
        t = a.strip().lower()
        if t in {"delegate_task", "handoff", "assign_task", "pc_agent", "browser_automation"}:
            patterns.append(f"action:{t}")
    return sorted(list(dict.fromkeys([p for p in patterns if p])))


def _to_instruction(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        msg = str(r.get("message") or payload.get("user_input") or payload.get("source_text") or payload.get("prompt") or "").strip()
        if not msg:
            continue
        completion = str(payload.get("bot_response") or payload.get("result") or payload.get("description") or "").strip()
        if not completion:
            completion = str(payload.get("completion") or payload.get("assistant_response") or "").strip()
        if not completion:
            completion = "Acknowledged."
        out.append({
            "event_id": r.get("event_id"),
            "input": msg,
            "expected_output": completion,
            "context": str(r.get("type") or "general"),
            "metadata": _base_meta(r, row_type="instruction"),
            "type": str(r.get("type") or "general"),
        })
    return out


def _to_conversation(chat_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in chat_rows:
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        u = str(payload.get("user_input") or payload.get("prompt") or r.get("message") or "").strip()
        a = str(payload.get("bot_response") or payload.get("completion") or payload.get("assistant_response") or "").strip()
        if not u:
            continue
        turns = [{"role": "user", "text": u}]
        if a:
            turns.append({"role": "assistant", "text": a})
        out.append(
            {
                "event_id": r.get("event_id"),
                "input": u,
                "expected_output": a or "Acknowledged.",
                "context": "conversation_turns",
                "metadata": _base_meta(r, row_type="conversation"),
                "turns": turns,
                "type": "conversation",
            }
        )
    return out


def _to_task(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        action_types = [str(a.get("type") or "").strip() for a in actions if isinstance(a, dict)]
        steps = payload.get("results") if isinstance(payload.get("results"), list) else []
        delegation = _delegation_patterns(payload, action_types)
        desc = str(
            payload.get("description")
            or payload.get("source_text")
            or payload.get("feature")
            or payload.get("reason")
            or r.get("message")
            or ""
        ).strip()
        if not desc:
            continue
        out.append({
            "event_id": r.get("event_id"),
            "input": desc,
            "expected_output": str(payload.get("result") or payload.get("status") or r.get("result_status") or "recorded"),
            "context": "; ".join([x for x in [str(r.get("type") or "task"), f"actions={len([t for t in action_types if t])}", f"steps={len(steps)}"] if x]),
            "metadata": {
                **_base_meta(r, row_type="task"),
                "multi_step": bool(steps),
                "delegation_patterns": delegation,
            },
            "task": desc,
            "result": str(payload.get("result_status") or payload.get("status") or r.get("result_status") or r.get("lifecycle_state") or "recorded"),
            "actions": [t for t in action_types if t],
            "delegation_patterns": delegation,
            "multi_step": bool(steps),
            "type": "task",
        })

        # Include step-level task traces when present to improve training signal density.
        step_rows = steps
        for idx, step in enumerate(step_rows):
            if not isinstance(step, dict):
                continue
            step_msg = str(step.get("message") or step.get("description") or step.get("step") or "").strip()
            if not step_msg:
                continue
            out.append(
                {
                    "event_id": f"{r.get('event_id')}:step:{idx}",
                    "input": step_msg,
                    "expected_output": str(step.get("status") or step.get("result") or "recorded"),
                    "context": f"task_step_{idx}",
                    "metadata": {
                        **_base_meta(r, row_type="task_step"),
                        "parent_event_id": str(r.get("event_id") or ""),
                        "step_index": idx,
                        "delegation_patterns": delegation,
                    },
                    "task": step_msg,
                    "result": str(step.get("status") or step.get("result") or "recorded"),
                    "actions": [t for t in action_types if t],
                    "delegation_patterns": delegation,
                    "type": "task_step",
                }
            )
    return out


def _to_error(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        msg = str(r.get("message") or payload.get("error") or payload.get("message") or "").strip()
        if not msg:
            continue
        cause = str(payload.get("cause") or payload.get("reason") or payload.get("error_type") or "").strip()
        context = str(payload.get("context") or payload.get("source_text") or payload.get("feature") or "").strip()
        fix = str(payload.get("fix_suggestion") or payload.get("resolution") or payload.get("recommendation") or "").strip()
        if not fix:
            fix = "Inspect correlated events and retry with safer parameters."
        details = " | ".join([p for p in [cause, context] if p])
        full_error = msg if not details else f"{msg} ({details})"
        out.append({
            "event_id": r.get("event_id"),
            "input": msg,
            "expected_output": fix,
            "context": context or str(r.get("type") or "error"),
            "metadata": _base_meta(r, row_type="error"),
            "error": full_error,
            "cause": cause or "unknown",
            "context_details": context or "unknown",
            "fix_suggestion": fix,
            "type": "error",
        })

        # Add one contextual variant when structured fields are available.
        if isinstance(payload, dict) and payload:
            context_bits = []
            for key in ("type", "status", "error_type", "feature", "source", "cause", "reason"):
                v = payload.get(key)
                if v in {None, ""}:
                    continue
                context_bits.append(f"{key}={str(v).strip()}")
            if context_bits:
                out.append(
                    {
                        "event_id": f"{r.get('event_id')}:ctx",
                        "input": msg,
                        "expected_output": "Check structured context fields and correlated task/session logs.",
                        "context": "; ".join(context_bits[:6]),
                        "metadata": _base_meta(r, row_type="error_context"),
                        "error": f"{msg} (context: {'; '.join(context_bits[:6])})",
                        "cause": cause or "contextual_runtime_failure",
                        "context_details": "; ".join(context_bits[:6]),
                        "fix_suggestion": "Check structured context fields and correlated task/session logs.",
                        "type": "error_context",
                    }
                )
    return out


def _to_rag(chat_rows: list[dict[str, Any]], task_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for r in chat_rows + task_rows:
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        msg = str(r.get("message") or payload.get("source_text") or payload.get("description") or payload.get("user_input") or "").strip()
        if not msg:
            continue
        rows.append(
            {
                "doc_id": str(r.get("event_id") or ""),
                "title": str(r.get("type") or "event"),
                "content": msg,
                "metadata": _base_meta(r, row_type="rag_document"),
                "tags": [str(r.get("source") or "system")],
            }
        )
    return rows


def _collection_weight(collection: str, row: dict[str, Any]) -> int:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    status = str(row.get("result_status") or row.get("lifecycle_state") or payload.get("status") or "").strip().lower()
    row_type = str(row.get("type") or "").strip().lower()
    weight = 1
    if collection == "error_logs":
        weight = 4
    elif collection == "learning_memory":
        weight = 4
        if str(payload.get("response_outcome") or "").strip().lower() in {"success", "failed"}:
            weight += 1
    elif collection == "task_logs":
        weight = 3
        if status in {"failed", "error", "blocked", "denied", "stopped"}:
            weight += 1
    elif collection == "training_events":
        weight = 2

    if row_type in {"failure_fix", "error", "error_context"}:
        weight += 1
    if status in {"failed", "error"}:
        weight += 1
    if "correct" in _norm_text(row.get("message") or payload.get("source_text") or ""):
        weight += 1
    return int(max(1, min(weight, 6)))


def _weighted_rows(collection: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weighted: list[dict[str, Any]] = []
    for r in rows:
        copies = _collection_weight(collection, r)
        weighted.extend([r] * copies)
    return weighted


def _learning_rows_as_training_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        pattern = str(
            row.get("input_pattern")
            or row.get("message")
            or payload.get("input_pattern")
            or payload.get("source_text")
            or payload.get("user_input")
            or ""
        ).strip()
        if not pattern:
            continue
        outcome = str(
            row.get("response_outcome")
            or payload.get("response_outcome")
            or row.get("result_status")
            or row.get("lifecycle_state")
            or ""
        ).strip().lower()
        status = "failed" if outcome == "failed" else ("completed" if outcome == "success" else "recorded")
        best = str(row.get("best_response") or payload.get("best_response") or payload.get("bot_response") or "").strip()
        fix = str(
            row.get("fix_pattern")
            or payload.get("fix_pattern")
            or row.get("improvement_hint")
            or payload.get("improvement_hint")
            or payload.get("fix_suggestion")
            or ""
        ).strip()
        content = best or fix or "Apply previous successful pattern with concise steps."
        converted = {
            "event_id": str(row.get("pattern_key") or row.get("event_id") or "").strip(),
            "type": str(row.get("pattern_type") or "learning_memory").strip().lower() or "learning_memory",
            "source": "learning",
            "user_id": "system",
            "session_id": "learning_memory",
            "timestamp": str(row.get("updated_at") or row.get("created_at") or ""),
            "lifecycle_state": status,
            "result_status": status,
            "message": pattern,
            "payload": {
                "source_text": pattern,
                "user_input": pattern,
                "bot_response": content,
                "result": content,
                "response_outcome": outcome or None,
                "quality_score": float(row.get("quality_score") or payload.get("quality_score") or 0.0),
                "priority_score": float(row.get("priority_score") or payload.get("priority_score") or 0.0),
                "frequency": int(row.get("frequency") or payload.get("frequency") or 0),
                "failure_case": str(row.get("failure_case") or payload.get("failure_case") or "").strip() or None,
                "fix_pattern": str(row.get("fix_pattern") or payload.get("fix_pattern") or "").strip() or None,
            },
        }
        out.append(converted)
    return out


def _is_high_value_learning_row(row: dict[str, Any]) -> bool:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    msg = str(row.get("message") or payload.get("source_text") or "").strip()
    if not msg:
        return False
    norm = _norm_text(msg)
    if norm in {"hello", "hi", "thanks", "ok", "yes", "no", "open", "run"}:
        return False
    quality = float(payload.get("quality_score") or 0.0)
    priority = float(payload.get("priority_score") or 0.0)
    freq = int(payload.get("frequency") or 0)
    outcome = str(payload.get("response_outcome") or "").strip().lower()
    row_type = str(row.get("type") or "").strip().lower()
    if row_type in {"failure_fix", "error"} and (freq >= 2 or outcome == "failed"):
        return True
    if outcome == "success" and quality >= 0.72 and freq >= 2:
        return True
    return bool(priority >= 0.68 or quality >= 0.72)


def build_datasets(
    database: Any,
    *,
    output_dir: str | Path,
    limit_per_collection: int = 10000,
    use_database_for_training: bool | None = None,
    allow_file_fallback_on_empty_db: bool = True,
    file_fallback_root: str | Path | None = None,
) -> dict[str, Any]:
    out_dir = Path(output_dir)

    resolved_use_db = True if use_database_for_training is None else bool(use_database_for_training)
    fallback_root = Path(file_fallback_root) if file_fallback_root is not None else None
    rows_by_collection: dict[str, list[dict[str, Any]]] = {name: [] for name in DB_SOURCE_COLLECTIONS}
    runtime_database, db_connected = _resolve_runtime_database(database)

    if resolved_use_db and runtime_database is not None:
        for name in DB_SOURCE_COLLECTIONS:
            if name == "learning_memory":
                rows_by_collection[name] = _dedupe_learning_memory(
                    _iter_learning_memory_raw(runtime_database, limit=limit_per_collection)
                )
            else:
                rows_by_collection[name] = _dedupe(_iter_normalized(runtime_database, name, limit=limit_per_collection))

    db_total = sum(len(rows_by_collection[name]) for name in DB_SOURCE_COLLECTIONS)
    source_mode = "database"

    if resolved_use_db and db_total == 0 and allow_file_fallback_on_empty_db:
        logger.warning("[training.dataset_builder] source_mode=fallback_files reason=db_empty_or_unavailable")
        for name in DB_SOURCE_COLLECTIONS:
            rows_by_collection[name] = _dedupe(
                _iter_file_normalized(
                    name,
                    limit=limit_per_collection,
                    fallback_root=fallback_root,
                )
            )
        source_mode = "fallback_files"
    elif not resolved_use_db:
        for name in DB_SOURCE_COLLECTIONS:
            rows_by_collection[name] = _dedupe(
                _iter_file_normalized(
                    name,
                    limit=limit_per_collection,
                    fallback_root=fallback_root,
                )
            )
        source_mode = "file_only"

    chat_rows = rows_by_collection["chat_logs"]
    task_rows = rows_by_collection["task_logs"]
    agent_rows = rows_by_collection["agent_logs"]
    error_rows = rows_by_collection["error_logs"]
    training_rows = rows_by_collection["training_events"]
    learning_rows = _learning_rows_as_training_rows(rows_by_collection["learning_memory"])

    weighted_chat_rows = _weighted_rows("chat_logs", chat_rows)
    weighted_task_rows = _weighted_rows("task_logs", task_rows)
    weighted_agent_rows = _weighted_rows("agent_logs", agent_rows)
    weighted_error_rows = _weighted_rows("error_logs", error_rows)
    weighted_training_rows = _weighted_rows("training_events", training_rows)
    high_value_learning_rows = [r for r in learning_rows if _is_high_value_learning_row(r)]
    weighted_learning_rows = list(learning_rows) + _weighted_rows("learning_memory", high_value_learning_rows)

    instruction = _to_instruction(weighted_chat_rows + weighted_training_rows + weighted_learning_rows + weighted_task_rows + weighted_agent_rows)
    conversation = _to_conversation(weighted_chat_rows + weighted_learning_rows)
    task_like_agent_rows = [
        r for r in weighted_agent_rows
        if str(r.get("message") or "").strip()
    ]
    task_dataset = _to_task(weighted_task_rows + task_like_agent_rows)
    error_like_agent_rows = [
        r for r in weighted_agent_rows
        if str(r.get("result_status") or r.get("lifecycle_state") or "").strip().lower() in {"failed", "error", "blocked", "denied"}
        or "error" in str(r.get("type") or "").strip().lower()
    ]
    error_like_task_rows = [
        r for r in weighted_task_rows
        if str(r.get("result_status") or r.get("lifecycle_state") or "").strip().lower() in {"failed", "error", "blocked", "denied", "stopped"}
        or re.search(r"\b(error|failed|exception|denied|timeout)\b", str(r.get("message") or ""), re.IGNORECASE)
    ]
    error_dataset = _to_error(weighted_error_rows + error_like_agent_rows + error_like_task_rows)
    rag_docs = _to_rag(weighted_chat_rows + weighted_learning_rows, weighted_task_rows + task_like_agent_rows)

    counts = {
        "instruction_dataset": _write_jsonl(out_dir / "instruction_dataset.jsonl", instruction),
        "conversation_dataset": _write_jsonl(out_dir / "conversation_dataset.jsonl", conversation),
        "task_dataset": _write_jsonl(out_dir / "task_dataset.jsonl", task_dataset),
        "error_dataset": _write_jsonl(out_dir / "error_dataset.jsonl", error_dataset),
        "rag_documents": _write_jsonl(out_dir / "rag_documents.jsonl", rag_docs),
    }

    metadata = {
        "schema_version": 1,
        "status": "success",
        "counts": counts,
        "db_connected": bool(db_connected),
        "collection_counts": {
            "chat_logs": len(chat_rows),
            "task_logs": len(task_rows),
            "agent_logs": len(agent_rows),
            "error_logs": len(error_rows),
            "training_events": len(training_rows),
            "learning_memory": len(learning_rows),
        },
        "weighted_collection_counts": {
            "chat_logs": len(weighted_chat_rows),
            "task_logs": len(weighted_task_rows),
            "agent_logs": len(weighted_agent_rows),
            "error_logs": len(weighted_error_rows),
            "training_events": len(weighted_training_rows),
            "learning_memory": len(weighted_learning_rows),
        },
        "high_value_learning_rows": int(len(high_value_learning_rows)),
        "dedupe": {
            "chat_logs": len(chat_rows),
            "task_logs": len(task_rows),
            "agent_logs": len(agent_rows),
            "error_logs": len(error_rows),
            "training_events": len(training_rows),
            "learning_memory": len(learning_rows),
        },
        "output_dir": str(out_dir),
        "source_mode": source_mode,
        "use_database_for_training": bool(resolved_use_db),
    }
    _write_jsonl(out_dir / "datasets_metadata.jsonl", [metadata])

    # Mirror metadata into Mongo for runtime visibility.
    try:
        if runtime_database is not None:
            md_event = normalize_for_collection(
                "datasets_metadata",
                {
                    "timestamp": None,
                    "user_id": "system",
                    "session_id": "system",
                    "correlation_id": "dataset_build",
                    "source": "system",
                    "mode": "cloud",
                    "lifecycle_state": "completed",
                    "type": "dataset_build",
                    "message": "Dataset build completed",
                    "metadata": metadata,
                },
            )
            runtime_database["datasets_metadata"].update_one(
                {"event_id": md_event.get("event_id")},
                {"$set": md_event},
                upsert=True,
            )
    except Exception:
        pass
    return metadata
