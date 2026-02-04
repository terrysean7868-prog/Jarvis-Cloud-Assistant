from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


def _normalize_ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _split_into_subtasks(text: str, *, max_parts: int = 6) -> List[str]:
    """Best-effort split for multi-step instructions.

    Examples:
      - "open chrome and search for cats" -> ["open chrome", "search for cats"]
      - "open notepad; write hello" -> ["open notepad", "write hello"]

    This is intentionally conservative; if it can't split safely it returns [text].
    """
    t = _normalize_ws(text)
    if not t:
        return []

    # Prefer explicit separators.
    parts: List[str] = []
    for chunk in re.split(r"\s*(?:;|\n|\r\n)+\s*", t):
        chunk = _normalize_ws(chunk)
        if chunk:
            parts.append(chunk)

    if len(parts) == 1:
        # "and then" / "then" / "and" split. Avoid splitting inside URLs.
        if not re.search(r"https?://", t, re.IGNORECASE):
            parts = [p for p in re.split(r"\s+(?:and\s+then|then|and)\s+", t, flags=re.IGNORECASE) if _normalize_ws(p)]

    # Clamp
    out: List[str] = []
    for p in parts:
        p = _normalize_ws(p)
        if p:
            out.append(p)
        if len(out) >= max_parts:
            break

    return out or [t]


def _is_explicit_shell_command(text: str) -> bool:
    tl = (text or "").strip().lower()
    if not tl:
        return False

    # If the user clearly provides a command.
    if tl.startswith("cmd:") or tl.startswith("powershell:") or tl.startswith("bash:"):
        return True

    # "run <command>" is ambiguous; accept only if it looks like a real command.
    if tl.startswith("run ") or tl.startswith("execute "):
        cmd = tl.split(" ", 1)[1].strip()
        if not cmd:
            return False
        return bool(re.search(r"\b(pip|python|node|npm|pnpm|yarn|git|pytest|uvicorn|docker|kubectl|conda)\b", cmd))

    return False


def _extract_shell_command(text: str) -> str:
    t = (text or "").strip()
    tl = t.lower()
    if tl.startswith("cmd:"):
        return t[4:].strip()
    if tl.startswith("powershell:"):
        return t[len("powershell:"):].strip()
    if tl.startswith("bash:"):
        return t[5:].strip()
    if tl.startswith("run "):
        return t[4:].strip()
    if tl.startswith("execute "):
        return t[len("execute "):].strip()
    return t


@dataclass
class AgentLoop:
    """Rule-first agent loop.

    Goal: produce *actionable* structured actions when high-confidence, and only
    fall back to the LLM when intent is ambiguous.

    This intentionally avoids complex prompting and keeps execution in existing
    layers (permissions in ChatOrchestrator + execution in ActionExecutor).
    """

    decision_maker: Any
    min_confidence: float = 0.88
    max_subtasks: int = 6

    async def propose(self, *, text: str, mode: str, context: str = "") -> Optional[JsonDict]:
        user_text = _normalize_ws(text)
        if not user_text:
            return None

        # Hard stop/cancel flows (deterministic).
        if re.fullmatch(r"(?i)\b(stop|cancel|abort|pause)\b", user_text):
            return {
                "text": "Stopping the current task.",
                "actions": [{"type": "stop_task"}],
                "mode": mode,
                "source": "agentic",
                "confidence": 1.0,
            }

        # Explicit shell commands.
        if _is_explicit_shell_command(user_text):
            cmd = _extract_shell_command(user_text)
            if cmd:
                return {
                    "text": f"Running: {cmd}",
                    "actions": [{"type": "execute_command", "command": cmd, "wait": True}],
                    "mode": mode,
                    "source": "agentic",
                    "confidence": 0.95,
                }

        # Multi-step planning.
        subtasks = _split_into_subtasks(user_text, max_parts=self.max_subtasks)
        if not subtasks:
            return None

        actions: List[dict] = []
        conf = 1.0

        for sub in subtasks:
            # Use mode-aware decision maker.
            try:
                decision = await self.decision_maker.make_decision(sub, context=None)
            except Exception:
                decision = None

            if not isinstance(decision, dict):
                conf = min(conf, 0.0)
                continue

            action = decision.get("recommended_action")
            a_conf = float(decision.get("confidence") or 0.0)
            conf = min(conf, a_conf)

            if isinstance(action, dict) and (action.get("type") or "").strip():
                actions.append(action)

        # If we got no useful actions, let the LLM handle it.
        if not actions:
            return None

        if conf < float(self.min_confidence):
            # Too uncertain; avoid executing the wrong thing.
            return {
                "text": "I’m not fully sure what you want me to do. Tell me one of these:\n"
                "- open an app (which one?)\n"
                "- search the web (what query?)\n"
                "- open a website (what URL?)",
                "actions": [],
                "mode": mode,
                "source": "agentic-clarify",
                "confidence": conf,
                "clarification": {
                    "kind": "generic",
                    "question": "Should I open an app, search the web, or open a website?",
                    "original_user_text": user_text,
                },
            }

        # High-confidence plan.
        if len(actions) == 1:
            t = (actions[0].get("type") or "").strip()
            if t == "open_app":
                name = actions[0].get("app_name") or "the app"
                ack = f"Opening {name}."
            elif t == "open_url":
                ack = "Opening the website."
            elif t == "web_search":
                ack = "Searching the web."
            elif t == "create_task":
                ack = "Creating the task."
            else:
                ack = "Working on it."
        else:
            ack = "Okay. I’ll do that step by step."

        return {
            "text": ack,
            "actions": actions,
            "mode": mode,
            "source": "agentic",
            "confidence": conf,
        }


_agent_loop_singleton: AgentLoop | None = None


async def get_agent_loop(*, min_confidence: float = 0.88, max_subtasks: int = 6) -> AgentLoop:
    global _agent_loop_singleton
    if _agent_loop_singleton is not None:
        return _agent_loop_singleton

    from src.core.mode_aware_decision_maker import get_mode_aware_decision_maker

    maker = await get_mode_aware_decision_maker()
    _agent_loop_singleton = AgentLoop(
        decision_maker=maker,
        min_confidence=float(min_confidence),
        max_subtasks=int(max_subtasks),
    )
    return _agent_loop_singleton
