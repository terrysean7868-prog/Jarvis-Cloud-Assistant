from __future__ import annotations

from pathlib import Path
from typing import Any


class CodeAnalyzer:
    """Lightweight static analyzer used by autonomous self-improvement workflows."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]

    def analyze_file(self, file_path: str) -> dict[str, Any]:
        p = Path(file_path)
        if not p.is_absolute():
            p = self.repo_root / p
        if not p.exists():
            return {"status": "error", "message": f"File not found: {p}"}

        text = p.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        long_lines = [idx + 1 for idx, line in enumerate(lines) if len(line) > 120]
        todos = [idx + 1 for idx, line in enumerate(lines) if "TODO" in line or "FIXME" in line]

        return {
            "status": "success",
            "path": str(p),
            "line_count": len(lines),
            "long_lines": long_lines[:50],
            "todo_markers": todos[:50],
        }
