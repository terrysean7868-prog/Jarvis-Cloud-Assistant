from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import load_jsonl, append_jsonl


def export_instruction_jsonl(input_path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    rows = load_jsonl(input_path)
    count = 0
    for r in rows:
        item = {
            "prompt": str(r.get("input") or r.get("prompt") or "").strip(),
            "completion": str(r.get("expected_output") or r.get("completion") or "").strip(),
        }
        if not item["prompt"] or not item["completion"]:
            continue
        append_jsonl(output_path, item)
        count += 1
    return {"status": "success", "rows": count, "output": str(output_path)}


def export_conversation_jsonl(input_path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    rows = load_jsonl(input_path)
    count = 0
    for r in rows:
        turns = r.get("turns") if isinstance(r.get("turns"), list) else []
        if not turns:
            continue
        append_jsonl(output_path, {"messages": turns})
        count += 1
    return {"status": "success", "rows": count, "output": str(output_path)}
