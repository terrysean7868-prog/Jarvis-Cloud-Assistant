from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


_DATA_DIR = Path(__file__).parent.parent.parent / "data" / "sessions"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Pending clarification expiry (seconds). Prevents stale clarifications hijacking later chats.
_PENDING_TTL_SECONDS = 1800


def _safe_session_key(session_id: str) -> str:
    s = (session_id or "").strip()
    if not s:
        return "default"
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", s)
    return s[:80] or "default"


def _state_path(session_id: str) -> Path:
    return _DATA_DIR / f"dialogue_state_{_safe_session_key(session_id)}.json"


def _clarification_log_path() -> Path:
    return _DATA_DIR / "clarification_examples.jsonl"


@dataclass
class PendingClarification:
    kind: str
    question: str
    original_user_text: str
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "question": self.question,
            "original_user_text": self.original_user_text,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "PendingClarification":
        return PendingClarification(
            kind=str(d.get("kind") or ""),
            question=str(d.get("question") or ""),
            original_user_text=str(d.get("original_user_text") or ""),
            created_at=float(d.get("created_at") or 0.0),
        )


class DialogueStateStore:
    """Very small per-session state store (file-backed).

    Purpose: enable human-like clarifications:
    - Jarvis asks a clarifying question
    - The next user message is treated as the answer
    - Jarvis resumes the original request

    This is intentionally lightweight and best-effort.
    """

    def load_pending(self, session_id: str) -> Optional[PendingClarification]:
        p = _state_path(session_id)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            pending = raw.get("pending")
            if not isinstance(pending, dict):
                return None
            out = PendingClarification.from_dict(pending)
            if not out.kind or not out.original_user_text:
                return None

            # Expire stale pending clarifications.
            try:
                if _PENDING_TTL_SECONDS > 0 and out.created_at:
                    if (time.time() - float(out.created_at)) > float(_PENDING_TTL_SECONDS):
                        try:
                            p.unlink()
                        except Exception:
                            pass
                        return None
            except Exception:
                pass

            return out
        except Exception:
            return None

    def save_pending(self, session_id: str, pending: PendingClarification) -> None:
        p = _state_path(session_id)
        payload = {"pending": pending.to_dict(), "updated_at": time.time()}
        try:
            p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Best-effort; never block replies.
            pass

        # Best-effort log to support later intent/slot learning.
        try:
            rec = {
                "ts": time.time(),
                "session": _safe_session_key(session_id),
                "kind": pending.kind,
                "question": pending.question,
                "original": pending.original_user_text,
            }
            _clarification_log_path().open("a", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def clear_pending(self, session_id: str) -> None:
        p = _state_path(session_id)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

    @staticmethod
    def is_cancel_message(user_text: str) -> bool:
        tl = (user_text or "").strip().lower()
        if not tl:
            return False
        return bool(re.search(r"\b(cancel|never mind|nevermind|stop)\b", tl))

    @staticmethod
    def looks_like_direct_answer(user_text: str, *, pending_kind: Optional[str] = None) -> bool:
        """Heuristic: decide if a message is likely answering a clarification.

        We bias toward treating the next message as the answer (human UX),
        unless it clearly looks like a brand-new command.
        """
        t = (user_text or "").strip()
        if not t:
            return False

        tl = t.lower().strip()
        kind = (pending_kind or "").strip().lower() or "generic"

        # Very long messages are more likely to be new requests.
        if len(t) > 800:
            return False

        # If the user starts with a clear imperative command, assume they moved on.
        # NOTE: Allow "research ..." as an answer when the clarification itself is about research.
        imperative_starts = (
            "open ",
            "launch ",
            "start ",
            "switch ",
            "close ",
            "quit ",
            "run ",
            "execute ",
            "create ",
            "delete ",
            "edit ",
            "write ",
            "type ",
            "search ",
            "look up ",
            "find ",
            "go to ",
            "visit ",
        )

        if any(tl.startswith(s) for s in imperative_starts):
            return False

        if tl.startswith("research ") or tl.startswith("do research") or tl.startswith("make research"):
            return kind in {"research", "generic"}

        # Most short replies are intended as answers (yes/no/values/topics).
        return True


# Global singleton (simple)
_dialogue_state_store: Optional[DialogueStateStore] = None


def get_dialogue_state_store() -> DialogueStateStore:
    global _dialogue_state_store
    if _dialogue_state_store is None:
        _dialogue_state_store = DialogueStateStore()
    return _dialogue_state_store
