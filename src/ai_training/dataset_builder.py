from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .data_schemas import normalize_for_collection


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
        msg = str(r.get("message") or "").strip()
        if not msg:
            continue
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        completion = str(payload.get("bot_response") or payload.get("result") or payload.get("description") or "").strip()
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
        u = str(payload.get("user_input") or r.get("message") or "").strip()
        a = str(payload.get("bot_response") or "").strip()
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
        msg = str(r.get("message") or "").strip()
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


def build_datasets(database: Any, *, output_dir: str | Path, limit_per_collection: int = 10000) -> dict[str, Any]:
    out_dir = Path(output_dir)

    chat_rows = _dedupe(_iter_normalized(database, "chat_logs", limit=limit_per_collection))
    task_rows = _dedupe(_iter_normalized(database, "task_logs", limit=limit_per_collection))
    agent_rows = _dedupe(_iter_normalized(database, "agent_logs", limit=limit_per_collection))
    error_rows = _dedupe(_iter_normalized(database, "error_logs", limit=limit_per_collection))
    training_rows = _dedupe(_iter_normalized(database, "training_events", limit=limit_per_collection))

    instruction = _to_instruction(chat_rows + training_rows + task_rows + agent_rows)
    conversation = _to_conversation(chat_rows)
    task_like_agent_rows = [
        r for r in agent_rows
        if str(r.get("message") or "").strip()
    ]
    task_dataset = _to_task(task_rows + task_like_agent_rows)
    error_like_agent_rows = [
        r for r in agent_rows
        if str(r.get("result_status") or r.get("lifecycle_state") or "").strip().lower() in {"failed", "error", "blocked", "denied"}
        or "error" in str(r.get("type") or "").strip().lower()
    ]
    error_like_task_rows = [
        r for r in task_rows
        if str(r.get("result_status") or r.get("lifecycle_state") or "").strip().lower() in {"failed", "error", "blocked", "denied", "stopped"}
        or re.search(r"\b(error|failed|exception|denied|timeout)\b", str(r.get("message") or ""), re.IGNORECASE)
    ]
    error_dataset = _to_error(error_rows + error_like_agent_rows + error_like_task_rows)
    rag_docs = _to_rag(chat_rows, task_rows + task_like_agent_rows)

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
        "dedupe": {
            "chat_logs": len(chat_rows),
            "task_logs": len(task_rows),
            "agent_logs": len(agent_rows),
            "error_logs": len(error_rows),
            "training_events": len(training_rows),
        },
        "output_dir": str(out_dir),
    }
    _write_jsonl(out_dir / "datasets_metadata.jsonl", [metadata])

    # Mirror metadata into Mongo for runtime visibility.
    try:
        if database is not None:
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
            database["datasets_metadata"].update_one(
                {"event_id": md_event.get("event_id")},
                {"$set": md_event},
                upsert=True,
            )
    except Exception:
        pass
    return metadata
