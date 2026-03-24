from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import ROOT_DIR, load_jsonl, stable_key


def _count_nonempty(rows: list[dict[str, Any]], keys: list[str]) -> int:
    c = 0
    for r in rows:
        for k in keys:
            if str(r.get(k) or "").strip():
                c += 1
                break
    return c


def inspect_dataset(base_dir: str | None = None) -> dict[str, Any]:
    root = Path(base_dir) if base_dir else (ROOT_DIR / "data" / "ai_training" / "datasets")

    instruction_rows = load_jsonl(root / "instruction_dataset.jsonl")
    conversation_rows = load_jsonl(root / "conversation_dataset.jsonl")
    task_rows = load_jsonl(root / "task_dataset.jsonl")
    error_rows = load_jsonl(root / "error_dataset.jsonl")

    all_rows = instruction_rows + conversation_rows + task_rows + error_rows

    keys = set()
    dup = 0
    for r in all_rows:
        k = stable_key([
            r.get("input") or r.get("prompt") or r.get("user") or "",
            r.get("expected_output") or r.get("completion") or r.get("assistant") or "",
            r.get("type") or "",
        ])
        if k in keys:
            dup += 1
        else:
            keys.add(k)

    total = len(all_rows)
    duplicate_rate = (dup / total) if total else 0.0

    masked_sensitive = True
    for r in all_rows[:1000]:
        s = str(r)
        if "sk-" in s or "Bearer " in s:
            masked_sensitive = False
            break

    return {
        "total_samples": total,
        "instruction_samples": len(instruction_rows),
        "conversation_samples": len(conversation_rows),
        "task_samples": len(task_rows),
        "error_samples": len(error_rows),
        "duplicate_rate": round(duplicate_rate, 4),
        "masked_sensitive": masked_sensitive,
    }
