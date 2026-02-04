import argparse
import inspect
import logging
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
import hashlib
import secrets

import requests

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = (Path(sys.executable).resolve().parent if IS_FROZEN else REPO_ROOT)

_APPDATA_BASE = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(APP_DIR)
APPDATA_DIR = (Path(_APPDATA_BASE) / "JarvisDesktop").resolve()
LOG_FILE = APPDATA_DIR / "jarvis_desktop.log"

JWT_SECRET_FILE = APPDATA_DIR / "jwt_secret.txt"


def _ensure_runtime_dirs() -> None:
    try:
        APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _get_or_create_jwt_secret() -> str:
    """Return a stable local JWT secret for desktop runs.

    Agent tokens are signed with JARVIS_JWT_SECRET. If missing, /api/agent/config
    will fail and PC agent cannot connect.
    """
    try:
        _ensure_runtime_dirs()
        if JWT_SECRET_FILE.exists():
            raw = JWT_SECRET_FILE.read_text(encoding="utf-8").strip()
            if raw:
                return raw
    except Exception:
        pass

    value = secrets.token_urlsafe(48)
    try:
        JWT_SECRET_FILE.write_text(value, encoding="utf-8")
    except Exception:
        pass
    return value


def _setup_logging() -> None:
    _ensure_runtime_dirs()
    try:
        root = logging.getLogger()
        root.setLevel(logging.INFO)

        # Replace handlers so repeated runs always log to the expected file.
        for h in list(root.handlers):
            try:
                root.removeHandler(h)
            except Exception:
                pass

        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

        file_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

        # Helpful when running from terminal.
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(fmt)
        root.addHandler(stream_handler)
    except Exception:
        # Logging must never prevent startup.
        pass


def _message_box(title: str, text: str) -> None:
    # Show something visible on Windows even when launched with no console.
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def _find_webview2_runtime_exe() -> Path | None:
    """Return msedgewebview2.exe if WebView2 Runtime appears installed."""

    if os.name != "nt":
        return None

    base_dirs: list[Path] = []
    for env_key in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        root = os.environ.get(env_key)
        if root:
            base_dirs.append(Path(root))

    candidates: list[Path] = []
    for base in base_dirs:
        candidates.append(base / "Microsoft" / "EdgeWebView" / "Application")

    for app_dir in candidates:
        try:
            if not app_dir.exists():
                continue

            direct = app_dir / "msedgewebview2.exe"
            if direct.exists():
                return direct

            for exe in app_dir.glob("**/msedgewebview2.exe"):
                if exe.exists():
                    return exe
        except Exception:
            continue

    return None


