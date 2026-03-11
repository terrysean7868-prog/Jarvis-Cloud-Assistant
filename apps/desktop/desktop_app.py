import argparse
import os
import socket
import threading
import time
import webbrowser
from urllib.parse import urlparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18001


def _is_local_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return (
        u.startswith("http://127.0.0.1")
        or u.startswith("http://localhost")
        or u.startswith("https://127.0.0.1")
        or u.startswith("https://localhost")
    )


def _normalize_local_url_and_bind(url: str, fallback_host: str, fallback_port: int) -> tuple[str, str, int]:
    raw = (url or "").strip()
    if not raw:
        host = (fallback_host or DEFAULT_HOST).strip() or DEFAULT_HOST
        port = int(fallback_port or DEFAULT_PORT)
        return f"http://{host}:{port}", host, port

    p = urlparse(raw)
    host = (p.hostname or fallback_host or DEFAULT_HOST).strip()
    port = int(p.port or fallback_port or DEFAULT_PORT)
    scheme = (p.scheme or "http").strip().lower() or "http"

    if host in ("127.0.0.1", "localhost") and scheme == "https":
        scheme = "http"

    normalized = f"{scheme}://{host}:{port}"
    return normalized, host, port


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return True
    except Exception:
        return False


def _notify(title: str, message: str) -> None:
    msg = str(message or "").strip() or "Jarvis desktop event"
    try:
        from plyer import notification  # type: ignore

        notification.notify(title=title, message=msg, app_name="Jarvis Desktop", timeout=5)
        return
    except Exception:
        pass

    try:
        print(f"[{title}] {msg}")
    except Exception:
        pass


def _post_json(url: str, payload: dict) -> dict:
    try:
        import requests

        res = requests.post(url, json=payload, timeout=7)
        if res.ok:
            return res.json() if res.text else {}
    except Exception:
        pass
    return {}


def _get_json(url: str) -> dict:
    try:
        import requests

        res = requests.get(url, timeout=7)
        if res.ok:
            return res.json() if res.text else {}
    except Exception:
        pass
    return {}


class BackendRunner:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)
        self._thread: threading.Thread | None = None
        self._server = None
        self.startup_error: str | None = None

    def start(self) -> None:
        self.startup_error = None
        if _port_open(self.host, self.port):
            return

        import uvicorn
        from app import app as fastapi_app

        config = uvicorn.Config(
            fastapi_app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            log_config=None,
            lifespan="on",
            workers=1,
        )
        self._server = uvicorn.Server(config)

        def _run() -> None:
            try:
                self._server.run()
            except Exception as e:
                self.startup_error = str(e)

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.should_exit = True
        except Exception:
            pass


