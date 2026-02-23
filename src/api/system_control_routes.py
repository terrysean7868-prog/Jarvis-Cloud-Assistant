from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter
from pydantic import BaseModel


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
    require_admin_session: Callable[[str | None], Any],
    require_authenticated_session: Callable[[str | None], Any],
    screen_access: Any,
    app_manager: Any,
    system_ops_available: bool,
    system_ops: Any,
) -> APIRouter:
    router = APIRouter(tags=["system-control"])

    def _system_ops_unavailable():
        return {"status": "error", "message": "System operations not available on this platform"}

    @router.post("/api/capture-screen")
    async def capture_screen_endpoint(region: dict | None = None):
        if cloud_mode:
            cloud_feature_disabled("Screen capture")
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
            cloud_feature_disabled("Screen OCR")
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
            cloud_feature_disabled("Opening local applications")
        require_admin_session(request.session_id)
        return app_manager.open_app(request.app_name, request.args)

    @router.post("/api/close-app")
    async def close_app_endpoint(request: AppNameRequest):
        if cloud_mode:
            cloud_feature_disabled("Closing local applications")
        require_admin_session(request.session_id)
        return app_manager.close_app(request.app_name)

    @router.post("/api/switch-app")
    async def switch_app_endpoint(request: AppNameRequest):
        if cloud_mode:
            cloud_feature_disabled("Switching local applications")
        require_admin_session(request.session_id)
        return app_manager.switch_to_app(request.app_name)

    @router.get("/api/running-apps")
    async def get_running_apps(session_id: str):
        if cloud_mode:
            cloud_feature_disabled("Listing local applications")
        require_authenticated_session(session_id)
        return {"apps": app_manager.list_running_apps()}

    @router.post("/api/execute-command")
    async def execute_command_endpoint(request: ExecuteCommandRequest):
        if cloud_mode:
            cloud_feature_disabled("Executing commands")
        require_admin_session(request.session_id)
        return app_manager.execute_command(request.command, request.wait)

    @router.get("/api/system/info")
    async def get_system_info(session_id: str):
        if cloud_mode:
            cloud_feature_disabled("System operations")
        require_authenticated_session(session_id)
        if not system_ops_available:
            return _system_ops_unavailable()
        return system_ops.get_system_info()

    @router.get("/api/system/processes")
    async def list_processes_endpoint(session_id: str, filter: Optional[str] = None):
        if cloud_mode:
            cloud_feature_disabled("System operations")
        require_authenticated_session(session_id)
        if not system_ops_available:
            return _system_ops_unavailable()
        return system_ops.list_processes(filter)

    @router.post("/api/system/process-kill")
    async def kill_process_endpoint(req: dict):
        if cloud_mode:
            cloud_feature_disabled("System operations")
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
            cloud_feature_disabled("System operations")
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
            cloud_feature_disabled("System operations")
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        command = req.get("command")
        timeout = req.get("timeout", 30)
        if not command:
            return {"status": "error", "message": "command required"}
        return system_ops.execute_command(command, timeout)

    @router.get("/api/system/screen")
    async def get_screen_info(session_id: str):
        if cloud_mode:
            cloud_feature_disabled("System operations")
        require_authenticated_session(session_id)
        if not system_ops_available:
            return _system_ops_unavailable()
        return system_ops.get_screen_info()

    @router.post("/api/system/screenshot")
    async def take_screenshot(req: dict = None):
        if cloud_mode:
            cloud_feature_disabled("System operations")
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        save_path = req.get("save_path") if req else None
        return system_ops.take_screenshot(save_path)

    @router.post("/api/system/mouse-move")
    async def move_mouse_endpoint(req: dict):
        if cloud_mode:
            cloud_feature_disabled("System operations")
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
            cloud_feature_disabled("System operations")
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
            cloud_feature_disabled("System operations")
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
            cloud_feature_disabled("System operations")
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        key = req.get("key")
        if not key:
            return {"status": "error", "message": "key required"}
        return system_ops.press_key(key)

    @router.post("/api/system/open-file")
    async def open_file_endpoint(req: dict):
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
        if not system_ops_available:
            return _system_ops_unavailable()
        return system_ops.get_open_windows()

    @router.post("/api/system/window-focus")
    async def focus_window_endpoint(req: dict):
        require_admin_session((req or {}).get("session_id"))
        if not system_ops_available:
            return _system_ops_unavailable()
        window_title = req.get("window_title")
        if not window_title:
            return {"status": "error", "message": "window_title required"}
        return system_ops.focus_window(window_title)

    return router
