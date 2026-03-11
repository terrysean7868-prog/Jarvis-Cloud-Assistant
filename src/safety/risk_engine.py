from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskAssessment:
    score: float
    level: str
    reasons: list[str]


class RiskEngine:
    """Heuristic risk scoring for autonomous actions."""

    def score_action(self, action: dict) -> RiskAssessment:
        action_type = str((action or {}).get("type") or "").strip().lower()
        score = 0.05
        reasons: list[str] = []

        if action_type in {"delete", "move", "self_update", "execute_command", "shutdown", "restart", "logoff"}:
            score += 0.45
            reasons.append("State-changing or potentially destructive action.")

        cmd = str((action or {}).get("command") or "").strip().lower()
        if cmd:
            if any(w in cmd for w in ["rm -rf", "format", "diskpart", "reg delete", "drop database"]):
                score += 0.5
                reasons.append("Dangerous command signature detected.")
            elif any(w in cmd for w in ["pip install", "npm install", "docker build"]):
                score += 0.2
                reasons.append("Build or environment modification command.")

        if action_type in {"capture_screen", "save_screenshot"}:
            score += 0.25
            reasons.append("Potential sensitive visual data access.")

        score = min(1.0, score)
        if score >= 0.8:
            level = "HIGH"
        elif score >= 0.4:
            level = "MEDIUM"
        else:
            level = "LOW"

        return RiskAssessment(score=score, level=level, reasons=reasons)

    def decision_for(self, action: dict) -> str:
        ra = self.score_action(action)
        if ra.level == "LOW":
            return "execute"
        if ra.level == "MEDIUM":
            return "confirm"
        return "block"

    def score_task(self, task: dict) -> RiskAssessment:
        text = f"{task.get('title', '')} {task.get('description', '')}".strip().lower()
        action_like = {"type": str(task.get("agent") or "agent_task").lower()}
        base = self.score_action(action_like)

        score = float(base.score)
        reasons = list(base.reasons)

        if any(k in text for k in ["delete", "remove", "wipe", "drop", "shutdown", "restart"]):
            score += 0.35
            reasons.append("Task language includes destructive operations.")

        if any(k in text for k in ["credentials", "secret", "token", "password", "key"]):
            score += 0.30
            reasons.append("Task may involve sensitive secrets.")

        if any(k in text for k in ["screen", "camera", "microphone", "clipboard"]):
            score += 0.20
            reasons.append("Task touches potentially sensitive local data channels.")

        score = min(1.0, score)
        if score >= 0.8:
            level = "HIGH"
        elif score >= 0.4:
            level = "MEDIUM"
        else:
            level = "LOW"

        return RiskAssessment(score=score, level=level, reasons=reasons)