class DesktopAutonomyMonitor:
    """Background monitor to surface autonomy events as desktop notifications."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen_goal_status: dict[str, str] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def _run() -> None:
            while not self._stop.is_set():
                try:
                    goals_res = _get_json(f"{self.base_url}/api/autonomy/goals?statuses=pending,running,awaiting_confirmation,failed,completed&limit=50")
                    goals = goals_res.get("goals") if isinstance(goals_res, dict) else []
                    if not isinstance(goals, list):
                        goals = []
                    for goal in goals:
                        goal_id = str(goal.get("_id") or "")
                        status = str(goal.get("status") or "")
                        title = str(goal.get("goal") or "Autonomy Goal")
                        if not goal_id:
                            continue
                        prev = self._seen_goal_status.get(goal_id)
                        if prev != status:
                            self._seen_goal_status[goal_id] = status
                            if status == "completed":
                                _notify("Task completion", f"{title} completed")
                            elif status in {"failed", "blocked"}:
                                _notify("Autonomy error", f"{title} status: {status}")
                            elif status == "awaiting_confirmation":
                                _notify("Approval required", f"{title} awaiting confirmation")

                    status_res = _get_json(f"{self.base_url}/api/autonomy/status")
                    health = status_res.get("health") if isinstance(status_res, dict) else {}
                    if isinstance(health, dict):
                        if int(health.get("agents") or 0) <= 0:
                            _notify("Agent monitor", "No active agents detected")
                except Exception:
                    pass

                time.sleep(8)

        self._stop.clear()
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


class DesktopController:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def open_main_dashboard(self) -> None:
        try:
            webbrowser.open(self.base_url)
        except Exception:
            pass

    def open_task_monitor(self) -> None:
        try:
            webbrowser.open(f"{self.base_url}/?panel=tasks")
        except Exception:
            pass

    def open_agent_monitor(self) -> None:
        try:
            webbrowser.open(f"{self.base_url}/?panel=agents")
        except Exception:
            pass

    def microphone_activation(self) -> None:
        try:
            webbrowser.open(f"{self.base_url}/?mic=1")
            _notify("Microphone activation", "Jarvis microphone activation requested")
        except Exception:
            pass

    def quick_command_launcher(self) -> None:
        cmd = ""
        try:
            import tkinter as tk
            from tkinter import simpledialog

            root = tk.Tk()
            root.withdraw()
            cmd = simpledialog.askstring("Jarvis quick command", "Enter command for Jarvis:") or ""
            root.destroy()
        except Exception:
            return

        cmd = cmd.strip()
        if not cmd:
            return

        _post_json(f"{self.base_url}/api/chat", {"user": "user", "text": cmd, "mode": "chat"})
        _notify("Quick command", f"Sent: {cmd}")

    def local_device_control(self) -> None:
        devices = _get_json(f"{self.base_url}/api/device/list").get("devices", [])
        device_id = ""
        if isinstance(devices, list) and devices:
            device_id = str(devices[0].get("device_id") or "")

        if not device_id:
            _notify("Local device control", "No connected device available")
            return

        payload = {
            "device_id": device_id,
            "actions": [{"type": "open_app", "app_name": "notepad"}],
            "source_text": "Desktop quick device control",
        }
        _post_json(f"{self.base_url}/api/device/dispatch", payload)
        _notify("Local device control", f"Sent open app action to {device_id}")


class SystemTray:
    def __init__(self, controller: DesktopController):
        self.controller = controller
        self._icon = None

    def run(self) -> None:
        try:
            import pystray  # type: ignore
            from PIL import Image, ImageDraw  # type: ignore
        except Exception:
            _notify("System tray", "pystray/Pillow not installed, tray disabled")
            return

        img = Image.new("RGB", (64, 64), (4, 20, 36))
        draw = ImageDraw.Draw(img)
        draw.ellipse((14, 14, 50, 50), outline=(50, 198, 255), width=4)
        draw.ellipse((24, 24, 40, 40), fill=(109, 225, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Main dashboard", lambda _: self.controller.open_main_dashboard()),
            pystray.MenuItem("Task monitor", lambda _: self.controller.open_task_monitor()),
            pystray.MenuItem("Agent monitor", lambda _: self.controller.open_agent_monitor()),
            pystray.MenuItem("Quick command", lambda _: self.controller.quick_command_launcher()),
            pystray.MenuItem("Microphone activation", lambda _: self.controller.microphone_activation()),
            pystray.MenuItem("Local device control", lambda _: self.controller.local_device_control()),
            pystray.MenuItem("Exit", self._stop),
        )

        self._icon = pystray.Icon("jarvis-desktop", img, "Jarvis Desktop", menu)
        self._icon.run()

    def _stop(self, icon, _item):
        try:
            icon.stop()
        except Exception:
            pass


def _wait_for_backend(url: str, timeout_s: float = 15.0) -> bool:
    import requests

    deadline = time.time() + max(1.0, float(timeout_s))
    # Canonical backend health endpoint is /health. Keep /api/health as a fallback
    # for older builds.
    health_urls = [
        url.rstrip("/") + "/health",
        url.rstrip("/") + "/api/health",
    ]
    while time.time() < deadline:
        for health_url in health_urls:
            try:
                r = requests.get(health_url, timeout=1.5)
                if r.ok:
                    return True
            except Exception:
                pass
        time.sleep(0.35)
    return False


def _open_ui(url: str, title: str, width: int, height: int) -> None:
    debug_webview = (os.getenv("JARVIS_DESKTOP_WEBVIEW_DEBUG", "") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    backend_pref = (os.getenv("JARVIS_DESKTOP_UI_BACKEND", "auto") or "auto").strip().lower()
    try:
        import webview

        if debug_webview:
            try:
                webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = True
            except Exception:
                pass

        webview.create_window(title=title, url=url, width=width, height=height)

        if backend_pref in ("edgechromium", "edge", "webview2"):
            backends = ["edgechromium"]
        elif backend_pref in ("qt", "pyside", "pyside6"):
            backends = ["qt"]
        elif backend_pref in ("auto", ""):
            backends = ["edgechromium", "qt", None]
        else:
            backends = ["edgechromium", "qt", None]

        last_error = None
        for backend in backends:
            try:
                if backend:
                    webview.start(gui=backend, debug=debug_webview)
                else:
                    webview.start(debug=debug_webview)
                return
            except Exception as e:
                last_error = e

        raise RuntimeError(f"No usable webview backend: {last_error}")
    except Exception as e:
        raise RuntimeError(f"Embedded desktop UI failed: {e}") from e


def _show_error_dialog(title: str, message: str) -> None:
    text = str(message or "").strip() or "Unknown desktop startup error."
    try:
        if os.name == "nt":
            import ctypes

            MB_OK = 0x00000000
            MB_ICONERROR = 0x00000010
            ctypes.windll.user32.MessageBoxW(None, text, title, MB_OK | MB_ICONERROR)
            return
    except Exception:
        pass

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
        root.destroy()
    except Exception:
        try:
            print(f"{title}: {text}")
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis Desktop App")
    parser.add_argument("--host", default=os.getenv("JARVIS_DESKTOP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("JARVIS_DESKTOP_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--url", default="", help="Desktop URL (local-only)")
    parser.add_argument("--no-backend", action="store_true", help="Do not start local backend")
    parser.add_argument("--background", action="store_true", help="Run in background with system tray")
    parser.add_argument("--title", default="Jarvis Desktop")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    args = parser.parse_args()

    host = str(args.host or DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(args.port or DEFAULT_PORT)

    url, host, port = _normalize_local_url_and_bind((args.url or "").strip(), host, port)
    if not _is_local_url(url):
        print("Desktop app is local-only. Use http://127.0.0.1:<port> or http://localhost:<port>.")
        return 2

    os.environ["JARVIS_CLOUD_MODE"] = "false"
    os.environ.setdefault("JARVIS_ENABLE_PC_AGENT", "true")
    os.environ.setdefault("JARVIS_ENABLE_SCHEDULER", "true")
    os.environ.setdefault("JARVIS_DESKTOP_API_URL", url)

    backend = BackendRunner(host=host, port=port)
    monitor = DesktopAutonomyMonitor(url)
    controller = DesktopController(url)
    tray = SystemTray(controller)

    try:
        backend_ready = False
        if not args.no_backend:
            backend.start()
            backend_ready = _wait_for_backend(url, timeout_s=25.0)
        else:
            backend_ready = _wait_for_backend(url, timeout_s=4.0)

        if not backend_ready:
            extra = ""
            if backend.startup_error:
                extra = f"\n\nStartup error: {backend.startup_error}"
            msg = (
                f"Backend not ready at {url}.\n\n"
                "Jarvis Desktop could not start the local API in time.\n"
                "Check whether another process is using the port, then retry."
                f"{extra}"
            )
            print(msg)
            _show_error_dialog("Jarvis Desktop", msg)
            return 1

        monitor.start()

        if args.background:
            _notify("Jarvis Desktop", "Running in background mode")
            tray.run()
            return 0

        tray_thread = threading.Thread(target=tray.run, daemon=True)
        tray_thread.start()

        _open_ui(url=url, title=str(args.title or "Jarvis Desktop"), width=max(900, int(args.width)), height=max(640, int(args.height)))
        return 0
    except Exception as e:
        msg = (
            "Jarvis Desktop failed to open embedded UI.\n\n"
            f"URL: {url}\n"
            f"Error: {e}"
        )
        print(msg)
        _show_error_dialog("Jarvis Desktop", msg)
        return 2
    finally:
        monitor.stop()
        backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
