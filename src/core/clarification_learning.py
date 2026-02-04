from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


_DEFAULT_DIR = Path(__file__).parent.parent.parent / "data" / "sessions"
_DEFAULT_DIR.mkdir(parents=True, exist_ok=True)

# Clarification learning defaults (intentionally NOT controlled by env vars)
DEFAULT_LEARNING_ENABLED = True
DEFAULT_MIN_SIMILARITY = 0.45


def _safe_session_key(session_id: str) -> str:
    s = (session_id or "").strip()
    if not s:
        return "default"
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", s)
    return s[:80] or "default"


def _stopwords() -> set[str]:
    return {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "for",
        "in",
        "on",
        "with",
        "this",
        "that",
        "it",
        "is",
        "are",
        "was",
        "were",
        "be",
        "as",
        "at",
        "from",
        "by",
        "please",
        "kindly",
        "jarvis",
        "hey",
        "i",
        "me",
        "my",
        "you",
        "your",
        "we",
        "our",
        "do",
        "did",
        "done",
        "make",
        "perform",
        "research",
        "analysis",
        "summary",
        "report",
        "give",
        "need",
        "want",
        "better",
        "improve",
        "version",
        "proper",
        "results",
    }


def _tokens(text: str) -> set[str]:
    tl = (text or "").lower()
    toks = set(re.findall(r"[a-z0-9]+", tl))
    return {t for t in toks if t and t not in _stopwords()}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return float(inter) / float(union or 1)


@dataclass
class ClarificationExample:
    kind: str
    question: str
    original_user_text: str
    answer_text: str
    slots: Dict[str, Any]
    created_at: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "question": self.question,
            "original_user_text": self.original_user_text,
            "answer_text": self.answer_text,
            "slots": self.slots,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ClarificationExample":
        return ClarificationExample(
            kind=str(d.get("kind") or ""),
            question=str(d.get("question") or ""),
            original_user_text=str(d.get("original_user_text") or ""),
            answer_text=str(d.get("answer_text") or ""),
            slots=d.get("slots") if isinstance(d.get("slots"), dict) else {},
            created_at=float(d.get("created_at") or 0.0),
        )