def _fatal(title: str, text: str, exc: Exception | None = None) -> None:
    try:
        _setup_logging()
        logging.error(text)
        if exc is not None:
            logging.error("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass

    # Always try to present the error to the user.
    try:
        _message_box(title, f"{text}\n\nLog: {LOG_FILE}")
    except Exception:
        pass

    raise SystemExit(text)


try:
    import webview  # type: ignore
except Exception as e:  # pragma: no cover
    _fatal(
        "Jarvis failed to start",
        "Missing dependency: pywebview. Install with: pip install -r scripts\\jarvis_desktop\\requirements.txt",
        e,
    )


DEFAULT_LOCAL_API = "http://127.0.0.1:18001"


def _is_local_api_url(api_url: str) -> bool:
    u = (api_url or "").strip().lower()
    return ("127.0.0.1" in u) or ("localhost" in u)


def _default_icon_path() -> Path | None:
    for candidate in [
        REPO_ROOT / "assets" / "jarvis.ico",
        REPO_ROOT / "jarvis-frontend" / "build" / "favicon.ico",
        REPO_ROOT / "jarvis-frontend" / "public" / "favicon.ico",
    ]:
        if candidate.exists():
            return candidate
    return None


def _create_windows_shortcut(shortcut_path: Path, target_exe: Path, args: str = "", icon: Path | None = None) -> None:
    """Create a Windows .lnk shortcut using WScript.Shell.

    This avoids adding extra Python deps. Works on Windows.
    """

    # Delay import: only valid on Windows.
    try:
        import win32com.client  # type: ignore
    except Exception as e:
        raise SystemExit(
            "Missing dependency: pywin32 is required to create a desktop shortcut.\n"
            "Install it (Windows): pip install pywin32\n"
            f"Import error: {e}"
        )

    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(str(shortcut_path))
    sc.Targetpath = str(target_exe)
    sc.Arguments = args or ""
    sc.WorkingDirectory = str(target_exe.parent)
    if icon and icon.exists():
        sc.IconLocation = str(icon)
    else:
        # Fall back to the exe icon.
        sc.IconLocation = str(target_exe)
    sc.save()


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class BackendRunner:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None
        self._server = None

    def start(self) -> None:
        try:
            import uvicorn  # type: ignore
        except Exception as e:
            raise SystemExit(
                "Missing dependency: uvicorn. Install backend requirements first.\n"
                f"Import error: {e}"
            )

        # Import the FastAPI app from the repo.
        # This import must succeed when packaged.
        try:
            from app import app as fastapi_app  # type: ignore
        except Exception as e:
            raise SystemExit(f"Failed to import backend app: {e}")

        # Desktop optimization:
        # - disable access logs (can be very chatty)
        # - default to warning-level logs
        log_level = (os.getenv("JARVIS_DESKTOP_UVICORN_LOG_LEVEL", "warning") or "warning").strip().lower()
        config = uvicorn.Config(
            fastapi_app,
            host=self.host,
            port=self.port,
            log_level=log_level,
            access_log=False,
            lifespan="on",
            workers=1,
        )
        server = uvicorn.Server(config)
        self._server = server

        def _run():
            # Runs until server.should_exit is set.
            server.run()

        t = threading.Thread(target=_run, name="jarvis-backend", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.should_exit = True
        except Exception:
            pass

        t = self._thread
        if t is not None:
            try:
                t.join(timeout=3.0)
            except Exception:
                pass


def _is_port_open(host: str, port: int, timeout_s: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:
        return False


def _pick_port(start: int = 18001, attempts: int = 10) -> int:
    for p in range(start, start + max(1, attempts)):
        if not _is_port_open("127.0.0.1", p):
            return p
    return start


def _wait_health(api_url: str, timeout_s: float = 20.0) -> bool:
    health = api_url.rstrip("/") + "/health"
    deadline = time.time() + max(1.0, timeout_s)
    while time.time() < deadline:
        try:
            r = requests.get(health, timeout=1.5)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


def main() -> int:
    _setup_logging()
    logging.info("Jarvis web shell starting (frozen=%s)", IS_FROZEN)

    # Preflight: pywebview on Windows requires WebView2 Runtime.
    if os.name == "nt":
        try:
            runtime_exe = _find_webview2_runtime_exe()
            if runtime_exe is None:
                raise SystemExit(
                    "WebView2 Runtime not found.\n"
                    "Install Microsoft Edge WebView2 Runtime, then try again."
                )
            logging.info("WebView2 runtime found: %s", runtime_exe)
        except SystemExit:
            raise
        except Exception as e:
            # Don't hard-fail on detection errors, but log them.
            logging.warning("WebView2 detection error: %s", e)

    logging.info("Building CLI parser")
    ap = argparse.ArgumentParser(description="Jarvis Desktop Web Shell (embedded web UI)")

    # IMPORTANT: Desktop builds must not inherit cloud/server URLs.
    # - JARVIS_SERVER_URL is reserved for the PC Agent (cloud -> device bridge)
    # - Desktop app always targets a local backend instance
    default_url = (os.getenv("JARVIS_DESKTOP_API_URL", "") or "").strip()
    if not default_url:
        default_url = DEFAULT_LOCAL_API

    ap.add_argument(
        "--url",
        default=default_url,
        help="Base API URL to open (desktop-only: http://127.0.0.1:<port> or http://localhost:<port>)",
    )

    # In the packaged desktop app, starting the backend locally is the default.
    ap.add_argument(
        "--start-backend",
        action="store_true",
        default=bool(IS_FROZEN),
        help="Start the local backend automatically (desktop default: on).",
    )
    ap.add_argument(
        "--enable-pc-agent",
        action="store_true",
        help="Enable PC agent features when starting the local backend (overrides --disable-pc-agent).",
    )
    ap.add_argument(
        "--disable-pc-agent",
        action="store_true",
        help="Disable PC agent features (useful for desktop low-priv mode).",
    )

    ap.add_argument(
        "--enable-scheduler",
        action="store_true",
        help="Enable background scheduler jobs (desktop default: disabled to reduce CPU).",
    )
    ap.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to use when --start-backend is set (0 = pick a free port near 18001).",
    )
    ap.add_argument(
        "--title",
        default="Jarvis",
        help="Window title",
    )

    ap.add_argument(
        "--mode",
        choices=["browser", "embedded"],
        default=(
            ("embedded" if IS_FROZEN else (os.getenv("JARVIS_DESKTOP_MODE", "browser") or "browser"))
        )
        .strip()
        .lower(),
        help="UI mode: 'embedded' is the desktop default; 'browser' is intended for dev only.",
    )

    ap.add_argument(
        "--icon",
        default=os.getenv("JARVIS_DESKTOP_ICON", "").strip(),
        help="Optional icon (.ico) for desktop shortcut creation.",
    )

    ap.add_argument(
        "--create-desktop-shortcut",
        action="store_true",
        help="Create a Desktop shortcut for this app and exit.",
    )
    ap.add_argument(
        "--shortcut-name",
        default="Jarvis",
        help="Shortcut name shown on Desktop.",
    )
    logging.info("Parsing CLI args: %s", sys.argv)
    args = ap.parse_args()
    logging.info("CLI args parsed")

    # Normalize mode
    args.mode = (getattr(args, "mode", "browser") or "browser").strip().lower()
    if IS_FROZEN:
        # Hard guarantee: the desktop EXE must show an embedded window.
        args.mode = "embedded"

    # Shortcut creation mode (useful for packaged .exe).
    if args.create_desktop_shortcut:
        if os.name != "nt":
            raise SystemExit("Desktop shortcut creation is only supported on Windows")

        exe = Path(sys.executable).resolve()
        # When running as script, sys.executable is python.exe (not directly runnable as an app).
        # Prefer a packaged EXE if present so the shortcut works on double-click.
        if not IS_FROZEN:
            try:
                packaged = (REPO_ROOT / "dist" / "Jarvis.exe").resolve()
                if packaged.exists():
                    exe = packaged
            except Exception:
                pass

        desktop = Path(os.path.join(os.path.expanduser("~"), "Desktop"))
        shortcut = desktop / f"{(args.shortcut_name or 'Jarvis').strip()}.lnk"

        # If the shortcut already exists, replace it.
        try:
            if shortcut.exists():
                shortcut.unlink()
        except Exception:
            pass

        # If a legacy Desktop\Jarvis.exe exists (user previously copied it), remove it only if
        # it is byte-identical to the current dist\Jarvis.exe. This avoids deleting unrelated files.
        try:
            legacy_exe = desktop / "Jarvis.exe"
            dist_exe = (REPO_ROOT / "dist" / "Jarvis.exe").resolve()
            if legacy_exe.exists() and dist_exe.exists():
                if _sha256_file(legacy_exe) == _sha256_file(dist_exe):
                    legacy_exe.unlink()
        except Exception:
            pass

        icon: Path | None = None
        if (args.icon or "").strip():
            try:
                p = Path((args.icon or "").strip())
                if p.exists():
                    icon = p
            except Exception:
                icon = None
        if icon is None:
            icon = _default_icon_path()

        # Desktop shortcut always targets local backend and embedded window.
        # PC agent features are enabled by default in frozen builds (see below).
        shortcut_args = "--start-backend --mode embedded"

        _create_windows_shortcut(shortcut, exe, args=shortcut_args, icon=icon)
        print(f"Created shortcut: {shortcut}")
        return 0

    api_url = (args.url or "").strip().rstrip("/")
    if not api_url.startswith("http://") and not api_url.startswith("https://"):
        api_url = "http://" + api_url

    # Desktop app is strictly local-only. This prevents accidental cloud usage
    # when a user has JARVIS_SERVER_URL set for the PC Agent.
    if not _is_local_api_url(api_url):
        raise SystemExit(
            "Jarvis Desktop is configured for local-only use.\n"
            f"Got: {api_url}\n\n"
            "Use a localhost URL (http://127.0.0.1:<port>), or clear any old desktop config."
        )

        logging.info(
        "Args: mode=%s start_backend=%s enable_pc_agent=%s url=%s",
        str(args.mode),
        bool(args.start_backend),
        bool(args.enable_pc_agent),
        api_url,
    )

    backend: BackendRunner | None = None

    try:
        if args.start_backend:
            # Only start when targeting localhost.
            if not _is_local_api_url(api_url):
                raise SystemExit("--start-backend is only supported for local URLs (127.0.0.1/localhost).")

            port = int(args.port) if int(args.port) > 0 else _pick_port(18001, 15)
            api_url = f"http://127.0.0.1:{port}"
            # Force local desktop runtime (do not inherit cloud env vars).
            os.environ["JARVIS_CLOUD_MODE"] = "false"
            os.environ["AUTH_USE_DB"] = "false"
            os.environ["JARVIS_PUBLIC_SERVER_URL"] = api_url

            if not (os.getenv("JARVIS_JWT_SECRET") or "").strip():
                os.environ["JARVIS_JWT_SECRET"] = _get_or_create_jwt_secret()

            # Desktop performance: avoid background maintenance threads unless explicitly enabled.
            os.environ.setdefault("JARVIS_ENABLE_SESSION_CLEANUP", "false")

            # Desktop performance: disable background scheduler unless explicitly enabled.
            if getattr(args, "enable_scheduler", False):
                os.environ.setdefault("JARVIS_ENABLE_SCHEDULER", "true")
            else:
                os.environ.setdefault("JARVIS_ENABLE_SCHEDULER", "false")
            # Desktop: enable PC agent by default in packaged builds so the UI can display
            # the local ws_url / token / shared secret for JarvisPCAgent.
            # - In dev (not frozen), keep it off by default.
            pc_agent_enabled = bool(IS_FROZEN)
            if getattr(args, "disable_pc_agent", False):
                pc_agent_enabled = False
            if getattr(args, "enable_pc_agent", False):
                pc_agent_enabled = True

            os.environ["JARVIS_ENABLE_PC_AGENT"] = "true" if pc_agent_enabled else "false"

            backend = BackendRunner("127.0.0.1", port)
            logging.info("Starting local backend on %s", api_url)
            backend.start()

        logging.info("Waiting for backend health: %s/health", api_url.rstrip("/"))

        # Double-click UX: if the user is targeting localhost and the backend isn't running,
        # automatically start it instead of exiting silently.
        if not _wait_health(api_url, timeout_s=6.0):
            is_local = _is_local_api_url(api_url)
            if is_local and not args.start_backend:
                logging.warning("Backend not reachable at %s; attempting auto-start", api_url)
                port = _pick_port(18001, 15)
                api_url = f"http://127.0.0.1:{port}"
                os.environ["JARVIS_CLOUD_MODE"] = "false"
                os.environ["AUTH_USE_DB"] = "false"
                if not (os.getenv("JARVIS_JWT_SECRET") or "").strip():
                    os.environ["JARVIS_JWT_SECRET"] = _get_or_create_jwt_secret()

                # Same default as above: enable in frozen builds, disable in dev.
                pc_agent_enabled = bool(IS_FROZEN)
                if getattr(args, "disable_pc_agent", False):
                    pc_agent_enabled = False
                if getattr(args, "enable_pc_agent", False):
                    pc_agent_enabled = True
                os.environ["JARVIS_ENABLE_PC_AGENT"] = "true" if pc_agent_enabled else "false"
                os.environ["JARVIS_PUBLIC_SERVER_URL"] = api_url
                os.environ["JARVIS_ENABLE_SCHEDULER"] = "false"
                os.environ["JARVIS_ENABLE_SESSION_CLEANUP"] = "false"
                backend = BackendRunner("127.0.0.1", port)
                logging.info("Auto-starting local backend on %s", api_url)
                backend.start()

            logging.info("Waiting for backend health (extended): %s/health", api_url.rstrip("/"))

            if not _wait_health(api_url, timeout_s=25.0):
                raise SystemExit(
                    "Backend is not reachable.\n"
                    f"Tried: {api_url}/health\n"
                    "Start the API (python run_local.py) or launch with --start-backend."
                )

        logging.info("Backend reachable: %s", api_url)

        # Load the UI from the backend root. In production/cloud, the backend serves the build.
        # In local dev, this will work when jarvis-frontend/build exists.
        ui_url = api_url + "/"

        logging.info("Launching UI: %s", ui_url)

        # Dev-only: open in the system browser.
        if (not IS_FROZEN) and (str(args.mode) == "browser"):
            try:
                webbrowser.open(ui_url)
                logging.info("Opened browser: %s", ui_url)
            except Exception as e:
                raise SystemExit(f"Failed to open browser: {e}")

            # If we started a local backend, keep this process alive so the backend
            # continues running; otherwise we'd exit immediately and shut it down.
            if backend is not None:
                logging.info("Local backend running; keeping Jarvis alive")
                try:
                    while True:
                        time.sleep(0.75)
                except KeyboardInterrupt:
                    logging.info("KeyboardInterrupt received; shutting down")
                    return 0

            return 0

        # Embedded mode (desktop default).
        try:
            icon = _default_icon_path()
            # pywebview compatibility: older versions don't accept the `icon` kwarg.
            window_kwargs = {
                "title": args.title,
                "url": ui_url,
                "width": 1100,
                "height": 760,
            }
            try:
                sig = inspect.signature(webview.create_window)
                if icon and ("icon" in sig.parameters):
                    window_kwargs["icon"] = str(icon)
            except Exception:
                # If we can't introspect, we'll try best-effort below.
                if icon:
                    window_kwargs["icon"] = str(icon)

            try:
                window = webview.create_window(**window_kwargs)
            except TypeError:
                # Retry without icon kwarg.
                window_kwargs.pop("icon", None)
                window = webview.create_window(**window_kwargs)
            logging.info("Webview window created; starting event loop")
            webview.start(gui="edgechromium")
            return 0
        except Exception as e:
            _fatal(
                "Jarvis failed to start",
                "Embedded UI failed to start. Ensure Microsoft Edge WebView2 Runtime is installed.",
                e,
            )
    except SystemExit as e:
        msg = str(e) if str(e) else "Jarvis exited."
        logging.error("SystemExit: %s", msg)
        _message_box("Jarvis failed to start", f"{msg}\n\nLog: {LOG_FILE}")
        raise
    except Exception as e:
        _fatal("Jarvis crashed", "Unexpected error while starting Jarvis.", e)
    finally:
        if backend is not None:
            backend.stop()


if __name__ == "__main__":
    raise SystemExit(main())
