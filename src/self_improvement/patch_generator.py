from __future__ import annotations

from typing import Any


class PatchGenerator:
    """Generates safe patch proposals (textual) from analysis output."""

    def generate_patch_plan(self, *, file_path: str, findings: dict[str, Any], objective: str) -> dict[str, Any]:
        suggestions = []
        if findings.get("long_lines"):
            suggestions.append("Refactor long lines into readable multi-line statements.")
        if findings.get("todo_markers"):
            suggestions.append("Resolve TODO/FIXME markers with concrete implementations.")
        if not suggestions:
            suggestions.append("No obvious static issues found; focus on behavior and tests.")

        return {
            "status": "success",
            "file_path": file_path,
            "objective": objective,
            "suggestions": suggestions,
            "proposed_changes": [
                {
                    "type": "refactor",
                    "description": s,
                }
                for s in suggestions
            ],
        }
