from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def _ok(label: str, details: str = "") -> dict:
    return {"check": label, "status": "ok", "details": details}


def _warn(label: str, details: str = "") -> dict:
    return {"check": label, "status": "warn", "details": details}


def _err(label: str, details: str = "") -> dict:
    return {"check": label, "status": "error", "details": details}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _http_get_json(base_url: str, route: str, session_id: str | None = None, timeout_s: float = 6.0) -> tuple[bool, dict]:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False, {"error": "missing base url"}

    params = {}
    if session_id:
        params["session_id"] = session_id

    query = f"?{urlencode(params)}" if params else ""
    url = f"{base}{route}{query}"
    req = Request(url=url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=max(1.0, float(timeout_s))) as res:
            raw = res.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return True, data if isinstance(data, dict) else {"raw": data}
    except Exception as e:
        return False, {"error": str(e), "url": url}


def run(base_url: str | None = None, session_id: str | None = None) -> dict:
    checks: list[dict] = []

    llm_adapter = ROOT / "src" / "core" / "llm_adapter.py"
    web_app = ROOT / "apps" / "web" / "app.py"
    self_panel = ROOT / "frontend" / "src" / "pages" / "SelfImprovementPanel.jsx"
    auto_dashboard = ROOT / "frontend" / "src" / "pages" / "AutonomyDashboard.jsx"
    app_ui = ROOT / "frontend" / "src" / "App.jsx"

    # Static presence checks
    for p in (llm_adapter, web_app, self_panel, auto_dashboard, app_ui):
        if p.exists():
            checks.append(_ok(f"file_exists:{p.name}", str(p.relative_to(ROOT))))
        else:
            checks.append(_err(f"file_exists:{p.name}", str(p)))

    # Static telemetry markers
    web_txt = _read_text(web_app)
    if "@app.get(\"/api/ops/telemetry\")" in web_txt:
        checks.append(_ok("ops_telemetry_endpoint", "route registered"))
    else:
        checks.append(_warn("ops_telemetry_endpoint", "route not found"))

    required_tokens = [
        "timeout_rate",
        "fallback_rate",
        "queued_for_agent_count",
        "pending_permission_count",
        "average_response_latency_ms",
        "delegated_execution",
    ]
    missing = [t for t in required_tokens if t not in web_txt]
    if missing:
        checks.append(_warn("ops_telemetry_fields", f"missing markers: {', '.join(missing)}"))
    else:
        checks.append(_ok("ops_telemetry_fields", "all required markers found"))

    # UI surface checks (static)
    app_txt = _read_text(app_ui)
    self_txt = _read_text(self_panel)
    auto_txt = _read_text(auto_dashboard)

    app_has_auto = "AutonomyDashboard" in app_txt
    auto_has_self = "SelfImprovementPanel" in auto_txt
    if app_has_auto and auto_has_self:
        checks.append(_ok("ui_surface_mounting", "App mounts AutonomyDashboard and autonomy mounts SelfImprovementPanel"))
    else:
        checks.append(_warn("ui_surface_mounting", "one or more dashboard surfaces are not wired as expected"))

    lifecycle_tokens = ["queued_for_agent", "awaiting_agent", "pending_permission", "delegated"]
    missing_lifecycle = [t for t in lifecycle_tokens if t not in app_txt]
    if missing_lifecycle:
        checks.append(_warn("delegated_lifecycle_rendering", f"missing UI tokens: {', '.join(missing_lifecycle)}"))
    else:
        checks.append(_ok("delegated_lifecycle_rendering", "delegated lifecycle states found in UI handler"))

    if "setStatus(" in self_txt and "refresh" in self_txt:
        checks.append(_ok("self_improvement_panel_state", "status + refresh flow present"))
    else:
        checks.append(_warn("self_improvement_panel_state", "status/refresh pattern not detected"))

    if "getAutonomyStatus" in auto_txt and "setError(" in auto_txt:
        checks.append(_ok("autonomy_dashboard_state", "status fetch + error guard present"))
    else:
        checks.append(_warn("autonomy_dashboard_state", "autonomy status/error guard pattern not detected"))

    # Optional live API checks
    live: dict[str, dict] = {}
    if base_url:
        ok_ops, ops_data = _http_get_json(base_url, "/api/ops/telemetry", session_id=session_id)
        live["ops_telemetry"] = {"ok": ok_ops, "data": ops_data}
        checks.append(_ok("live_ops_telemetry", "reachable") if ok_ops else _warn("live_ops_telemetry", ops_data.get("error", "request failed")))

        ok_auto, auto_data = _http_get_json(base_url, "/api/autonomy/status", session_id=session_id)
        live["autonomy_status"] = {"ok": ok_auto, "data": auto_data}
        checks.append(_ok("live_autonomy_status", "reachable") if ok_auto else _warn("live_autonomy_status", auto_data.get("error", "request failed")))

    summary = {
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "error": sum(1 for c in checks if c["status"] == "error"),
    }

    return {
        "status": "ok" if summary["error"] == 0 else "error",
        "summary": summary,
        "checks": checks,
        "live": live,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Jarvis staging validation checklist/report helper")
    parser.add_argument("--base-url", default="", help="Optional API base URL, e.g. http://127.0.0.1:18001")
    parser.add_argument("--session-id", default="", help="Optional authenticated session_id for cloud/staging API checks")
    args = parser.parse_args(argv)

    report = run(base_url=(args.base_url or None), session_id=(args.session_id or None))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
