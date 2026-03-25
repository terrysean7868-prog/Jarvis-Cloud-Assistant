from __future__ import annotations

import hashlib
import math
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


def _extract_fix_hint(payload: dict[str, Any] | None) -> str:
    p = payload if isinstance(payload, dict) else {}
    for key in ("fix_suggestion", "resolution", "recommendation", "result", "next_step"):
        val = str(p.get(key) or "").strip()
        if val:
            return val
    return "Apply deterministic fix checklist and retry with safer parameters."


def _is_generic_pattern(text: str) -> bool:
    t = _norm_text(text)
    if not t:
        return True
    if len(t) < 8:
        return True
    generic_terms = {
        "hello",
        "hi",
        "thanks",
        "ok",
        "yes",
        "no",
        "help",
        "please help",
        "try again",
        "do it",
        "open app",
        "open",
        "run",
    }
    return t in generic_terms


def _parse_iso(value: Any) -> datetime | None:
    s = str(value or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


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

    @staticmethod
    def _learning_confidence(row: dict[str, Any], *, query: str = "") -> float:
        pattern_type = str((row or {}).get("pattern_type") or "").strip().lower()
        response_outcome = str((row or {}).get("response_outcome") or "").strip().lower()
        quality = max(0.0, min(1.0, float((row or {}).get("quality_score") or 0.0)))
        priority = max(0.0, float((row or {}).get("priority_score") or 0.0))
        freq = max(0, int((row or {}).get("frequency") or 0))
        recency_days = 365.0
        updated_at = _parse_iso((row or {}).get("updated_at"))
        if updated_at is not None:
            recency_days = max(0.0, (datetime.now(UTC) - updated_at).total_seconds() / 86400.0)
        recency_score = max(0.0, min(1.0, math.exp(-recency_days / 10.0)))
        repeat_score = max(0.0, min(1.0, math.log10(freq + 1.0)))
        correction_usefulness = 1.0 if str((row or {}).get("best_response") or "").strip() else 0.0
        failure_impact = 0.0
        if pattern_type in {"failure_fix", "error"}:
            failure_impact = 1.0
        if response_outcome == "failed":
            failure_impact = max(failure_impact, 0.9)
        sim = _similarity(query, str((row or {}).get("input_pattern") or "")) if query else 0.0
        generic_penalty = 0.25 if _is_generic_pattern(str((row or {}).get("input_pattern") or "")) else 0.0
        confidence = (
            (quality * 0.2)
            + (min(1.0, priority / 2.0) * 0.2)
            + (failure_impact * 0.28)
            + (correction_usefulness * 0.1)
            + (repeat_score * 0.16)
            + (recency_score * 0.07)
            + (sim * 0.05)
            - generic_penalty
        )
        return max(0.0, min(1.0, float(confidence)))

    def _ensure_indexes(self) -> None:
        db._ensure_connected()
        if db.db is None:
            return
        try:
            db.db["learning_memory"].create_index([("pattern_key", 1)], unique=True)
            db.db["learning_memory"].create_index([("pattern_type", 1), ("priority_score", -1), ("frequency", -1)])
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
        quality_score = max(0.0, min(1.0, float(entry.get("quality_score") or 0.7)))
        freq = int(max(1, int(entry.get("frequency") or 1)))
        repeat_score = max(0.0, min(1.0, math.log10(freq + 1.0)))
        response_outcome = str(entry.get("response_outcome") or "").strip().lower() or None
        failure_impact = 1.0 if pattern_type in {"failure_fix", "error"} else 0.0
        if response_outcome == "failed":
            failure_impact = max(failure_impact, 0.9)
        correction_usefulness = 1.0 if str(entry.get("best_response") or entry.get("fix_pattern") or "").strip() else 0.0
        priority_score = round(
            (quality_score * 0.4)
            + (failure_impact * 0.3)
            + (correction_usefulness * 0.18)
            + (repeat_score * 0.12),
            4,
        )
        set_payload = {
            "pattern_type": pattern_type,
            "pattern_key": key,
            "input_pattern": input_pattern,
            "best_response": str(entry.get("best_response") or "").strip() or None,
            "failure_case": str(entry.get("failure_case") or "").strip() or None,
            "fix_pattern": str(entry.get("fix_pattern") or "").strip() or None,
            "improvement_hint": str(entry.get("improvement_hint") or "").strip() or None,
            "response_outcome": response_outcome,
            "quality_score": round(quality_score, 4),
            "priority_score": priority_score,
            "updated_at": now,
            "source": "self_learning_engine",
        }

        db.db["learning_memory"].update_one(
            {"pattern_key": key},
            {
                "$set": set_payload,
                "$setOnInsert": {"created_at": now},
                "$inc": {"frequency": freq},
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
        response_status: str | None = None,
        fallback_used: bool | None = None,
        task_result_status: str | None = None,
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
        status_s = str(response_status or "").strip().lower()
        if status_s in {"failed", "error"}:
            weak_reasons.append("response_failed")
        task_status = str(task_result_status or "").strip().lower()
        if task_status in {"failed", "error", "blocked", "denied", "stopped"}:
            weak_reasons.append("task_failed")
        fallback_flag = bool(fallback_used) or ("fallback" in str(source or "").strip().lower())
        if fallback_flag:
            weak_reasons.append("fallback_used")

        response_outcome = "success"
        if (
            "response_failed" in weak_reasons
            or "task_failed" in weak_reasons
            or re.search(r"\b(failed|error|exception|unable)\b", r_norm)
        ):
            response_outcome = "failed"
        elif weak_reasons or (repeats >= 3 and len(r_norm) < 24):
            response_outcome = "weak"

        quality_score = 1.0
        quality_score -= min(0.8, 0.2 * len(weak_reasons))
        if actions:
            quality_score += 0.1
        if response_outcome == "failed":
            quality_score -= 0.2
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
            "response_outcome": response_outcome,
            "action_count": len(actions or []),
            "repeat_count_hint": int(repeats),
            "fallback_used": bool(fallback_flag),
            "task_result_status": task_status or None,
        }
        db.db["response_quality_signals"].insert_one(payload)

        # Update learning memory with successful or weak outcomes.
        if response_outcome == "failed":
            self._upsert_learning_entry(
                {
                    "pattern_type": "failure_fix",
                    "input_pattern": query,
                    "failure_case": " | ".join(weak_reasons),
                    "fix_pattern": "Provide direct diagnosis, root cause, and one deterministic recovery step.",
                    "improvement_hint": "When failure repeats, include one concrete fallback path in the first response.",
                    "response_outcome": response_outcome,
                    "quality_score": quality_score,
                    "frequency": 1,
                }
            )
        elif weak_reasons:
            self._upsert_learning_entry(
                {
                    "pattern_type": "chat",
                    "input_pattern": query,
                    "failure_case": " | ".join(weak_reasons),
                    "improvement_hint": "Provide a clearer direct answer and include one concrete next step.",
                    "response_outcome": response_outcome,
                    "quality_score": quality_score,
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
                    "response_outcome": response_outcome,
                    "quality_score": quality_score,
                    "frequency": 1,
                }
            )

        return {
            "status": "success",
            "quality_score": round(float(quality_score), 4),
            "weak": bool(weak_reasons),
            "weak_reasons": weak_reasons,
            "response_outcome": response_outcome,
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

        q_norm = _norm_text(q)
        is_debug_or_task = bool(re.search(r"\b(debug|error|traceback|exception|task|failed|fix|retry|timeout)\b", q_norm))
        rows = list(db.db["learning_memory"].find({}, {"_id": 0}).sort("updated_at", -1).limit(500))
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            pat = str(row.get("input_pattern") or "")
            if not pat:
                continue
            confidence = self._learning_confidence(row, query=q)
            sim = _similarity(q, pat)
            if confidence < 0.48 and sim < 0.7:
                continue
            if _is_generic_pattern(pat) and confidence < 0.88:
                continue
            if is_debug_or_task and str(row.get("pattern_type") or "").strip().lower() == "failure_fix":
                confidence = min(1.0, confidence + 0.18)
            scored.append((confidence, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        hints: list[str] = []
        for conf, row in scored[: max(1, int(limit) * 2)]:
            best = str(row.get("best_response") or "").strip()
            fail = str(row.get("failure_case") or "").strip()
            fix_pattern = str(row.get("fix_pattern") or "").strip()
            improve = str(row.get("improvement_hint") or "").strip()
            if best and conf >= 0.72:
                hints.append(f"best_response_pattern: {best[:180]}")
            if fix_pattern and conf >= 0.68:
                hints.append(f"failure_fix_pattern: failure={fail[:90] or 'unknown'} | fix={fix_pattern[:140]}")
            elif fail and conf >= 0.7:
                hints.append(f"avoid_failure_pattern: {fail[:140]}")
            if improve and conf >= 0.66:
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
            .find(
                {
                    "pattern_type": "chat",
                    "best_response": {"$exists": True, "$ne": None},
                    "response_outcome": "success",
                    "quality_score": {"$gte": 0.75},
                },
                {"_id": 0},
            )
            .sort([("priority_score", -1), ("frequency", -1), ("updated_at", -1)])
            .limit(200)
        )
        best_score = 0.0
        best_text: str | None = None
        for row in rows:
            pat = str(row.get("input_pattern") or "")
            ans = str(row.get("best_response") or "").strip()
            if not pat or not ans:
                continue
            if _is_generic_pattern(pat):
                continue
            sim = _similarity(q, pat)
            freq = int(row.get("frequency") or 0)
            conf = self._learning_confidence(row, query=q)
            if sim >= 0.95 and freq >= 3 and conf >= 0.82:
                score = (sim * 0.72) + (conf * 0.28)
                if score > best_score:
                    best_score = score
                    best_text = ans
        return best_text

    def build_learning_quality_report(self, *, lookback_hours: int = 96) -> dict[str, Any]:
        self._ensure_indexes()
        if db.db is None:
            return {"status": "skipped", "reason": "db_unavailable"}

        since = (datetime.now(UTC) - timedelta(hours=max(1, int(lookback_hours)))).isoformat()
        lm_rows = list(db.db["learning_memory"].find({}, {"_id": 0}).limit(3000))
        quality_rows = list(
            db.db["response_quality_signals"].find(
                {"recorded_at": {"$gte": since}},
                {"_id": 0, "query": 1, "response_outcome": 1, "quality_score": 1, "weak_reasons": 1, "recorded_at": 1},
            ).limit(4000)
        )

        def _top_rows(rows: list[dict[str, Any]], *, n: int = 5) -> list[dict[str, Any]]:
            ranked = sorted(rows, key=lambda r: self._learning_confidence(r), reverse=True)
            out: list[dict[str, Any]] = []
            for r in ranked[: n * 3]:
                out.append(
                    {
                        "pattern_type": str(r.get("pattern_type") or "unknown"),
                        "input_pattern": str(r.get("input_pattern") or "")[:120],
                        "failure_case": str(r.get("failure_case") or "")[:120],
                        "fix_pattern": str(r.get("fix_pattern") or "")[:120],
                        "best_response": str(r.get("best_response") or "")[:120],
                        "frequency": int(r.get("frequency") or 0),
                        "quality_score": float(r.get("quality_score") or 0.0),
                        "priority_score": float(r.get("priority_score") or 0.0),
                        "confidence": round(self._learning_confidence(r), 4),
                    }
                )
                if len(out) >= n:
                    break
            return out

        top_failure_fix = _top_rows([r for r in lm_rows if str(r.get("pattern_type") or "") == "failure_fix"], n=5)
        top_corrected = _top_rows(
            [r for r in lm_rows if str(r.get("pattern_type") or "") == "chat" and str(r.get("response_outcome") or "") == "success"],
            n=5,
        )

        weak_counter: Counter[str] = Counter()
        for r in quality_rows:
            if str(r.get("response_outcome") or "") != "weak":
                continue
            q = _norm_text(str(r.get("query") or ""))
            if q:
                weak_counter[q] += 1
        top_repeated_weak = [{"query": q, "count": int(c)} for q, c in weak_counter.most_common(5)]

        low_conf_rows = []
        for r in lm_rows:
            conf = self._learning_confidence(r)
            if conf < 0.48:
                low_conf_rows.append(
                    {
                        "pattern_type": str(r.get("pattern_type") or "unknown"),
                        "input_pattern": str(r.get("input_pattern") or "")[:120],
                        "confidence": round(conf, 4),
                        "frequency": int(r.get("frequency") or 0),
                    }
                )
        low_conf_rows = sorted(low_conf_rows, key=lambda x: float(x.get("confidence") or 0.0))[:5]

        return {
            "status": "success",
            "top_failure_fix_patterns": top_failure_fix,
            "top_repeated_weak_patterns": top_repeated_weak,
            "top_corrected_response_patterns": top_corrected,
            "low_confidence_patterns_to_ignore": low_conf_rows,
        }

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
        quality_rows = list(
            db.db["response_quality_signals"].find(
                {"recorded_at": {"$gte": since}},
                {"_id": 0, "query": 1, "response_text": 1, "response_outcome": 1, "quality_score": 1, "recorded_at": 1},
            ).sort("recorded_at", 1).limit(2500)
        )

        # Backfill older unknown outcomes to reduce noisy labels.
        try:
            unknown_rows = list(
                db.db["response_quality_signals"].find(
                    {
                        "recorded_at": {"$gte": since},
                        "$or": [
                            {"response_outcome": {"$exists": False}},
                            {"response_outcome": "unknown"},
                            {"response_outcome": None},
                        ],
                    },
                    {"_id": 1, "query": 1, "response_text": 1, "quality_score": 1, "weak": 1, "weak_reasons": 1, "repeat_count_hint": 1, "fallback_used": 1},
                ).limit(2500)
            )
            for row in unknown_rows:
                qn = _norm_text(str((row or {}).get("query") or ""))
                rn = _norm_text(str((row or {}).get("response_text") or ""))
                qr = float((row or {}).get("quality_score") or 0.0)
                weak = bool((row or {}).get("weak"))
                repeats = int((row or {}).get("repeat_count_hint") or 0)
                fallback = bool((row or {}).get("fallback_used"))
                outcome = "success"
                if re.search(r"\b(fail|error|exception|unable|timeout)\b", rn):
                    outcome = "failed"
                elif weak or repeats >= 2 or fallback or qr < 0.55:
                    outcome = "weak"
                if _is_generic_pattern(qn) and outcome == "success":
                    outcome = "weak"
                db.db["response_quality_signals"].update_one({"_id": row.get("_id")}, {"$set": {"response_outcome": outcome}})
        except Exception:
            pass

        query_counts: Counter[str] = Counter()
        for c in chats:
            msg = str((c or {}).get("message") or "").strip()
            n = _norm_text(msg)
            if n:
                query_counts[n] += 1

        repeated_queries = [(q, c) for q, c in query_counts.items() if c >= 2]
        repeated_queries.sort(key=lambda x: x[1], reverse=True)

        fail_msgs: list[str] = []
        fail_fixes: list[tuple[str, str]] = []
        success_msgs: list[str] = []
        for t in tasks:
            st = str((t or {}).get("result_status") or "").strip().lower()
            msg = str((t or {}).get("message") or "").strip()
            payload = (t or {}).get("payload") if isinstance((t or {}).get("payload"), dict) else {}
            if not msg:
                continue
            if st in {"failed", "error", "blocked", "denied", "stopped"}:
                fail_msgs.append(msg)
                fail_fixes.append((msg, _extract_fix_hint(payload)))
            elif st in {"success", "completed"}:
                success_msgs.append(msg)

        for e in errors:
            msg = str((e or {}).get("message") or "").strip()
            payload = (e or {}).get("payload") if isinstance((e or {}).get("payload"), dict) else {}
            if msg:
                fail_msgs.append(msg)
                fail_fixes.append((msg, _extract_fix_hint(payload)))

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
                    "response_outcome": "failed",
                    "quality_score": 0.2,
                    "frequency": int(freq),
                }
            )

        fail_fix_counter = Counter([( _norm_text(f), _norm_text(x)) for f, x in fail_fixes if _norm_text(f) and _norm_text(x)])
        for (failure_case, fix_pattern), freq in fail_fix_counter.most_common(25):
            self._upsert_learning_entry(
                {
                    "pattern_type": "failure_fix",
                    "input_pattern": failure_case,
                    "failure_case": failure_case,
                    "fix_pattern": fix_pattern,
                    "improvement_hint": "Prefer this validated fix pattern when this failure is detected.",
                    "response_outcome": "failed",
                    "quality_score": 0.35,
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
                    "response_outcome": "success",
                    "quality_score": 0.9,
                    "frequency": int(freq),
                }
            )

        # Corrected-response pattern mining: failed/weak first, then success for similar query.
        corrected_patterns = 0
        latest_non_success_by_query: dict[str, dict[str, Any]] = {}
        for row in quality_rows:
            query = _norm_text(str((row or {}).get("query") or ""))
            if not query:
                continue
            outcome = str((row or {}).get("response_outcome") or "").strip().lower()
            if outcome in {"failed", "weak"}:
                latest_non_success_by_query[query] = row
                continue
            if outcome == "success" and query in latest_non_success_by_query:
                response_text = str((row or {}).get("response_text") or "").strip()
                if not response_text:
                    continue
                corrected_patterns += 1
                self._upsert_learning_entry(
                    {
                        "pattern_type": "chat",
                        "input_pattern": query,
                        "best_response": response_text,
                        "improvement_hint": "Corrected response pattern: prioritize this answer style for repeated/corrected query.",
                        "response_outcome": "success",
                        "quality_score": max(0.75, float((row or {}).get("quality_score") or 0.75)),
                        "frequency": 2,
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
            "corrected_response_patterns": int(corrected_patterns),
            "improvement_suggestions_count": int(
                db.db["self_improvement_suggestions"].count_documents({"status": "open"}) if db.db is not None else 0
            ),
            "learning_quality_report": self.build_learning_quality_report(lookback_hours=lookback_hours),
            "status": "completed",
        }

        try:
            db.db["learning_cycle_reports"].insert_one(cycle_summary)
        except Exception:
            pass

        return cycle_summary
