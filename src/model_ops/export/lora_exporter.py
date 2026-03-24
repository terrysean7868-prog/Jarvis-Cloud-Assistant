from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import load_jsonl, append_jsonl


def export_lora_dataset(instruction_path: Path, task_path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    rows = load_jsonl(instruction_path) + load_jsonl(task_path)
    count = 0
    for r in rows:
        prompt = str(r.get("input") or r.get("prompt") or r.get("task") or "").strip()
        response = str(r.get("expected_output") or r.get("completion") or r.get("result") or "").strip()
        if not prompt or not response:
            continue
        append_jsonl(output_path, {
            "instruction": prompt,
            "input": "",
            "output": response,
        })
        count += 1
    return {"status": "success", "rows": count, "output": str(output_path)}
