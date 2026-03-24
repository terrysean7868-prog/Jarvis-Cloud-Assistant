from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from .runtime_router import resolve_route
from .utils import DATA_DIR, save_json


BENCHMARK_CASES = [
    {"category": "normal_chat", "text": "Hello Jarvis"},
    {"category": "delegated_execution_planning", "text": "Plan steps to open VS Code and run tests"},
    {"category": "permission_negotiation", "text": "Ask permission before deleting files"},
    {"category": "code_debug", "text": "Debug this traceback and suggest fix"},
    {"category": "project_analysis", "text": "Analyze this codebase architecture"},
    {"category": "fallback_quality", "text": "Give short fallback answer if provider fails"},
    {"category": "local_availability", "text": "Can this run offline?"},
]


def run_benchmark(profile_name: str, *, include_latency_probe: bool = True) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in BENCHMARK_CASES:
        t0 = perf_counter()
        route = resolve_route(case["text"], mode="chat", profile_name=profile_name)
        latency_ms = (perf_counter() - t0) * 1000.0 if include_latency_probe else 0.0

        primary_provider = route.get("primary", {}).get("provider")
        ok = primary_provider is not None
        rows.append(
            {
                "category": case["category"],
                "route": route,
                "latency_ms": round(latency_ms, 3),
                "success": bool(ok),
                "failure": None if ok else "no_route",
            }
        )

    total = len(rows)
    success = sum(1 for r in rows if r.get("success"))
    avg_latency = (sum(float(r.get("latency_ms") or 0.0) for r in rows) / total) if total else 0.0

    summary = {
        "profile": profile_name,
        "total": total,
        "success_rate": round((success / total) if total else 0.0, 4),
        "avg_latency_ms": round(avg_latency, 3),
        "failure_rate": round((1.0 - (success / total)) if total else 0.0, 4),
        "score_by_category": {r["category"]: (1.0 if r.get("success") else 0.0) for r in rows},
    }

    ts = datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-")
    path = DATA_DIR / "benchmarks" / f"benchmark_{profile_name}_{ts}.json"
    save_json(path, {"summary": summary, "rows": rows})

    return {"summary": summary, "rows": rows, "path": str(path)}


def latest_benchmark_report() -> dict[str, Any]:
    folder = DATA_DIR / "benchmarks"
    if not folder.exists():
        return {"status": "not_found", "report": None}
    files = sorted(folder.glob("benchmark_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"status": "not_found", "report": None}
    import json

    data = json.loads(files[0].read_text(encoding="utf-8"))
    return {"status": "success", "report": data, "path": str(files[0])}
