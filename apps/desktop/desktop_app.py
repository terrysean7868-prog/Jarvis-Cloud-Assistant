import argparse
import os
import socket
import sys
import threading
import time
import webbrowser


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18001


def _is_local_url(url: str) -> bool:
    u = (url or "").strip().lower()
    return u.startswith("http://127.0.0.1") or u.startswith("http://localhost")


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.4):
            return True
    except Exception:
        return False


class BackendRunner:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)
        self._thread: threading.Thread | None = None
        self._server = None

    def start(self) -> None:
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
            except Exception:
                pass

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.should_exit = True
        except Exception:
            pass


def _wait_for_backend(url: str, timeout_s: float = 15.0) -> bool:
    import requests

    deadline = time.time() + max(1.0, float(timeout_s))
    health_url = url.rstrip("/") + "/api/health"
    while time.time() < deadline:
        try:
            r = requests.get(health_url, timeout=1.5)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


def _open_ui(url: str, title: str, width: int, height: int) -> None:
    try:
        import webview

        webview.create_window(title=title, url=url, width=width, height=height)
        webview.start()
    except Exception:
        webbrowser.open(url, new=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis Desktop App")
    parser.add_argument("--host", default=os.getenv("JARVIS_DESKTOP_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("JARVIS_DESKTOP_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--url", default="", help="Desktop URL (local-only)")
    parser.add_argument("--no-backend", action="store_true", help="Do not start local backend")
    parser.add_argument("--title", default="Jarvis Desktop")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=820)
    args = parser.parse_args()

    host = str(args.host or DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(args.port or DEFAULT_PORT)

    url = (args.url or "").strip() or f"http://{host}:{port}"
    if not _is_local_url(url):
        print("Desktop app is local-only. Use http://127.0.0.1:<port> or http://localhost:<port>.")
        return 2

    os.environ["JARVIS_CLOUD_MODE"] = "false"
    os.environ.setdefault("JARVIS_ENABLE_PC_AGENT", "true")
    os.environ.setdefault("JARVIS_ENABLE_SCHEDULER", "true")
    os.environ.setdefault("JARVIS_DESKTOP_API_URL", url)

    backend = BackendRunner(host=host, port=port)
    try:
        if not args.no_backend:
            backend.start()
            _wait_for_backend(url, timeout_s=20.0)

        _open_ui(url=url, title=str(args.title or "Jarvis Desktop"), width=max(900, int(args.width)), height=max(640, int(args.height)))
        return 0
    finally:
        backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
