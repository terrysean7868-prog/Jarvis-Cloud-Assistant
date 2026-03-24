from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from src.utils.db import db


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _norm_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    raw = re.sub(r"[^a-z0-9\s]", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip()


def _token_set(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", _norm_text(text))}


def _similarity(a: str, b: str) -> float:
    sa = _token_set(a)
    sb = _token_set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return float(inter / union) if union else 0.0


def _pattern_key(kind: str, text: str) -> str:
    src = f"{kind}|{_norm_text(text)}"
    return hashlib.sha1(src.encode("utf-8", errors="ignore")).hexdigest()[:24]


class SelfLearningEngine:
    """Controlled learning loop using existing logs and datasets.

    Safety model:
    - Never executes code or self-updates.
    - Writes only to observability collections.
    - Uses bounded windows + cooldown to avoid runtime regressions.
    """

    def __init__(self, cooldown_seconds: int = 45):
        self.cooldown_seconds = max(10, int(cooldown_seconds))
        self._last_cycle_at: datetime | None = None
        self._lock = threading.Lock()

    def _ensure_indexes(self) -> None:
        db._ensure_connected()
        if db.db is None:
            return
        try:
            db.db["learning_memory"].create_index([("pattern_key", 1)], unique=True)
            db.db["learning_memory"].create_index([("pattern_type", 1), ("frequency", -1)])
            db.db["learning_memory"].create_index([("updated_at", -1)])
        except Exception:
            pass
        try:
            db.db["self_improvement_suggestions"].create_index([("created_at", -1)])
            db.db["self_improvement_suggestions"].create_index([("status", 1), ("priority", -1)])
        except Exception:
            pass
        try:
            db.db["model_performance_stats"].create_index([("recorded_at", -1)])
            db.db["model_performance_stats"].create_index([("task_type", 1), ("model_id", 1), ("recorded_at", -1)])
        except Exception:
            pass
        try:
            db.db["response_quality_signals"].create_index([("recorded_at", -1)])
            db.db["response_quality_signals"].create_index([("user_id", 1), ("recorded_at", -1)])
        except Exception:
            pass

    def _upsert_learning_entry(self, entry: dict[str, Any]) -> None:
        self._ensure_indexes()
        if db.db is None:
            return
        pattern_type = str(entry.get("pattern_type") or "chat").strip().lower()
        input_pattern = str(entry.get("input_pattern") or "").strip()
        if not input_pattern:
            return

        key = _pattern_key(pattern_type, input_pattern)
        now = _now_iso()
        set_payload = {
            "pattern_type": pattern_type,
            "pattern_key": key,
            "input_pattern": input_pattern,
            "best_response": str(entry.get("best_response") or "").strip() or None,
            "failure_case": str(entry.get("failure_case") or "").strip() or None,
            "improvement_hint": str(entry.get("improvement_hint") or "").strip() or None,
            "updated_at": now,
            "source": "self_learning_engine",
        }

        db.db["learning_memory"].update_one(
            {"pattern_key": key},
            {
                "$set": set_payload,
                "$setOnInsert": {"created_at": now},
                "$inc": {"frequency": int(max(1, int(entry.get("frequency") or 1)))},
            },
            upsert=True,
        )

    def log_response_quality(
        self,
        *,
        user_id: str,
        query: str,
        response_text: str,
        actions: list[dict[str, Any]] | None = None,
        source: str = "chat",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Record response quality signal (read-only inference + observational write)."""
        self._ensure_indexes()
        if db.db is None:
            return {"status": "skipped", "reason": "db_unavailable"}

        q_norm = _norm_text(query)
        weak_reasons: list[str] = []

        # Repeated query detection in recent chats.
        try:
            repeats = int(
                db.db["chat_logs"].count_documents(
                    {"message": {"$regex": re.escape(q_norm), "$options": "i"}},
                    limit=8,
                )
            )
            if repeats >= 2:
                weak_reasons.append("repeated_user_query")
        except Exception:
            repeats = 0

        # Correction phrase detection in incoming query.
        if re.search(r"\b(no|wrong|not\s+that|incorrect|fix\s+that|try\s+again)\b", q_norm):
            weak_reasons.append("user_corrected_assistant")

        # Weak response heuristics.
        r_norm = _norm_text(response_text)
        if not r_norm or len(r_norm) < 10:
            weak_reasons.append("empty_or_short_response")
        if re.search(r"\b(not\s+sure|cannot|couldn\s*t|unavailable|try\s+again)\b", r_norm):
            weak_reasons.append("uncertain_response")

        quality_score = 1.0
        quality_score -= min(0.75, 0.2 * len(weak_reasons))
        if actions:
            quality_score += 0.1
        quality_score = max(0.0, min(1.0, quality_score))

        payload = {
            "recorded_at": _now_iso(),
            "user_id": str(user_id or "user").strip().lower() or "user",
            "query": str(query or "").strip(),
            "response_text": str(response_text or "").strip(),
            "response_source": str(source or "chat"),
            "request_id": str(request_id or "").strip() or None,
            "weak": bool(weak_reasons),
            "weak_reasons": weak_reasons,
            "quality_score": round(float(quality_score), 4),
            "action_count": len(actions or []),
            "repeat_count_hint": int(repeats),
        }
        db.db["response_quality_signals"].insert_one(payload)

        # Update learning memory with successful or weak outcomes.
        if weak_reasons:
            self._upsert_learning_entry(
                {
                    "pattern_type": "chat",
                    "input_pattern": query,
                    "failure_case": " | ".join(weak_reasons),
                    "improvement_hint": "Provide a clearer direct answer and include one concrete next step.",
                    "frequency": 1,
                }
            )
        else:
            self._upsert_learning_entry(
                {
                    "pattern_type": "chat",
                    "input_pattern": query,
                    "best_response": response_text,
                    "improvement_hint": "Reuse concise structure for similar future prompts.",
                    "frequency": 1,
                }
            )

        return {
            "status": "success",
            "quality_score": round(float(quality_score), 4),
            "weak": bool(weak_reasons),
            "weak_reasons": weak_reasons,
        }

    def record_model_performance(
        self,
        *,
        task_type: str,
        model_id: str,
        provider: str,
        success: bool,
        latency_ms: float,
        fallback_used: bool,
        error_kind: str | None = None,
    ) -> None:
        self._ensure_indexes()
        if db.db is None:
            return
        db.db["model_performance_stats"].insert_one(
            {
                "recorded_at": _now_iso(),
                "task_type": str(task_type or "simple_chat"),
                "model_id": str(model_id or "unknown"),
                "provider": str(provider or "unknown"),
                "success": bool(success),
                "latency_ms": float(max(0.0, float(latency_ms or 0.0))),
                "fallback_used": bool(fallback_used),
                "error_kind": str(error_kind or "").strip() or None,
            }
        )

    def get_learning_hints(self, text: str, *, limit: int = 3) -> list[str]:
        self._ensure_indexes()
        if db.db is None:
            return []
        q = str(text or "").strip()
        if not q:
            return []

        rows = list(db.db["learning_memory"].find({}, {"_id": 0}).sort("updated_at", -1).limit(250))
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            pat = str(row.get("input_pattern") or "")
            if not pat:
                continue
            sim = _similarity(q, pat)
            freq_bonus = min(0.3, float(int(row.get("frequency") or 0)) / 30.0)
            score = sim + freq_bonus
            if score < 0.22:
                continue
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        hints: list[str] = []
        for _, row in scored[: max(1, int(limit))]:
            best = str(row.get("best_response") or "").strip()
            fail = str(row.get("failure_case") or "").strip()
            improve = str(row.get("improvement_hint") or "").strip()
            if best:
                hints.append(f"best_response_pattern: {best[:180]}")
            if fail:
                hints.append(f"avoid_failure_pattern: {fail[:140]}")
            if improve:
                hints.append(f"improvement_hint: {improve[:140]}")
            if len(hints) >= max(1, int(limit)):
                break
        return hints[: max(1, int(limit))]

    def get_cached_best_response(self, text: str) -> str | None:
        self._ensure_indexes()
        if db.db is None:
            return None
        q = str(text or "").strip()
        if not q:
            return None
        rows = list(
            db.db["learning_memory"]
            .find({"pattern_type": "chat", "best_response": {"$exists": True, "$ne": None}}, {"_id": 0})
            .sort("frequency", -1)
            .limit(120)
        )
        best_score = 0.0
        best_text: str | None = None
        for row in rows:
            pat = str(row.get("input_pattern") or "")
            ans = str(row.get("best_response") or "").strip()
            if not pat or not ans:
                continue
            sim = _similarity(q, pat)
            freq = int(row.get("frequency") or 0)
            if sim >= 0.93 and freq >= 2:
                if sim > best_score:
                    best_score = sim
                    best_text = ans
        return best_text

    def _suggest(self, *, issue: str, root_cause: str, suggested_fix: str, affected_module: str, priority: int = 5) -> None:
        self._ensure_indexes()
        if db.db is None:
            return
        entry = {
            "suggestion_id": hashlib.sha1(f"{issue}|{root_cause}|{affected_module}".encode("utf-8", errors="ignore")).hexdigest()[:24],
            "issue": issue,
            "root_cause": root_cause,
            "suggested_fix": suggested_fix,
            "affected_module": affected_module,
            "priority": int(max(1, min(10, priority))),
            "status": "open",
            "created_at": _now_iso(),
            "source": "self_learning_engine",
        }
        db.db["self_improvement_suggestions"].update_one(
            {"suggestion_id": entry["suggestion_id"], "status": "open"},
            {"$set": entry},
            upsert=True,
        )

    def run_controlled_learning_cycle(self, *, lookback_hours: int = 48) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(UTC)
            if self._last_cycle_at and (now - self._last_cycle_at) < timedelta(seconds=self.cooldown_seconds):
                return {"status": "skipped", "reason": "cooldown"}
            self._last_cycle_at = now

        self._ensure_indexes()
        if db.db is None:
            return {"status": "skipped", "reason": "db_unavailable"}

        since = (datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()

        chats = list(db.db["chat_logs"].find({"timestamp": {"$gte": since}}, {"_id": 0, "message": 1, "payload": 1, "timestamp": 1}).limit(1200))
        tasks = list(db.db["task_logs"].find({"timestamp": {"$gte": since}}, {"_id": 0, "message": 1, "result_status": 1, "payload": 1, "timestamp": 1}).limit(1200))
        errors = list(db.db["error_logs"].find({"timestamp": {"$gte": since}}, {"_id": 0, "message": 1, "payload": 1, "timestamp": 1}).limit(1200))

        query_counts: Counter[str] = Counter()
        for c in chats:
            msg = str((c or {}).get("message") or "").strip()
            n = _norm_text(msg)
            if n:
                query_counts[n] += 1

        repeated_queries = [(q, c) for q, c in query_counts.items() if c >= 2]
        repeated_queries.sort(key=lambda x: x[1], reverse=True)

        fail_msgs: list[str] = []
        success_msgs: list[str] = []
        for t in tasks:
            st = str((t or {}).get("result_status") or "").strip().lower()
            msg = str((t or {}).get("message") or "").strip()
            if not msg:
                continue
            if st in {"failed", "error", "blocked", "denied", "stopped"}:
                fail_msgs.append(msg)
            elif st in {"success", "completed"}:
                success_msgs.append(msg)

        for e in errors:
            msg = str((e or {}).get("message") or "").strip()
            if msg:
                fail_msgs.append(msg)

        fail_counter = Counter([_norm_text(m) for m in fail_msgs if _norm_text(m)])
        success_counter = Counter([_norm_text(m) for m in success_msgs if _norm_text(m)])

        # Persist learnings (controlled, suggestion-based).
        for q, freq in repeated_queries[:20]:
            self._upsert_learning_entry(
                {
                    "pattern_type": "chat",
                    "input_pattern": q,
                    "improvement_hint": "User repeats this query; prioritize directness and include concrete steps.",
                    "frequency": int(freq),
                }
            )

        for key, freq in fail_counter.most_common(20):
            self._upsert_learning_entry(
                {
                    "pattern_type": "error",
                    "input_pattern": key,
                    "failure_case": key,
                    "improvement_hint": "Map this error pattern to a deterministic fix checklist.",
                    "frequency": int(freq),
                }
            )

        for key, freq in success_counter.most_common(20):
            self._upsert_learning_entry(
                {
                    "pattern_type": "task",
                    "input_pattern": key,
                    "best_response": "successful execution pattern",
                    "improvement_hint": "Reuse this successful task decomposition when similar intents are detected.",
                    "frequency": int(freq),
                }
            )

        # Generate structured self-improvement suggestions (no auto code changes).
        top_fail = fail_counter.most_common(1)
        if top_fail:
            issue = f"Frequent failure pattern: {top_fail[0][0][:120]}"
            self._suggest(
                issue=issue,
                root_cause="Recurring runtime/task failures detected in recent logs.",
                suggested_fix="Add targeted precondition checks and retry guidance for this pattern.",
                affected_module="src/core/llm_adapter.py",
                priority=8,
            )

        if repeated_queries:
            self._suggest(
                issue="Repeated user queries detected",
                root_cause="Responses for high-frequency prompts may be too generic.",
                suggested_fix="Use learning_memory best_response hints before generating final answer.",
                affected_module="src/core/llm_adapter.py",
                priority=7,
            )

        failed = len(fail_msgs)
        succeeded = len(success_msgs)
        total_eval = failed + succeeded
        success_score = float(succeeded / total_eval) if total_eval else 0.0

        cycle_summary = {
            "created_at": _now_iso(),
            "lookback_hours": int(max(1, int(lookback_hours))),
            "success_score": round(success_score, 4),
            "failure_patterns": [k for k, _ in fail_counter.most_common(8)],
            "repeated_queries": [{"query": q, "frequency": int(c)} for q, c in repeated_queries[:8]],
            "improvement_suggestions_count": int(
                db.db["self_improvement_suggestions"].count_documents({"status": "open"}) if db.db is not None else 0
            ),
            "status": "completed",
        }

        try:
            db.db["learning_cycle_reports"].insert_one(cycle_summary)
        except Exception:
            pass

        return cycle_summary
