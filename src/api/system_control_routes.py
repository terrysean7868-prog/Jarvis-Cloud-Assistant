from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter
from pydantic import BaseModel

try:
    from src.utils.db import db as database
except Exception:
    database = None


class OpenAppRequest(BaseModel):
    app_name: str
    args: list[str] | None = None
    session_id: str | None = None


class AppNameRequest(BaseModel):
    app_name: str
    session_id: str | None = None


class ExecuteCommandRequest(BaseModel):
    command: str
    wait: bool = True
    session_id: str | None = None


def build_system_control_router(
    *,
    cloud_mode: bool,
    cloud_feature_disabled: Callable[[str], Any],
    cloud_safe_system_info: Callable[[], dict[str, Any]] | None,
    cloud_delegate_or_queue: Callable[..., Any] | None,
    require_admin_session: Callable[[str | None], Any],
    require_authenticated_session: Callable[[str | None], Any],
    screen_access: Any,
    app_manager: Any,
    system_ops_available: bool,
    system_ops: Any,
) -> APIRouter:
    router = APIRouter(tags=["system-control"])

    # Tuned wait window for read-only delegated actions so connected agents
    # can return completed results more consistently before we fall back.
    WAIT_FOR_AGENT_RESULT_TIMEOUT = 12.0

    def _system_ops_unavailable():
        return {
            "status": "restricted",
            "mode": "local",
            "execution": None,
            "message": "System operations not available on this platform",
        }

    def _to_json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): _to_json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_to_json_safe(v) for v in value]
        iso = getattr(value, "isoformat", None)
        if callable(iso):
            try:
                return iso()
            except Exception:
                pass
        return str(value)

    def _normalize_local_payload(payload: Any, *, default_status: str = "completed") -> dict[str, Any]:
        out = dict(payload) if isinstance(payload, dict) else {"value": payload}
        s = str(out.get("status") or "").strip().lower()
        if s in {"success", "ok", "done"}:
            out["status"] = "completed"
        elif s in {"error", "failed"}:
            out["status"] = "failed"
        elif not s:
            out["status"] = default_status
        out.setdefault("mode", "local")
        out.setdefault("execution", None)
        return _to_json_safe(out)

    def _sanitized_readonly(feature: str, payload_key: str, default_value: Any):
        return {
            "status": "success",
            "mode": "cloud",
            "details_level": "sanitized",
            payload_key: default_value,
            "restriction": {
                "feature": feature,
                "required_by": "admin",
                "delegated": True,
                "delegated_to": "pc_agent",
                "guidance": [
                    "Connect a PC agent for local runtime data.",
                    "Cloud mode returns sanitized placeholders for local-only details.",
                ],
            },
        }

    def _first_agent_result(delegate_payload: dict[str, Any]) -> dict[str, Any]:
        payload = (delegate_payload or {}).get("agent_result") or {}
        results = payload.get("results") or []
        if isinstance(results, list) and results and isinstance(results[0], dict):
            first = dict(results[0])
            # New contract: {status, result, error, execution_time}
            if isinstance(first.get("result"), dict):
                return _to_json_safe(dict(first.get("result") or {}))
            return _to_json_safe(first)
        direct = (delegate_payload or {}).get("result")
        if isinstance(direct, dict):
            return _to_json_safe(dict(direct))
        return {}

    def _ensure_cloud_envelope(payload: Any, *, default_message: str = "Delegated to PC agent") -> dict[str, Any]:
        out = dict(payload) if isinstance(payload, dict) else {"status": "failed", "mode": "cloud", "execution": None}
        out.setdefault("mode", "cloud")
        out.setdefault("execution", _to_json_safe(payload) if isinstance(payload, dict) else None)
        if "result" not in out:
            out["result"] = _first_agent_result(out)
        if "message" not in out:
            out["message"] = default_message
        return _to_json_safe(out)

    async def _cloud_exec(
        *,
        session_id: str | None,
        feature: str,
        actions: list[dict[str, Any]],
        source_text: str,
        require_admin: bool,
        await_timeout_s: float = 2.5,
    ) -> dict[str, Any]:
        if database is not None:
            try:
                database.save_system_event(
                    "agent_execution_requested",
                    f"Delegated {feature}",
                    "pending",
                    {
                        "feature": feature,
                        "session_id": session_id,
                        "source": "system_control",
                        "mode": "cloud",
                        "actions": actions,
                    },
                )
            except Exception:
                pass
        if callable(cloud_delegate_or_queue):
            delegated = await cloud_delegate_or_queue(
                session_id=session_id,
                feature=feature,
                actions=actions,
                source_text=source_text,
                require_admin=require_admin,
                await_timeout_s=await_timeout_s,
            )
            if isinstance(delegated, dict):
                if database is not None:
                    try:
                        st = str(delegated.get("status") or "queued_for_agent").strip().lower()
                        database.save_system_event(
                            "agent_execution_result",
                            f"Delegated {feature} status={st}",
                            "error" if st in {"failed", "error", "forbidden"} else "success",
                            {
                                "feature": feature,
                                "session_id": session_id,
                                "source": "system_control",
                                "mode": "cloud",
                                "delegated_status": st,
                                "actions": actions,
                            },
                        )
                    except Exception:
                        pass
                return _ensure_cloud_envelope(delegated)
            return {"status": "failed", "mode": "cloud", "message": "Invalid delegated payload"}
        cloud_feature_disabled(feature)
        return {
            "status": "restricted",
            "mode": "cloud",
            "feature": feature,
        }

    @router.post("/api/capture-screen")
    async def capture_screen_endpoint(region: dict | None = None):
        if cloud_mode:
            sid = (region or {}).get("session_id") if isinstance(region, dict) else None
            req_action = {
                "type": "capture_screen",
            }
            if isinstance(region, dict):
                if "include_base64" in region:
                    req_action["include_base64"] = bool(region.get("include_base64"))
                if isinstance(region.get("save_path"), str) and region.get("save_path"):
                    req_action["save_path"] = region.get("save_path")
                reg = region.get("region")
                if isinstance(reg, dict):
                    req_action["region"] = reg

            delegated = await _cloud_exec(
                session_id=sid,
                feature="Screen capture",
                actions=[req_action],
                source_text="capture_screen",
                require_admin=True,
                await_timeout_s=5.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if isinstance(out, dict) and out:
                    out.setdefault("mode", "cloud")
                    out["execution"] = delegated
                    return out
            return {
                "status": delegated.get("status") or "delegated",
                "mode": "cloud",
                "execution": delegated,
                "message": "Screen capture delegated to PC agent.",
            }
        sid = (region or {}).get("session_id") if isinstance(region, dict) else None
        require_admin_session(sid)
        try:
            reg = None
            if region:
                reg = (region.get("x"), region.get("y"), region.get("width"), region.get("height"))
            include_base64 = bool((region or {}).get("include_base64", False)) if isinstance(region, dict) else False
            return screen_access.take_screenshot_info(region=reg, include_base64=include_base64)
        except Exception as e:
            return {"error": str(e)}

    @router.post("/api/read-screen")
    async def read_screen_endpoint(region: dict | None = None):
        if cloud_mode:
            sid = (region or {}).get("session_id") if isinstance(region, dict) else None
            delegated = await _cloud_exec(
                session_id=sid,
                feature="Screen OCR",
                actions=[{"type": "analyze_screen"}],
                source_text="read_screen",
                require_admin=True,
                await_timeout_s=5.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                return {
                    "status": "success" if str((out or {}).get("status") or "").lower() in {"success", "completed", "ok"} else "error",
                    "text": (out or {}).get("text_excerpt") or "",
                    "mode": "cloud",
                    "execution": delegated,
                }
            return {
                "status": delegated.get("status") or "delegated",
                "text": "",
                "mode": "cloud",
                "execution": delegated,
            }
        sid = (region or {}).get("session_id") if isinstance(region, dict) else None
        require_admin_session(sid)
        try:
            reg = None
            if region:
                reg = (region.get("x"), region.get("y"), region.get("width"), region.get("height"))
            text = screen_access.read_screen_text(reg)
            return {"text": text, "status": "success"}
        except Exception as e:
            return {"error": str(e), "status": "error"}

    @router.post("/api/open-app")
    async def open_app_endpoint(request: OpenAppRequest):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=request.session_id,
                feature="Opening local applications",
                actions=[{"type": "open_app", "app_name": request.app_name, "args": request.args or []}],
                source_text=f"open_app:{request.app_name}",
                require_admin=True,
                await_timeout_s=3.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session(request.session_id)
        return app_manager.open_app(request.app_name, request.args)

    @router.post("/api/close-app")
    async def close_app_endpoint(request: AppNameRequest):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=request.session_id,
                feature="Closing local applications",
                actions=[{"type": "close_app", "app_name": request.app_name}],
                source_text=f"close_app:{request.app_name}",
                require_admin=True,
                await_timeout_s=3.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session(request.session_id)
        return app_manager.close_app(request.app_name)

    @router.post("/api/switch-app")
    async def switch_app_endpoint(request: AppNameRequest):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=request.session_id,
                feature="Switching local applications",
                actions=[{"type": "switch_app", "app_name": request.app_name}],
                source_text=f"switch_app:{request.app_name}",
                require_admin=True,
                await_timeout_s=3.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session(request.session_id)
        return app_manager.switch_to_app(request.app_name)

    @router.get("/api/running-apps")
    async def get_running_apps(session_id: str):
        require_authenticated_session(session_id)
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=session_id,
                feature="Listing local applications",
                actions=[{"type": "list_running_apps"}],
                source_text="running_apps",
                require_admin=False,
                await_timeout_s=WAIT_FOR_AGENT_RESULT_TIMEOUT,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                apps = (out or {}).get("apps") if isinstance(out, dict) else []
                return {
                    "status": "success",
                    "apps": apps if isinstance(apps, list) else [],
                    "mode": "cloud",
                    "execution": delegated,
                }
            return {
                "status": delegated.get("status") or "queued_for_agent",
                "apps": [],
                "mode": "cloud",
                "execution": delegated,
            }
        apps = app_manager.list_running_apps()
        return {
            "status": "completed",
            "apps": apps if isinstance(apps, list) else [],
            "count": len(apps) if isinstance(apps, list) else 0,
            "mode": "local",
            "execution": None,
        }

    @router.post("/api/execute-command")
    async def execute_command_endpoint(request: ExecuteCommandRequest):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=request.session_id,
                feature="Executing commands",
                actions=[{"type": "execute_command", "command": request.command, "wait": bool(request.wait)}],
                source_text=f"execute_command:{request.command}",
                require_admin=True,
                await_timeout_s=8.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session(request.session_id)
        return app_manager.execute_command(request.command, request.wait)

    @router.get("/api/system/info")
    async def get_system_info(session_id: str):
        require_authenticated_session(session_id)
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=session_id,
                feature="System operations",
                actions=[{"type": "system_info"}],
                source_text="system_info",
                require_admin=False,
                await_timeout_s=WAIT_FOR_AGENT_RESULT_TIMEOUT,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if isinstance(out, dict) and out:
                    out.setdefault("mode", "cloud")
                    out["execution"] = delegated
                    return out
            fallback = cloud_safe_system_info() if callable(cloud_safe_system_info) else _sanitized_readonly("System operations", "local_system_details", "restricted")
            if isinstance(fallback, dict):
                fallback["status"] = delegated.get("status") or fallback.get("status") or "awaiting_agent"
                fallback["execution"] = delegated
            return fallback
        if not system_ops_available:
            return _system_ops_unavailable()
        return _normalize_local_payload(system_ops.get_system_info(), default_status="available")

    @router.get("/api/system/processes")
    async def list_processes_endpoint(session_id: str, filter: Optional[str] = None):
        require_authenticated_session(session_id)
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=session_id,
                feature="System operations",
                actions=[{"type": "list_processes", "filter": filter}],
                source_text="list_processes",
                require_admin=False,
                await_timeout_s=WAIT_FOR_AGENT_RESULT_TIMEOUT,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                return {
                    "status": "success",
                    "processes": (out or {}).get("processes") if isinstance((out or {}).get("processes"), list) else [],
                    "count": int((out or {}).get("count") or 0),
                    "mode": "cloud",
                    "execution": delegated,
                }
            return {
                "status": delegated.get("status") or "queued_for_agent",
                "processes": [],
                "count": 0,
                "mode": "cloud",
                "execution": delegated,
            }
        if not system_ops_available:
            return _system_ops_unavailable()
        out = system_ops.list_processes(filter)
        if isinstance(out, dict):
            out = dict(out)
            out.setdefault("processes", [])
            if not isinstance(out.get("processes"), list):
                out["processes"] = []
            out.setdefault("count", len(out.get("processes") or []))
            return _normalize_local_payload(out)
        return _normalize_local_payload({"processes": [], "count": 0})

    @router.post("/api/system/process-kill")
    async def kill_process_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "kill_process", "name": (req or {}).get("process_name")}],
                source_text=f"kill_process:{(req or {}).get('process_name')}",
                require_admin=True,
                await_timeout_s=6.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        process_name = req.get("process_name")
        if not process_name:
            return {"status": "error", "message": "process_name required"}
        return system_ops.kill_process(process_name)

    @router.post("/api/system/launch-app")
    async def launch_application_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "launch_application", "app_path": (req or {}).get("app_path"), "args": (req or {}).get("args", [])}],
                source_text=f"launch_application:{(req or {}).get('app_path')}",
                require_admin=True,
                await_timeout_s=6.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        app_path = req.get("app_path")
        args = req.get("args", [])
        if not app_path:
            return {"status": "error", "message": "app_path required"}
        return system_ops.launch_application(app_path, args)

    @router.post("/api/system/execute")
    async def system_execute_command_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "execute_command", "command": (req or {}).get("command"), "wait": True}],
                source_text=f"system_execute:{(req or {}).get('command')}",
                require_admin=True,
                await_timeout_s=8.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        command = req.get("command")
        timeout = req.get("timeout", 30)
        if not command:
            return {"status": "error", "message": "command required"}
        return _normalize_local_payload(system_ops.execute_command(command, timeout))

    @router.get("/api/system/screen")
    async def get_screen_info(session_id: str):
        require_authenticated_session(session_id)
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=session_id,
                feature="System operations",
                actions=[{"type": "screen_info"}],
                source_text="screen_info",
                require_admin=False,
                await_timeout_s=WAIT_FOR_AGENT_RESULT_TIMEOUT,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if isinstance(out, dict) and out:
                    out.setdefault("mode", "cloud")
                    out["execution"] = delegated
                    return out
            return {
                "status": delegated.get("status") or "awaiting_agent",
                "mode": "cloud",
                "screen_width": None,
                "screen_height": None,
                "mouse_x": None,
                "mouse_y": None,
                "execution": delegated,
            }
        if not system_ops_available:
            return _system_ops_unavailable()
        return _normalize_local_payload(system_ops.get_screen_info(), default_status="available")

    @router.post("/api/system/screenshot")
    async def take_screenshot(req: dict = None):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "capture_screen", "save_path": (req or {}).get("save_path") if isinstance(req, dict) else None}],
                source_text="take_screenshot",
                require_admin=True,
                await_timeout_s=6.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        save_path = req.get("save_path") if req else None
        return system_ops.take_screenshot(save_path)

    @router.post("/api/system/mouse-move")
    async def move_mouse_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "screen_navigation", "command": "move", "x": (req or {}).get("x"), "y": (req or {}).get("y")}],
                source_text="move_mouse",
                require_admin=True,
                await_timeout_s=4.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        x = req.get("x")
        y = req.get("y")
        if x is None or y is None:
            return {"status": "error", "message": "x and y required"}
        return system_ops.move_mouse(int(x), int(y))

    @router.post("/api/system/mouse-click")
    async def click_mouse_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "screen_navigation", "command": "click", "x": (req or {}).get("x"), "y": (req or {}).get("y"), "button": (req or {}).get("button", "left")}],
                source_text="click_mouse",
                require_admin=True,
                await_timeout_s=4.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        x = req.get("x")
        y = req.get("y")
        button = req.get("button", "left")
        if x is None or y is None:
            return {"status": "error", "message": "x and y required"}
        return system_ops.click_mouse(int(x), int(y), button)

    @router.post("/api/system/type-text")
    async def type_text_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "type_text", "text": (req or {}).get("text"), "interval": (req or {}).get("interval", 0.1)}],
                source_text="type_text",
                require_admin=True,
                await_timeout_s=5.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        text = req.get("text")
        interval = req.get("interval", 0.1)
        if not text:
            return {"status": "error", "message": "text required"}
        return system_ops.type_text(text, interval)

    @router.post("/api/system/press-key")
    async def press_key_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "press_key", "key": (req or {}).get("key")}],
                source_text="press_key",
                require_admin=True,
                await_timeout_s=4.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        key = req.get("key")
        if not key:
            return {"status": "error", "message": "key required"}
        return system_ops.press_key(key)

    @router.post("/api/system/open-file")
    async def open_file_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "open_path", "path": (req or {}).get("file_path")}],
                source_text=f"open_file:{(req or {}).get('file_path')}",
                require_admin=True,
                await_timeout_s=4.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        file_path = req.get("file_path")
        if not file_path:
            return {"status": "error", "message": "file_path required"}
        return system_ops.open_file(file_path)

    @router.get("/api/system/windows")
    async def get_open_windows(session_id: str):
        require_authenticated_session(session_id)
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=session_id,
                feature="System operations",
                actions=[{"type": "open_windows"}],
                source_text="open_windows",
                require_admin=False,
                await_timeout_s=WAIT_FOR_AGENT_RESULT_TIMEOUT,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                windows = (out or {}).get("windows") if isinstance(out, dict) else []
                return {
                    "status": "success",
                    "windows": windows if isinstance(windows, list) else [],
                    "count": int((out or {}).get("count") or 0),
                    "mode": "cloud",
                    "execution": delegated,
                }
            return {
                "status": delegated.get("status") or "queued_for_agent",
                "windows": [],
                "count": 0,
                "mode": "cloud",
                "execution": delegated,
            }
        if not system_ops_available:
            return _system_ops_unavailable()
        out = system_ops.get_open_windows()
        if isinstance(out, dict):
            normalized = dict(out)
            normalized.setdefault("windows", [])
            if not isinstance(normalized.get("windows"), list):
                normalized["windows"] = []
            normalized.setdefault("count", len(normalized.get("windows") or []))
            return _normalize_local_payload(normalized)
        return _normalize_local_payload({"windows": [], "count": 0})

    @router.post("/api/system/window-focus")
    async def focus_window_endpoint(req: dict):
        if cloud_mode:
            delegated = await _cloud_exec(
                session_id=(req or {}).get("session_id"),
                feature="System operations",
                actions=[{"type": "switch_app", "app_name": (req or {}).get("window_title")}],
                source_text=f"focus_window:{(req or {}).get('window_title')}",
                require_admin=True,
                await_timeout_s=4.0,
            )
            if delegated.get("status") == "completed":
                out = _first_agent_result(delegated)
                if out:
                    out["execution"] = delegated
                    return out
            return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        window_title = req.get("window_title")
        if not window_title:
            return {"status": "error", "message": "window_title required"}
        return system_ops.focus_window(window_title)

    return router
