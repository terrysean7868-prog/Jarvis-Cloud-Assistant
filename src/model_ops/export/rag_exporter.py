from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import load_jsonl, append_jsonl


def export_rag_docs(conversation_path: Path, task_path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
    rows = load_jsonl(conversation_path) + load_jsonl(task_path)
    count = 0
    for idx, r in enumerate(rows, start=1):
        content = str(r.get("summary") or r.get("result") or r.get("text") or "").strip()
        if not content:
            turns = r.get("turns") if isinstance(r.get("turns"), list) else []
            content = "\n".join([str(t.get("text") or "") for t in turns if isinstance(t, dict)]).strip()
        if not content:
            continue
        append_jsonl(output_path, {
            "doc_id": f"rag_{idx}",
            "title": str(r.get("type") or "runtime_doc"),
            "content": content,
            "tags": r.get("tags") if isinstance(r.get("tags"), list) else [],
        })
        count += 1
    return {"status": "success", "rows": count, "output": str(output_path)}
