from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import load_jsonl, append_jsonl


def export_eval_samples(instruction_path: Path, error_path: Path, output_path: Path, limit: int = 200) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    rows = (load_jsonl(instruction_path) + load_jsonl(error_path))[: max(1, int(limit))]
    count = 0
    for r in rows:
        q = str(r.get("input") or r.get("prompt") or r.get("error") or "").strip()
        a = str(r.get("expected_output") or r.get("completion") or r.get("fix_suggestion") or "").strip()
        if not q:
            continue
        append_jsonl(output_path, {
            "question": q,
            "expected": a,
            "category": str(r.get("type") or "general"),
        })
        count += 1
    return {"status": "success", "rows": count, "output": str(output_path)}