class ClarificationLearner:
    """Learns from clarification Q→A to reduce future follow-up questions.

    Design goals:
    - Intent-agnostic: works even when new intents appear.
    - Safe: only auto-applies low-risk constraints (region/time/output format).
    - Lightweight: file-backed JSONL, no external deps.
    """

    def __init__(
        self,
        *,
        base_dir: Optional[Path] = None,
        enabled: bool = DEFAULT_LEARNING_ENABLED,
        min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ):
        self.base_dir = base_dir or _DEFAULT_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.enabled = bool(enabled)
        self.min_similarity = float(min_similarity)

    def _path(self, session_id: str) -> Path:
        return self.base_dir / f"clarification_learning_{_safe_session_key(session_id)}.jsonl"

    def record(self, session_id: str, ex: ClarificationExample) -> None:
        p = self._path(session_id)
        try:
            p.open("a", encoding="utf-8").write(json.dumps(ex.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            pass

    def load(self, session_id: str, *, kind: Optional[str] = None, max_items: int = 200) -> List[ClarificationExample]:
        p = self._path(session_id)
        if not p.exists():
            return []
        out: List[ClarificationExample] = []
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []

        # Read from end (most recent first)
        for line in reversed(lines[-max_items:]):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            ex = ClarificationExample.from_dict(d)
            if not ex.kind or not ex.original_user_text:
                continue
            if kind and (ex.kind or "").strip().lower() != kind.strip().lower():
                continue
            out.append(ex)
        return out

    @staticmethod
    def classify_kind(user_text: str) -> str:
        tl = (user_text or "").strip().lower()
        if not tl:
            return "generic"
        if re.search(r"\b(email|mail)\b", tl):
            return "email"
        if re.search(r"\b(research|with\s+sources|with\s+links|citations?|summarize|analysis|analyze)\b", tl):
            return "research"
        if re.search(r"\b(read|list|mkdir|write|edit|delete|move|copy)\b", tl):
            return "file_action"
        if re.search(r"\b(brightness|volume|wifi|bluetooth|power\s+plan|energy\s+saver)\b", tl):
            return "device_action"
        return "generic"

    @staticmethod
    def extract_slots(kind: str, question: str, answer_text: str) -> Dict[str, Any]:
        """Extract reusable constraints from a user's answer.

        This intentionally focuses on stable constraints/preferences.
        """
        kind = (kind or "").strip().lower() or "generic"
        answer = (answer_text or "").strip()
        tl = answer.lower()
        slots: Dict[str, Any] = {}

        # Generic key:value patterns (works for any intent)
        for m in re.finditer(r"\b([a-z][a-z0-9_\- ]{2,30})\s*[:=]\s*([^\n]{1,120})", answer, flags=re.IGNORECASE):
            k = re.sub(r"\s+", "_", m.group(1).strip().lower())
            v = m.group(2).strip()
            if k and v:
                slots.setdefault("kv", {})
                if isinstance(slots["kv"], dict):
                    slots["kv"][k] = v

        # Output format preference
        if re.search(r"\b(table|tabular)\b", tl):
            slots["output_format"] = "table"
        elif re.search(r"\b(bullets?|bullet\s+points?)\b", tl):
            slots["output_format"] = "bullets"
        elif re.search(r"\b(step\s*-?by\s*-?step|steps)\b", tl):
            slots["output_format"] = "steps"

        # Time range: capture explicit years / ranges
        years = re.findall(r"\b(20\d{2})\b", tl)
        if years:
            uniq = sorted({int(y) for y in years})
            if len(uniq) == 1:
                slots["year"] = uniq[0]
            else:
                slots["year_range"] = f"{uniq[0]}-{uniq[-1]}"

        yr_range = re.search(r"\b(20\d{2})\s*[-–]\s*(20\d{2})\b", tl)
        if yr_range:
            slots["year_range"] = f"{yr_range.group(1)}-{yr_range.group(2)}"

        # Relative time ranges
        rel = re.search(r"\b(last|past)\s+(\d{1,2})\s+(day|days|week|weeks|month|months|year|years)\b", tl)
        if rel:
            slots["time_range"] = f"{rel.group(1)} {rel.group(2)} {rel.group(3)}"

        # Region hints
        region_map = {
            "global": ["global", "worldwide", "world"],
            "india": ["india"],
            "us": ["us", "usa", "united states", "america"],
            "uk": ["uk", "united kingdom", "britain"],
            "europe": ["europe", "eu"],
            "middle_east": ["middle east"],
            "apac": ["apac", "asia pacific"],
        }
        for reg, keys in region_map.items():
            if any(k in tl for k in keys):
                slots["region"] = reg
                break

        # Research depth preference
        if kind == "research":
            if re.search(r"\b(deep|in-?depth|detailed|thorough|full)\b", tl):
                slots["depth"] = "deep"
            elif re.search(r"\b(quick|brief|short)\b", tl):
                slots["depth"] = "quick"

        return slots

    @staticmethod
    def _missing_slots(kind: str, user_text: str) -> List[str]:
        kind = (kind or "").strip().lower() or "generic"
        tl = (user_text or "").strip().lower()
        missing: List[str] = []

        if kind == "research":
            has_year = bool(re.search(r"\b20\d{2}\b", tl) or re.search(r"\b(last|past)\s+\d+\s+(day|week|month|year)s?\b", tl))
            has_region = bool(re.search(r"\b(global|worldwide|india|usa|us\b|united states|uk\b|europe|apac|asia pacific)\b", tl))
            if not has_region:
                missing.append("region")
            if not has_year:
                missing.append("time_range")
        # Output format is generally useful across intents, but optional.
        has_format = bool(re.search(r"\b(bullets?|table|step\s*-?by\s*-?step|steps)\b", tl))
        if not has_format:
            missing.append("output_format")

        return missing

    def suggest(self, session_id: str, *, kind: str, user_text: str) -> Tuple[Dict[str, Any], float]:
        """Return (slots, confidence) from best matching prior clarification."""
        kind = (kind or "").strip().lower() or "generic"
        examples = self.load(session_id, kind=kind)
        if not examples:
            return {}, 0.0

        target_tokens = _tokens(user_text)
        best: Optional[ClarificationExample] = None
        best_score = 0.0

        for ex in examples[:80]:
            s = _jaccard(target_tokens, _tokens(ex.original_user_text))
            if s > best_score:
                best = ex
                best_score = s

        if not best or best_score <= 0.0:
            return {}, 0.0

        # Conservative threshold
        if best_score < float(self.min_similarity):
            return {}, best_score

        slots = best.slots if isinstance(best.slots, dict) else {}
        return slots, best_score

    def augment_request(self, session_id: str, user_text: str) -> Tuple[str, Dict[str, Any]]:
        """Return (maybe_augmented_text, applied_slots).

        Applies only missing, low-risk constraints.
        """
        if not self.enabled:
            return user_text, {}

        kind = self.classify_kind(user_text)
        missing = self._missing_slots(kind, user_text)
        if not missing:
            return user_text, {}

        slots, conf = self.suggest(session_id, kind=kind, user_text=user_text)
        if not slots:
            return user_text, {}

        applied: Dict[str, Any] = {}
        for k in missing:
            if k in slots:
                applied[k] = slots[k]

        # Allow year_range to satisfy time_range.
        if "time_range" in missing and "time_range" not in applied and "year_range" in slots:
            applied["time_range"] = slots["year_range"]

        if not applied:
            return user_text, {}

        # Append as explicit defaults to avoid changing semantics.
        # This steers the LLM/tools without pretending the user said it.
        suffix_lines = ["Defaults (from your previous answers):"]
        for k, v in applied.items():
            suffix_lines.append(f"- {k}: {v}")
        augmented = (user_text.strip() + "\n\n" + "\n".join(suffix_lines)).strip()
        return augmented, applied


_learner: Optional[ClarificationLearner] = None


def get_clarification_learner() -> ClarificationLearner:
    global _learner
    if _learner is None:
        _learner = ClarificationLearner()
    return _learner
