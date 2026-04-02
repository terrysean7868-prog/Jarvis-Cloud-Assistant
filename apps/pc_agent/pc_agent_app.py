import argparse
import json
import base64
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
IS_FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", str(REPO_ROOT))).resolve()
APP_DIR = (Path(sys.executable).resolve().parent if IS_FROZEN else REPO_ROOT)

_APPDATA_BASE = str((Path.home() / "AppData" / "Local").resolve())
APPDATA_DIR = (Path(_APPDATA_BASE) / "JarvisPCAgent").resolve()

# All runtime-writable files go into %APPDATA%\JarvisPCAgent so the packaged exe
# doesn't try to write into its (often read-only) install directory.
DATA_DIR = APPDATA_DIR / "data"
CONFIG_FILE = APPDATA_DIR / "agent_desktop_config.json"
PERMISSIONS_FILE = APPDATA_DIR / "agent_permissions.json"
LOG_FILE = APPDATA_DIR / "jarvis_pc_agent.log"


def _ensure_runtime_dirs() -> None:
    try:
        APPDATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "tmp").mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "sessions").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _setup_logging() -> None:
    _ensure_runtime_dirs()
    try:
        logging.basicConfig(
            filename=str(LOG_FILE),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    except Exception:
        # Logging must never prevent startup.
        pass


def _message_box(title: str, text: str) -> None:
    # Always try to show something visible even when pywebview/tk isn't working.
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def _icon_path() -> Optional[str]:
    candidates = [
        BUNDLE_DIR / "assets" / "jarvis.ico",
        APP_DIR / "assets" / "jarvis.ico",
        REPO_ROOT / "frontend" / "public" / "favicon.ico",
    ]
    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except Exception:
            continue
    return None


def _load_pc_agent_ui_html() -> Optional[str]:
    candidates = [
        BUNDLE_DIR / "assets" / "pc_agent_ui.html",
        APP_DIR / "assets" / "pc_agent_ui.html",
        REPO_ROOT / "assets" / "pc_agent_ui.html",
    ]
    for p in candidates:
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:
            continue
    return None


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _copy_text_to_clipboard(text: str) -> bool:
    s = str(text or "")
    if not s:
        return False

    if os.name == "nt":
        try:
            import win32clipboard  # type: ignore

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(s, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            pass

    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(s)
        root.update_idletasks()
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _clean_cfg_str(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)

    s = value.strip()
    # Common user-paste / legacy forms: "https://..." or "\"https://...\""
    s = s.replace('\\"', '"').replace("\\'", "'").replace('\\/', '/')
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def _decode_jwt_payload_unverified(token: str) -> dict:
    raw = str(token or "").strip()
    if not raw or raw.count(".") < 2:
        return {}
    try:
        parts = raw.split(".")
        payload_part = parts[1]
        pad = "=" * (-len(payload_part) % 4)
        decoded = base64.urlsafe_b64decode((payload_part + pad).encode("utf-8"))
        obj = json.loads(decoded.decode("utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _sanitize_agent_token(token: str) -> tuple[str, str | None]:
    cleaned = _clean_cfg_str(token)
    if not cleaned:
        return "", None

    payload = _decode_jwt_payload_unverified(cleaned)
    token_typ = str((payload or {}).get("typ") or "").strip().lower()
    if token_typ and token_typ != "agent":
        return "", "Provided token appears to be a login/session token, not an agent token. Open Jarvis -> PC agent panel and copy agent_token from /api/agent/config."

    return cleaned, None


def _token_device_id(token: str) -> str:
    payload = _decode_jwt_payload_unverified(token)
    return _clean_cfg_str(payload.get("device_id") or payload.get("sub")).lower()


def _find_webview2_runtime_exe() -> Optional[Path]:
    """Return a path to msedgewebview2.exe if WebView2 Runtime appears installed."""

    base_dirs: list[Path] = [
        Path("C:/Program Files"),
        Path("C:/Program Files (x86)"),
        (Path.home() / "AppData" / "Local").resolve(),
    ]

    candidates: list[Path] = []
    for base in base_dirs:
        # System-wide install typically lives under Program Files; per-user installs can appear under LocalAppData.
        candidates.append(base / "Microsoft" / "EdgeWebView" / "Application")

    for app_dir in candidates:
        try:
            if not app_dir.exists():
                continue

            direct = app_dir / "msedgewebview2.exe"
            if direct.exists():
                return direct

            # Typical layout: Application/<version>/msedgewebview2.exe
            for exe in app_dir.glob("**/msedgewebview2.exe"):
                if exe.exists():
                    return exe
        except Exception:
            continue

    return None


def _discover_local_jarvis_server_url() -> Optional[str]:
    """Best-effort: find a local Jarvis backend started by Jarvis Desktop.

    Jarvis Desktop typically starts on 127.0.0.1:18001 (or the next free port).
    """
    candidates = [f"http://127.0.0.1:{p}" for p in range(18001, 18016)] + [f"http://localhost:{p}" for p in range(18001, 18016)]
    for base in candidates:
        try:
            url = f"{base.rstrip('/')}/health"
            req = urllib.request.Request(url=url, headers={"User-Agent": "JarvisPCAgent"}, method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if int(getattr(resp, "status", 0) or 0) == 200:
                    return base.rstrip("/")
        except Exception:
            continue
    return None


def load_config() -> dict:
    cfg = _load_json(CONFIG_FILE)
    if not cfg:
        cfg = {
            "server_url": "https://jarvis-cloud-assistant.onrender.com",
            "device_id": "primary",
            "agent_token": "",
            "shared_secret": "",
            "loop_mode": False,
            "auto_start": False,
            "window_width": 980,
            "window_height": 760,
        }

    # Sanitize user-edited values to avoid showing escaped quotes in UI.
    cfg["server_url"] = _clean_cfg_str(cfg.get("server_url"))
    cfg["device_id"] = _clean_cfg_str(cfg.get("device_id"))
    cfg["agent_token"] = _clean_cfg_str(cfg.get("agent_token"))
    cfg["shared_secret"] = _clean_cfg_str(cfg.get("shared_secret"))

    # If user hasn't configured the server yet (or it's still the cloud default),
    # try to auto-detect a local Jarvis Desktop backend.
    try:
        default_cloud = "https://jarvis-cloud-assistant.onrender.com"
        if (not cfg.get("server_url")) or (cfg.get("server_url") == default_cloud):
            local = _discover_local_jarvis_server_url()
            if local:
                cfg["server_url"] = local
                # In local shared-secret mode the server almost always targets device_id='primary'.
                # Defaulting to COMPUTERNAME leads to "connected but no actions" confusion.
                try:
                    raw_default_device = "primary"
                    default_device = _clean_cfg_str(raw_default_device)
                    current_device = _clean_cfg_str(cfg.get("device_id"))
                    if current_device and default_device and current_device.lower() == default_device.lower() and current_device.lower() != "primary":
                        cfg["device_id"] = "primary"
                except Exception:
                    pass
    except Exception:
        pass
    try:
        cfg["window_width"] = int(cfg.get("window_width") or 980)
        cfg["window_height"] = int(cfg.get("window_height") or 760)
    except Exception:
        cfg["window_width"] = 980
        cfg["window_height"] = 760
    return cfg


def save_config(cfg: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        # UI will show errors elsewhere.
        pass


def load_permissions() -> dict:
    perms = _load_json(PERMISSIONS_FILE)
    if not perms:
        perms = {
            "allow_app_control": False,
            "allow_execute_command": False,
            "allow_file_ops": False,
            "allow_screen": False,
            "allow_self_update": False,
        }
    return perms


def run_daemon(token: Optional[str], server_url: Optional[str], shared_secret: Optional[str] = None) -> int:
    # Import only in daemon mode so UI startup stays fast.
    from apps.pc_agent.pc_agent import run_agent  # type: ignore

    try:
        import asyncio

        asyncio.run(run_agent(agent_token=token, server_base_url=server_url, shared_secret=shared_secret))
        return 0
    except asyncio.CancelledError:
        return 0
    except KeyboardInterrupt:
        return 0


@dataclass
class AgentRunConfig:
    token: str
    shared_secret: str
    server_url: str
    device_id: str
    loop_mode: bool


class AgentSupervisor:
    def __init__(self, log_q: "queue.Queue[str]"):
        self._log_q = log_q
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._paused = False
        self._last_cfg: Optional[AgentRunConfig] = None

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def is_running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def start(self, cfg: AgentRunConfig) -> None:
        if self.is_running():
            self._log(f"[{_now_hms()}] Agent already running")
            return

        self._stop.clear()
        self._paused = False
        self._last_cfg = cfg

        def _runner() -> None:
            while not self._stop.is_set():
                cmd = [sys.executable, "--daemon"]
                if cfg.token.strip():
                    cmd += ["--token", cfg.token.strip()]
                if (not cfg.token.strip()) and cfg.shared_secret.strip():
                    cmd += ["--shared-secret", cfg.shared_secret.strip()]
                if cfg.server_url.strip():
                    cmd += ["--server", cfg.server_url.strip()]

                env = os.environ.copy()
                env.setdefault("PYTHONUNBUFFERED", "1")
                if cfg.server_url.strip():
                    env["JARVIS_SERVER_URL"] = cfg.server_url.strip()
                if cfg.device_id.strip():
                    env["JARVIS_DEVICE_ID"] = cfg.device_id.strip()

                # Packaged exe runs from a read-only install location; ensure agent
                # uses a stable, writable project root and permission file.
                env.setdefault("JARVIS_AGENT_PROJECT_ROOT", str(APPDATA_DIR))
                env.setdefault("JARVIS_AGENT_PERMISSIONS_FILE", str(PERMISSIONS_FILE))
                _ensure_runtime_dirs()

                self._log(f"[{_now_hms()}] Starting agent…")
                try:
                    self._proc = subprocess.Popen(
                        cmd,
                        cwd=str(APP_DIR),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                    )
                except Exception as e:
                    self._log(f"[{_now_hms()}] ERROR: Failed to start agent: {e}")
                    self._proc = None
                    return

                self._log(f"[{_now_hms()}] Agent started (PID: {self._proc.pid})")

                def _pump(stream, prefix: str) -> None:
                    try:
                        for line in stream:
                            if self._stop.is_set():
                                break
                            line = (line or "").rstrip()
                            if line:
                                self._log(f"[{_now_hms()}] {prefix}{line}")
                    except Exception:
                        pass

                if self._proc.stdout:
                    threading.Thread(target=_pump, args=(self._proc.stdout, ""), daemon=True).start()
                if self._proc.stderr:
                    threading.Thread(target=_pump, args=(self._proc.stderr, "ERR: "), daemon=True).start()

                # Wait for exit
                while not self._stop.is_set():
                    code = self._proc.poll()
                    if code is not None:
                        break
                    time.sleep(0.25)

                code = self._proc.poll()
                if self._stop.is_set():
                    self._log(f"[{_now_hms()}] Agent stopped")
                    return

                self._log(f"[{_now_hms()}] Agent exited (code {code})")
                self._proc = None

                if not cfg.loop_mode:
                    return

                if self._paused:
                    return

                self._log(f"[{_now_hms()}] Loop mode: restarting in 5s…")
                for _ in range(10):
                    if self._stop.is_set():
                        return
                    time.sleep(0.5)

        t = threading.Thread(target=_runner, name="pc-agent-supervisor", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if not p:
            return
        try:
            p.terminate()
        except Exception:
            pass

        deadline = time.time() + 5.0
        while time.time() < deadline:
            if p.poll() is not None:
                break
            time.sleep(0.1)

        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

        self._proc = None

    def pause(self) -> None:
        self._paused = True
        self.stop()
        self._log(f"[{_now_hms()}] Jobs paused")

    def resume(self) -> None:
        if self.is_running():
            return
        if self._last_cfg is None:
            self._log(f"[{_now_hms()}] Resume skipped: no previous config")
            return
        self._paused = False
        self.start(self._last_cfg)
        self._log(f"[{_now_hms()}] Jobs resumed")

    def cancel_jobs(self) -> None:
        self.stop()
        self._log(f"[{_now_hms()}] Active jobs cancelled")

    def paused(self) -> bool:
        return bool(self._paused)

    def _log(self, msg: str) -> None:
        try:
            self._log_q.put_nowait(msg)
        except Exception:
            pass


def run_ui() -> int:
    _setup_logging()
    logging.info("UI start (frozen=%s, app_dir=%s)", IS_FROZEN, APP_DIR)
    if True:
        cfg = load_config()

        BUILD_STAMP = "2026-02-04"

        log_q: "queue.Queue[str]" = queue.Queue()
        sup = AgentSupervisor(log_q)
        log_lines: list[str] = []
        log_seq = 0
        log_lock = threading.Lock()

        def _drain_logs() -> None:
            nonlocal log_lines
            nonlocal log_seq
            try:
                while True:
                    line = log_q.get_nowait()
                    with log_lock:
                        log_seq += 1
                        # Keep a monotonic sequence id so truncation never breaks the UI polling.
                        log_lines.append(f"{log_seq}|{line}")
                        if len(log_lines) > 2000:
                            log_lines = log_lines[-2000:]
            except queue.Empty:
                return

        def _current_run_cfg(cfg_dict: dict) -> AgentRunConfig:
                return AgentRunConfig(
                        token=str(cfg_dict.get("agent_token") or "").strip(),
                shared_secret=str(cfg_dict.get("shared_secret") or "").strip(),
                        server_url=str(cfg_dict.get("server_url") or "").strip(),
                        device_id=str(cfg_dict.get("device_id") or "").strip(),
                        loop_mode=bool(cfg_dict.get("loop_mode")),
                )

        def _save_cfg(cfg_dict: dict) -> None:
                cfg.update(
                        {
                                "server_url": _clean_cfg_str(cfg_dict.get("server_url")),
                                "device_id": _clean_cfg_str(cfg_dict.get("device_id")),
                                "agent_token": _clean_cfg_str(cfg_dict.get("agent_token")),
                    "shared_secret": _clean_cfg_str(cfg_dict.get("shared_secret")),
                                "loop_mode": bool(cfg_dict.get("loop_mode")),
                                "auto_start": bool(cfg_dict.get("auto_start")),
                        }
                )
                save_config(cfg)
                try:
                        log_q.put_nowait(f"[{_now_hms()}] Saved config")
                except Exception:
                        pass

        html = _load_pc_agent_ui_html()
        if not html:
            html = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta http-equiv="X-UA-Compatible" content="IE=edge" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Jarvis PC Agent</title>
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial;
            color: #eaf2ff;
            background: #0b1220;
        }
        .bg {
            min-height: 100vh;
            background:
                radial-gradient(1200px 900px at 20% 0%, rgba(0,194,255,0.18), transparent 60%),
                radial-gradient(1000px 800px at 90% 10%, rgba(47,224,134,0.10), transparent 55%),
                #0b1220;
        }
        .wrap { max-width: 1140px; margin: 0 auto; padding: 18px; }
        .topbar { display: -ms-flexbox; display: flex; -ms-flex-align: center; align-items: center; -ms-flex-pack: justify; justify-content: space-between; }
        .topbar > * { margin-right: 12px; }
        .topbar > *:last-child { margin-right: 0; }
        .title { font-size: 20px; font-weight: 700; letter-spacing: 0.2px; }
        .subtitle { font-size: 12px; color: rgba(234,242,255,0.72); margin-top: 2px; }
        .badge { padding: 6px 10px; border: 1px solid rgba(255,255,255,0.10); border-radius: 999px; background: rgba(255,255,255,0.04); font-size: 12px; }

        .cols { margin-top: 14px; display: -ms-flexbox; display: flex; -ms-flex-wrap: wrap; flex-wrap: wrap; }
        .col { -ms-flex: 1 1 520px; flex: 1 1 520px; min-width: 360px; }
        .col + .col { margin-left: 14px; }
        @media (max-width: 980px) { .col + .col { margin-left: 0; margin-top: 14px; } }

        .card { border: 1px solid rgba(255,255,255,0.10); background: rgba(255,255,255,0.06); border-radius: 14px; padding: 14px; }
        .card h3 { margin: 0 0 10px 0; font-size: 13px; text-transform: uppercase; letter-spacing: 0.12em; color: rgba(234,242,255,0.72); }
        .row { display: block; margin-top: 10px; }
        .row:first-child { margin-top: 0; }
        label { font-size: 12px; color: rgba(234,242,255,0.72); display: block; margin-bottom: 5px; }
        input[type=text], input[type=password] {
            width: 100%; padding: 10px 10px; border-radius: 10px;
            border: 1px solid rgba(255,255,255,0.12);
            background: rgba(0,0,0,0.20);
            color: #eaf2ff;
            outline: none;
        }
        input[type=text]:focus, input[type=password]:focus { border-color: rgba(0,194,255,0.45); box-shadow: 0 0 0 3px rgba(0,194,255,0.12); }
        .btn {
            padding: 10px 12px; border-radius: 10px; cursor: pointer;
            border: 1px solid rgba(255,255,255,0.12);
            background: rgba(0,0,0,0.18);
            color: #eaf2ff; font-weight: 600;
        }
        .btn.primary { background: linear-gradient(180deg, rgba(0,194,255,0.22), rgba(0,194,255,0.08)); border-color: rgba(0,194,255,0.35); }
        .btn.good { background: linear-gradient(180deg, rgba(47,224,134,0.22), rgba(47,224,134,0.08)); border-color: rgba(47,224,134,0.35); }
        .btn.bad { background: linear-gradient(180deg, rgba(255,90,122,0.22), rgba(255,90,122,0.08)); border-color: rgba(255,90,122,0.35); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .pill { padding: 6px 10px; border-radius: 999px; font-size: 12px; border: 1px solid rgba(255,255,255,0.12); background: rgba(0,0,0,0.14); }
        .pill.good { border-color: rgba(47,224,134,0.35); color: #b9ffd9; }
        .pill.bad { border-color: rgba(255,90,122,0.35); color: #ffd0da; }
        .muted { color: rgba(234,242,255,0.72); font-size: 12px; }
        .logs {
            height: 370px; overflow: auto; border-radius: 12px;
            border: 1px solid rgba(255,255,255,0.12);
            background: rgba(0,0,0,0.22); padding: 10px;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 12px; white-space: pre-wrap;
        }
        .kv { margin-top: 8px; }
        .kv-row { display: -ms-flexbox; display: flex; -ms-flex-align: center; align-items: center; margin-top: 6px; }
        .kv-row:first-child { margin-top: 0; }
        .k { width: 130px; min-width: 130px; color: rgba(234,242,255,0.72); font-size: 12px; }
        .v { font-size: 12px; }
        .toolbar { margin-top: 12px; display: -ms-flexbox; display: flex; -ms-flex-align: center; align-items: center; }
        .toolbar .btn { margin-right: 8px; }
        .toolbar .btn:last-child { margin-right: 0; }
        .toolbar .spacer { -ms-flex: 1 1 auto; flex: 1 1 auto; }
        .checkboxes label { display: inline-block; margin-right: 12px; }
        .notice { margin-top: 10px; padding: 10px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.10); background: rgba(0,0,0,0.18); }

        .toast {
            position: fixed;
            left: 50%;
            bottom: 18px;
            transform: translateX(-50%);
            max-width: 92vw;
            padding: 10px 14px;
            border-radius: 999px;
            border: 1px solid rgba(255,255,255,0.14);
            background: rgba(0,0,0,0.55);
            color: #eaf2ff;
            font-size: 13px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.35);
            display: none;
            z-index: 9999;
        }
        .toast.good { border-color: rgba(47,224,134,0.45); }
        .toast.bad { border-color: rgba(255,90,122,0.55); }
        .toast.warn { border-color: rgba(255,210,77,0.55); }

        /* No-JS interaction test helpers */
        #clickTest { position: absolute; left: -9999px; }
        #clickTestOn { display: none; }
        #clickTest:checked ~ #clickTestOff { display: none; }
        #clickTest:checked ~ #clickTestOn { display: inline-flex; }
        details summary { cursor: pointer; }
    </style>
</head>
<body>
    <div class="bg">
        <div class="wrap">
            <div class="topbar">
                <div>
                    <div class="title">Jarvis PC Agent</div>
                    <div class="subtitle">Companion agent for cloud/web remote actions</div>
                </div>
                <div class="badge" id="build">Desktop __BUILD_STAMP__</div>
            </div>

            <noscript>
                <div class="notice" style="margin-top:12px; border-color: rgba(255,90,122,0.35)">
                    <div style="font-weight:600; margin-bottom:4px">JavaScript is disabled</div>
                    <div class="muted">This UI requires JavaScript. If you're on a managed PC, a policy may be disabling JavaScript in WebView2. Try installing/updating Microsoft Edge WebView2 Runtime, or use the fallback UI.</div>
                </div>
            </noscript>

            <div class="notice" id="debugLine" style="margin-top:12px; display:block">
                <div style="font-weight:600; margin-bottom:4px">Debug</div>
                <div class="muted" id="debugText">JS not loaded yet…</div>
                <div style="margin-top:10px; display:flex; gap:8px; flex-wrap:wrap">
                    <button class="btn" id="selfTest">Self-test button</button>
                    <button class="btn" id="toggleDebug">Toggle debug</button>
                </div>

                <div style="margin-top:12px">
                    <div class="muted">No-JS interaction test (should work even if JS is broken):</div>
                    <div style="margin-top:8px; display:flex; gap:8px; align-items:center; flex-wrap:wrap">
                        <input id="clickTest" type="checkbox" />
                        <label for="clickTest" class="btn">Toggle indicator</label>
                        <span class="pill bad" id="clickTestOff">OFF</span>
                        <span class="pill good" id="clickTestOn">ON</span>
                        <input id="focusTest" type="text" placeholder="Type here (no JS needed)" style="max-width:280px" />
                    </div>
                    <details style="margin-top:10px">
                        <summary class="muted">Expand/collapse (no JS)</summary>
                        <div class="muted" style="margin-top:6px">If this opens, clicks are reaching the page.</div>
                    </details>
                </div>
            </div>

            <div class="cols">
                <div class="col">
                    <div class="card">
                        <h3>Configuration</h3>
                        <div class="row">
                            <label for="server">Server URL</label>
                            <input id="server" type="text" placeholder="https://jarvis-cloud-assistant.onrender.com" />
                        </div>
                        <div class="row">
                            <label for="device">Device ID</label>
                            <input id="device" type="text" placeholder="my-pc" />
                        </div>
                        <div class="row">
                            <label for="token">Agent Token</label>
                            <input id="token" type="password" placeholder="Paste agent token" />
                            <div class="muted" style="margin-top:6px">From Jarvis UI (Desktop/Web): PC agent panel → Copy Agent token.</div>
                        </div>
                        <div class="row">
                            <label for="secret">Shared Secret (optional)</label>
                            <input id="secret" type="password" placeholder="Paste shared secret (local desktop)" />
                            <div class="muted" style="margin-top:6px">For local Jarvis Desktop you can use Shared secret instead of token.</div>
                        </div>
                        <div class="row checkboxes">
                            <label><input id="loop" type="checkbox" /> Loop mode (auto-restart)</label>
                            <label><input id="auto" type="checkbox" /> Auto-start</label>
                        </div>
                        <div class="toolbar">
                            <button class="btn primary" id="save">Save</button>
                            <button class="btn good" id="start">Start</button>
                            <button class="btn bad" id="stop">Stop</button>
                            <span class="spacer"></span>
                            <button class="btn" id="refresh">Refresh</button>
                            <button class="btn" id="findLocal">Find Local Jarvis</button>
                        </div>
                        <div class="notice" id="engineNotice" style="display:none">
                            <div style="font-weight:600; margin-bottom:4px">Limited UI mode</div>
                            <div class="muted">Install Microsoft Edge WebView2 Runtime for the best Jarvis PC Agent UI.</div>
                        </div>
                    </div>
                </div>

                <div class="col">
                    <div class="card">
                        <h3>Status</h3>
                        <div class="toolbar" style="margin-top:0">
                            <div id="statusPill" class="pill bad">Not running</div>
                            <span class="spacer"></span>
                            <div class="muted" id="pid">PID: —</div>
                        </div>
                        <div class="kv" id="perms"></div>
                        <div class="muted" style="margin-top:12px">Permissions are set from the cloud UI per device.</div>
                    </div>
                </div>
            </div>

            <div class="card" style="margin-top:14px">
                <h3>Logs</h3>
                <div id="logs" class="logs"></div>
                <div class="toolbar">
                    <button class="btn" id="clear">Clear</button>
                            <button class="btn" id="copyLogs">Copy Logs</button>
                    <span class="spacer"></span>
                    <div class="muted" id="last">—</div>
                </div>
            </div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        (function () {
            function $(id) { return document.getElementById(id); }

            function trimStr(x) {
                var s = String(x == null ? '' : x);
                // Avoid backslash-s regex to keep the embedded HTML string safe and predictable.
                return s.replace(/^[ \t\r\n]+/, '').replace(/[ \t\r\n]+$/, '');
            }

            var logSeq = 0;
            var refreshInFlight = false;
            var pollTimer = null;
            var isReady = false;
            var toastTimer = null;
            var lastToastSig = '';

            function toast(msg, kind, ms) {
                try {
                    var el = $('toast');
                    if (!el) return;
                    el.className = 'toast ' + (kind || 'good');
                    el.textContent = String(msg || '');
                    el.style.display = 'block';
                    if (toastTimer) clearTimeout(toastTimer);
                    toastTimer = setTimeout(function () { el.style.display = 'none'; }, (ms || 2200));
                } catch (e) { }
            }

            function toastForRejectReason(reason) {
                var r = trimStr(reason || '');
                if (!r) return null;
                if (r === 'pc_agent_disabled') return { msg: 'PC agent disabled on server', kind: 'bad', ms: 3600 };
                if (r === 'invalid_shared_secret') return { msg: 'Invalid shared secret (copy it from Jarvis Desktop)', kind: 'bad', ms: 3600 };
                if (r === 'invalid_agent_token') return { msg: 'Invalid agent token (use the Agent token, not login JWT)', kind: 'bad', ms: 3600 };
                if (r === 'server_missing_jwt_secret') return { msg: 'Server missing JWT secret (restart Jarvis Desktop)', kind: 'bad', ms: 3600 };
                if (r === 'expected_auth') return { msg: 'Server expected auth message', kind: 'bad', ms: 3600 };
                if (r === 'invalid_json') return { msg: 'Invalid auth payload (bad JSON)', kind: 'bad', ms: 3600 };
                if (r === 'missing_device_id') return { msg: 'Missing device_id', kind: 'bad', ms: 3200 };
                if (r === 'device_not_authorized') return { msg: 'Device not authorized for this token', kind: 'bad', ms: 3600 };
                return { msg: 'Server rejected connection: ' + r, kind: 'bad', ms: 3600 };
            }

            function extractRejectReasonFromLine(line) {
                var s = String(line || '');
                var marker = 'Server rejected connection:';
                var idx = s.indexOf(marker);
                if (idx === -1) return '';
                var rest = s.slice(idx + marker.length);
                rest = trimStr(rest);
                // Strip trailing hint in parentheses.
                var hintIdx = rest.indexOf(' (Hint:');
                if (hintIdx !== -1) rest = trimStr(rest.slice(0, hintIdx));
                return rest;
            }

            function setStatus(running, pid) {
                var pill = $('statusPill');
                pill.textContent = running ? 'Running' : 'Not running';
                pill.className = 'pill ' + (running ? 'good' : 'bad');
                $('pid').textContent = 'PID: ' + (pid || '—');
                $('start').disabled = !!running;
                $('stop').disabled = !running;
            }

            function renderPerms(p) {
                var el = $('perms');
                el.innerHTML = '';
                var items = [
                    ['App control', !!(p && p.allow_app_control)],
                    ['Execute command', !!(p && p.allow_execute_command)],
                    ['File ops', !!(p && p.allow_file_ops)],
                    ['Screen', !!(p && p.allow_screen)],
                    ['Self-update', !!(p && p.allow_self_update)]
                ];
                for (var i = 0; i < items.length; i++) {
                    var k = items[i][0];
                    var v = items[i][1];
                    var row = document.createElement('div');
                    row.className = 'kv-row';
                    var kEl = document.createElement('div');
                    kEl.className = 'k';
                    kEl.textContent = k;
                    var vEl = document.createElement('div');
                    vEl.className = 'v';
                    vEl.textContent = v ? 'Enabled' : 'Disabled';
                    vEl.style.color = v ? '#b9ffd9' : '#ffd0da';
                    row.appendChild(kEl);
                    row.appendChild(vEl);
                    el.appendChild(row);
                }
            }

            function loadConfig() {
                return window.pywebview.api.load_config().then(function (cfg) {
                    $('server').value = (cfg && cfg.server_url) ? cfg.server_url : '';
                    $('device').value = (cfg && cfg.device_id) ? cfg.device_id : '';
                    $('token').value = (cfg && cfg.agent_token) ? cfg.agent_token : '';
                    $('secret').value = (cfg && cfg.shared_secret) ? cfg.shared_secret : '';
                    $('loop').checked = !!(cfg && cfg.loop_mode);
                    $('auto').checked = !!(cfg && cfg.auto_start);
                    return cfg;
                });
            }

            function currentCfg() {
                return {
                    server_url: $('server').value,
                    device_id: $('device').value,
                    agent_token: $('token').value,
                    shared_secret: $('secret').value,
                    loop_mode: $('loop').checked,
                    auto_start: $('auto').checked
                };
            }

            function parseLogLine(raw) {
                var s = String(raw || '');
                var sep = s.indexOf('|');
                if (sep === -1) return { seq: null, line: s };
                var seq = Number(s.slice(0, sep));
                var line = s.slice(sep + 1);
                if (!isFinite(seq)) seq = null;
                return { seq: seq, line: line };
            }

            function refresh() {
                if (!window.pywebview || !window.pywebview.api) {
                    return Promise.reject(new Error('pywebview api not ready'));
                }

                var api = window.pywebview.api;
                return Promise.all([
                    api.get_status(),
                    api.get_permissions(),
                    api.get_logs(logSeq)
                ]).then(function (res) {
                    var st = res[0];
                    var perms = res[1];
                    var logs = res[2];

                    setStatus(!!(st && st.running), st ? st.pid : null);
                    renderPerms(perms || {});

                    if (logs && logs.lines && logs.lines.length) {
                        var box = $('logs');
                        for (var i = 0; i < logs.lines.length; i++) {
                            var parsed = parseLogLine(logs.lines[i]);
                            var line = parsed.line;
                            box.textContent += line + '\n';

                            // One-shot toasts for key events.
                            var s = String(line || '');
                            var sig = s.slice(-140);
                            if (sig && sig !== lastToastSig) {
                                if (s.indexOf('Connected:') !== -1) {
                                    lastToastSig = sig;
                                    toast('Agent connected', 'good');
                                } else if (s.indexOf('Server rejected connection') !== -1) {
                                    lastToastSig = sig;
                                    var reason = extractRejectReasonFromLine(s);
                                    var t = toastForRejectReason(reason);
                                    if (t) toast(t.msg, t.kind, t.ms);
                                    else toast('Server rejected connection (check token/secret)', 'bad', 3600);
                                } else if (s.indexOf('Missing agent auth') !== -1) {
                                    lastToastSig = sig;
                                    toast('Missing token/secret', 'bad', 3200);
                                }
                            }
                        }

                        if (typeof logs.next_seq === 'number') {
                            logSeq = logs.next_seq;
                        } else if (typeof logs.next_index === 'number') {
                            // Back-compat
                            logSeq = logs.next_index;
                        }
                        box.scrollTop = box.scrollHeight;
                        $('last').textContent = 'Updated: ' + new Date().toLocaleTimeString();
                    }
                });
            }

            function safeRefresh(showFailureToast) {
                if (!isReady) return Promise.resolve();
                if (refreshInFlight) return Promise.resolve();
                refreshInFlight = true;
                return refresh().catch(function () {
                    if (showFailureToast) toast('Refresh failed', 'bad');
                }).then(function () {
                    refreshInFlight = false;
                });
            }

            function startPolling() {
                try { if (pollTimer) clearTimeout(pollTimer); } catch (e) { }
                function tick() {
                    safeRefresh(false).then(function () {
                        pollTimer = setTimeout(tick, 1400);
                    });
                }
                pollTimer = setTimeout(tick, 900);
            }

            function ensureReady() {
                if (!isReady) {
                    toast('UI not ready yet', 'warn', 1600);
                    return false;
                }
                if (typeof Promise === 'undefined') {
                    toast('This UI needs WebView2 (Promise missing)', 'bad', 3600);
                    return false;
                }
                return true;
            }

            function setDebugVisible(visible) {
                try {
                    var line = $('debugLine');
                    if (!line) return;
                    line.style.display = visible ? 'block' : 'none';
                } catch (e) { }
            }

            function setDebugText(text) {
                try {
                    var el = $('debugText');
                    if (!el) return;
                    el.textContent = String(text || '');
                } catch (e) { }
            }

            function bootDebug() {
                // Keep debug visible so we can diagnose JS/input issues.
                setDebugVisible(true);

                try {
                    var ua = (navigator && navigator.userAgent) ? navigator.userAgent : '';
                    var p = (typeof Promise !== 'undefined');
                    setDebugText('JS loaded. Promise=' + (p ? 'yes' : 'no') + ' | UA=' + ua);
                } catch (e) {
                    setDebugText('JS loaded. (Failed to read UA)');
                }

                // Capture JS runtime errors (syntax errors may still prevent this from running).
                try {
                    window.onerror = function (msg, url, line, col) {
                        try {
                            setDebugVisible(true);
                            setDebugText('JS error: ' + msg + ' @' + (line || '?') + ':' + (col || '?'));
                            toast('JS error (see Debug)', 'bad', 5000);
                        } catch (e2) { }
                        return false;
                    };
                } catch (e) { }

                // Basic click tracer.
                try {
                    document.onclick = function (ev) {
                        try {
                            var t = ev && ev.target ? ev.target : null;
                            var id = t && t.id ? t.id : '';
                            var tag = t && t.tagName ? t.tagName : '';
                            if (id) setDebugText('Click: #' + id + ' (' + tag + ')');
                        } catch (e2) { }
                    };
                } catch (e) { }
            }

            function bindButtons() {
                $('save').onclick = function () {
                    if (!ensureReady()) return;
                    toast('Saving…', 'warn', 1200);
                    window.pywebview.api.save_config(currentCfg()).then(function () {
                        toast('Saved', 'good');
                    }).catch(function () {
                        toast('Save failed', 'bad');
                    });
                };

                $('start').onclick = function () {
                    if (!ensureReady()) return;
                    var cfg = currentCfg();
                    var server = trimStr(cfg.server_url || '');
                    var token = trimStr(cfg.agent_token || '');
                    var secret = trimStr(cfg.shared_secret || '');
                    var device = trimStr(cfg.device_id || '').toLowerCase();
                    if (!server) { toast('Server URL is required', 'warn'); return; }
                    if (!token && !secret) { toast('Paste Agent token or Shared secret', 'warn', 3200); return; }
                    if (!token && secret && device && device !== 'primary') {
                        toast('Tip: for local Shared Secret mode set Device ID to "primary"', 'warn', 3600);
                    }
                    toast('Starting agent…', 'warn', 1600);
                    window.pywebview.api.start_agent(cfg).then(function () {
                        toast('Starting agent…', 'good');
                    }).catch(function () {
                        toast('Start failed', 'bad');
                    });
                };

                $('stop').onclick = function () {
                    if (!ensureReady()) return;
                    toast('Stopping…', 'warn', 1600);
                    window.pywebview.api.stop_agent().then(function () {
                        toast('Stopping…', 'warn');
                    }).catch(function () {
                        toast('Stop failed', 'bad');
                    });
                };

                $('refresh').onclick = function () {
                    if (!ensureReady()) return;
                    safeRefresh(true).then(function () {
                        toast('Refreshed', 'good', 1200);
                    });
                };

                $('clear').onclick = function () {
                    if (!ensureReady()) return;
                    try { $('logs').textContent = ''; } catch (e) { }
                    logSeq = 0;
                    window.pywebview.api.clear_logs().then(function () {
                        toast('Cleared logs', 'good', 1200);
                    }).catch(function () {
                        toast('Clear failed', 'bad');
                    });
                };

                $('copyLogs').onclick = function () {
                    if (!ensureReady()) return;
                    toast('Copying logs…', 'warn', 1200);
                    window.pywebview.api.copy_logs().then(function (res) {
                        if (res && res.ok) {
                            toast('Logs copied', 'good', 1400);
                        } else {
                            toast('No logs to copy', 'bad', 1600);
                        }
                    }).catch(function () {
                        toast('Copy failed', 'bad');
                    });
                };

                $('findLocal').onclick = function () {
                    if (!ensureReady()) return;
                    toast('Searching local Jarvis…', 'warn', 1600);
                    window.pywebview.api.discover_local().then(function (r) {
                        var url = (r && r.server_url) ? String(r.server_url) : '';
                        if (url) {
                            $('server').value = url;
                            toast('Found local Jarvis: ' + url, 'good', 2800);
                        } else {
                            toast('Local Jarvis not found (start Jarvis Desktop first)', 'warn', 3200);
                        }
                    }).catch(function () {
                        toast('Local discovery failed', 'bad');
                    });
                };

                // Always-works self-test (no pywebview API needed).
                var st = $('selfTest');
                if (st) {
                    st.onclick = function () {
                        try {
                            st.textContent = 'Self-test OK @ ' + new Date().toLocaleTimeString();
                            toast('Click captured', 'good', 1200);
                        } catch (e) { }
                    };
                }

                var td = $('toggleDebug');
                if (td) {
                    td.onclick = function () {
                        try {
                            var line = $('debugLine');
                            var shown = !!(line && line.style && line.style.display !== 'none');
                            setDebugVisible(!shown);
                        } catch (e) { }
                    };
                }
            }

            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', function () {
                    bindButtons();
                    bootDebug();
                });
            } else {
                bindButtons();
                bootDebug();
            }

            window.addEventListener('pywebviewready', function () {
                try { setDebugText('pywebviewready fired (bridge available)'); } catch (e) { }
                // If we're running on the legacy MSHTML engine, advanced layout may be limited.
                try {
                    var ua = (navigator && navigator.userAgent) ? navigator.userAgent : '';
                    if (ua.indexOf('Trident/') !== -1 || ua.indexOf('MSIE ') !== -1) {
                        $('engineNotice').style.display = 'block';
                        $('build').textContent = 'Legacy';
                    }
                } catch (e) { }

                isReady = true;
                loadConfig().then(function () {
                    return safeRefresh(true);
                }).then(function () {
                    startPolling();
                    var cfg = currentCfg();
                    if (cfg.auto_start) {
                        return window.pywebview.api.start_agent(cfg);
                    }
                }).catch(function () { });
            });
        })();
    </script>
</body>
</html>
        """

        # Production UI: do not show build stamps in the window UI.
        try:
            html = html.replace("__BUILD_STAMP__", "")
        except Exception:
            pass

        class PcAgentWebApi:
            def load_config(self) -> dict:
                return load_config()

            def discover_local(self) -> dict:
                url = _discover_local_jarvis_server_url()
                return {"server_url": url or ""}

            def save_config(self, cfg_dict: dict) -> dict:
                _save_cfg(cfg_dict or {})
                return {"ok": True}

            def start_agent(self, cfg_dict: dict) -> dict:
                cfg_dict = cfg_dict or {}
                safe_token, warn = _sanitize_agent_token(str(cfg_dict.get("agent_token") or ""))
                cfg_dict["agent_token"] = safe_token
                if warn:
                    try:
                        log_q.put_nowait(f"[{_now_hms()}] WARNING: {warn}")
                    except Exception:
                        pass

                requested_device = _clean_cfg_str(cfg_dict.get("device_id")).lower()
                token_device = _token_device_id(safe_token) if safe_token else ""
                if safe_token and requested_device and token_device and token_device != requested_device:
                    try:
                        log_q.put_nowait(
                            f"[{_now_hms()}] WARNING: agent_token is bound to device_id='{token_device}', but the selected device_id is '{requested_device}'. Clear the token and reconfigure the PC agent config for this device."
                        )
                    except Exception:
                        pass
                    cfg_dict["agent_token"] = ""
                    safe_token = ""

                if not safe_token and not _clean_cfg_str(cfg_dict.get("shared_secret")):
                    return {"ok": False, "error": "missing_agent_auth", "message": "Provide agent_token (recommended) or shared_secret."}

                _save_cfg(cfg_dict)
                sup.start(_current_run_cfg(cfg_dict))
                return {"ok": True}

            def stop_agent(self) -> dict:
                sup.stop()
                return {"ok": True}

            def get_status(self) -> dict:
                return {
                    "running": sup.is_running(),
                    "pid": sup.pid,
                    "paused": sup.paused(),
                }

            def get_permissions(self) -> dict:
                return load_permissions()

            def clear_logs(self) -> dict:
                nonlocal log_lines
                nonlocal log_seq
                with log_lock:
                    log_lines = []
                    log_seq = 0
                _drain_logs()
                return {"ok": True}

            def pause_jobs(self) -> dict:
                sup.pause()
                return {"ok": True, "paused": True}

            def resume_jobs(self) -> dict:
                sup.resume()
                return {"ok": True, "paused": False}

            def cancel_jobs(self) -> dict:
                sup.cancel_jobs()
                return {"ok": True}

            def get_active_jobs(self) -> dict:
                running = sup.is_running()
                return {
                    "jobs": [
                        {
                            "job_id": f"pid-{sup.pid}",
                            "status": "running" if running else ("paused" if sup.paused() else "idle"),
                            "pid": sup.pid,
                        }
                    ] if (running or sup.paused()) else [],
                    "count": 1 if (running or sup.paused()) else 0,
                }

            def get_executed_commands(self, limit: int = 100) -> dict:
                _drain_logs()
                try:
                    n = max(1, min(int(limit or 100), 500))
                except Exception:
                    n = 100

                lines: list[str] = []
                with log_lock:
                    for raw in log_lines[-2000:]:
                        s = str(raw)
                        sep = s.find("|")
                        if sep != -1:
                            s = s[sep + 1:]
                        low = s.lower()
                        if "execute_command" in low or "command" in low or "running:" in low or "dispatch" in low:
                            lines.append(s)
                return {"commands": lines[-n:], "count": len(lines[-n:])}

            def get_logs(self, since_index: int = 0) -> dict:
                _drain_logs()
                try:
                    idx = int(since_index or 0)
                except Exception:
                    idx = 0
                with log_lock:
                    # log_lines entries are stored as: "<seq>|<line>"
                    lines = []
                    for raw in log_lines:
                        try:
                            s = str(raw)
                            sep = s.find("|")
                            if sep == -1:
                                # Legacy/unknown format: treat as always-new.
                                lines.append(s)
                                continue
                            seq = int(s[:sep])
                            if seq > idx:
                                lines.append(s)
                        except Exception:
                            continue
                    next_seq = int(log_seq)
                return {"lines": lines, "next_seq": next_seq}

            def copy_logs(self) -> dict:
                _drain_logs()
                with log_lock:
                    plain = []
                    for raw in log_lines:
                        try:
                            s = str(raw)
                            sep = s.find("|")
                            plain.append(s[sep + 1:] if sep != -1 else s)
                        except Exception:
                            continue
                    text = "\n".join(plain).strip()

                if not text:
                    return {"ok": False, "error": "empty"}
                ok = _copy_text_to_clipboard(text)
                return {"ok": bool(ok), "copied_chars": len(text) if ok else 0}

        # Preferred UI: embedded HTML via pywebview (matches desktop/cloud look better).
        try:
            import webview  # type: ignore

            debug_webview = False

            if debug_webview:
                try:
                    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = True
                    if not webview.settings.get("REMOTE_DEBUGGING_PORT"):
                        webview.settings["REMOTE_DEBUGGING_PORT"] = 9222
                except Exception:
                    pass

            # NOTE: pywebview 6.x does not accept an 'icon' kwarg in create_window.
            # The executable's icon (set via PyInstaller) controls the Explorer thumbnail.

            api = PcAgentWebApi()
            webview.create_window(
                "Jarvis PC Agent",
                html=html,
                js_api=api,
                width=int(cfg.get("window_width", 980)),
                height=int(cfg.get("window_height", 760)),
            )

            runtime_exe = _find_webview2_runtime_exe()

            # If we can locate the WebView2 runtime, tell pywebview explicitly.
            # This reduces the chance of silently falling back to MSHTML on some setups.
            try:
                if runtime_exe:
                    webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(runtime_exe.parent)
            except Exception:
                pass

            def _post_start_diagnostics() -> None:
                """Runs after the window is created to detect if JavaScript is blocked."""

                try:
                    win = webview.windows[0] if getattr(webview, "windows", None) else None
                except Exception:
                    win = None

                # Keep the title clean (no build stamp / backend suffix).

                if not win:
                    return

                # Avoid intrusive popups in production. This probe is useful for diagnosis,
                # but can false-positive during early startup on some machines.
                if not debug_webview:
                    return

                last_err = ""
                # Wait for the document to become interactive/complete before probing arithmetic.
                for _ in range(12):
                    try:
                        state = str(win.evaluate_js("document.readyState") or "")
                        if state in ("interactive", "complete"):
                            break
                    except Exception as e:
                        last_err = str(e)
                    time.sleep(0.35)

                ok = False
                for _ in range(6):
                    try:
                        v = win.evaluate_js("1+1")
                        ok = str(v).strip() in ("2", "2.0")
                        if ok:
                            break
                    except Exception as e:
                        last_err = str(e)
                    time.sleep(0.35)

                if ok:
                    return

                try:
                    err_tail = (last_err or "").strip()
                    extra = f"\n\nDetails: {err_tail}" if err_tail else ""
                    _message_box(
                        "Jarvis PC Agent",
                        "The embedded UI loaded, but JavaScript is not running (or is blocked).\n\n"
                        "This usually means either:\n"
                        "- WebView2 JavaScript is disabled by policy (managed PC), or\n"
                        "- The app is falling back to legacy MSHTML and Active Scripting is disabled.\n\n"
                        "Fix options:\n"
                        "1) Install/update Microsoft Edge WebView2 Runtime, then reopen.\n"
                        "2) If on a managed PC, ask IT to enable JavaScript for WebView2.\n"
                        "3) As a workaround, run: JarvisPCAgent.exe --fallback-ui\n"
                        + extra,
                    )
                except Exception:
                    pass

            # Backend selection:
            # - "auto" (default): lets pywebview pick a working backend (opens reliably)
            # - "edgechromium": best UI, requires WebView2 Runtime
            # - "mshtml": legacy fallback
            backend = "auto"

            # NOTE: The HTML UI uses modern JS (async/await, const/let, arrow funcs).
            # The legacy MSHTML engine will load but the JS will fail silently, making buttons appear dead.
            # So: use EdgeChromium when WebView2 exists; otherwise force the functional Tk fallback UI.
            if backend in ("tk", "fallback", "fallback-ui"):
                raise RuntimeError("Forced Tk fallback UI")

            if backend in ("edgechromium", "edge", "webview2"):
                try:
                    webview.start(gui="edgechromium", debug=debug_webview, func=_post_start_diagnostics)
                except Exception as start_err:
                    if not runtime_exe:
                        raise RuntimeError(
                            "Microsoft Edge WebView2 Runtime not found. Install it to enable the modern UI."
                        ) from start_err
                    raise
            elif backend in ("auto", ""):
                try:
                    # Always try EdgeChromium first; avoids false negatives from runtime detection.
                    webview.start(gui="edgechromium", debug=debug_webview, func=_post_start_diagnostics)
                except Exception as start_err:
                    if not runtime_exe:
                        raise RuntimeError(
                            "Microsoft Edge WebView2 Runtime not found. Install it (recommended), or run with --fallback-ui."
                        ) from start_err
                    raise
            elif backend in ("mshtml", "ie"):
                raise RuntimeError(
                    "Legacy MSHTML UI is not supported (buttons will not work). Install WebView2 or use --fallback-ui."
                )
            else:
                # Unknown backend value; prefer the default auto behavior.
                try:
                    webview.start(gui="edgechromium", debug=debug_webview, func=_post_start_diagnostics)
                except Exception as start_err:
                    if not runtime_exe:
                        raise RuntimeError(
                            "Microsoft Edge WebView2 Runtime not found. Install it (recommended), or run with --fallback-ui."
                        ) from start_err
                    raise

            sup.stop()
            return 0
        except Exception as e:
            # Fallback: minimal Tk UI and a clear installation prompt.
            import tkinter as tk
            from tkinter import messagebox, ttk

            logging.exception("Web UI unavailable; using fallback UI")

            log_q.put_nowait(f"[{_now_hms()}] Web UI unavailable ({e}); using fallback UI")

            try:
                if "webview2" in str(e).lower() or "edge" in str(e).lower():
                    msg = (
                        "Jarvis PC Agent UI needs Microsoft Edge WebView2 Runtime for the modern UI.\n\n"
                        "Click OK to open the WebView2 Runtime download page, then reopen JarvisPCAgent.exe."
                    )
                    messagebox.showinfo("Jarvis PC Agent", msg)
                    webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/")
            except Exception:
                pass

            root = tk.Tk()
            root.title("Jarvis PC Agent (Fallback UI)")
            root.geometry(
                f"{int(cfg.get('window_width', 980))}x{int(cfg.get('window_height', 760))}"
            )

            server_var = tk.StringVar(value=str(cfg.get("server_url", "")))
            device_var = tk.StringVar(value=str(cfg.get("device_id", "")))
            token_var = tk.StringVar(value=str(cfg.get("agent_token", "")))
            secret_var = tk.StringVar(value=str(cfg.get("shared_secret", "")))
            loop_var = tk.BooleanVar(value=bool(cfg.get("loop_mode", False)))
            auto_var = tk.BooleanVar(value=bool(cfg.get("auto_start", False)))
            status_var = tk.StringVar(value="Not running")
            pid_var = tk.StringVar(value="—")
            toast_var = tk.StringVar(value="")

            main = ttk.Frame(root, padding=10)
            main.pack(fill=tk.BOTH, expand=True)
            ttk.Label(main, text="Jarvis PC Agent", font=("Segoe UI", 16, "bold")).pack(anchor="w")

            status_frame = ttk.Labelframe(main, text="Status", padding=10)
            status_frame.pack(fill=tk.X, pady=(10, 10))
            ttk.Label(status_frame, text="Status:").grid(row=0, column=0, sticky="w")
            ttk.Label(status_frame, textvariable=status_var).grid(
                row=0, column=1, sticky="w", padx=(8, 0)
            )
            ttk.Label(status_frame, text="PID:").grid(row=0, column=2, sticky="w", padx=(20, 0))
            ttk.Label(status_frame, textvariable=pid_var).grid(row=0, column=3, sticky="w", padx=(8, 0))

            cfg_frame = ttk.Labelframe(main, text="Configuration", padding=10)
            cfg_frame.pack(fill=tk.X)
            ttk.Label(cfg_frame, text="Server URL:").grid(row=0, column=0, sticky="w")
            ttk.Entry(cfg_frame, textvariable=server_var, width=70).grid(
                row=0, column=1, sticky="we", padx=(8, 0)
            )
            ttk.Label(cfg_frame, text="Device ID:").grid(row=1, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(cfg_frame, textvariable=device_var, width=70).grid(
                row=1, column=1, sticky="we", padx=(8, 0), pady=(8, 0)
            )
            ttk.Label(cfg_frame, text="Agent Token:").grid(row=2, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(cfg_frame, textvariable=token_var, width=70, show="•").grid(
                row=2, column=1, sticky="we", padx=(8, 0), pady=(8, 0)
            )
            cfg_frame.columnconfigure(1, weight=1)

            ttk.Label(cfg_frame, text="Shared Secret (optional):").grid(row=3, column=0, sticky="w", pady=(8, 0))
            ttk.Entry(cfg_frame, textvariable=secret_var, width=70, show="•").grid(
                row=3, column=1, sticky="we", padx=(8, 0), pady=(8, 0)
            )

            opts = ttk.Frame(cfg_frame)
            opts.grid(row=4, column=1, sticky="w", pady=(8, 0))
            ttk.Checkbutton(opts, text="Loop mode", variable=loop_var).pack(side=tk.LEFT)
            ttk.Checkbutton(opts, text="Auto-start", variable=auto_var).pack(side=tk.LEFT, padx=(12, 0))

            btns = ttk.Frame(main)
            btns.pack(fill=tk.X, pady=(10, 10))

            toast_label = ttk.Label(main, textvariable=toast_var)
            toast_label.pack(anchor="w")

            _toast_after_id = {"id": None}

            def _toast(msg: str) -> None:
                try:
                    toast_var.set(str(msg or ""))
                    if _toast_after_id["id"] is not None:
                        root.after_cancel(_toast_after_id["id"])
                    _toast_after_id["id"] = root.after(2200, lambda: toast_var.set(""))
                except Exception:
                    pass

            def _save() -> None:
                _save_cfg(
                    {
                        "server_url": server_var.get(),
                        "device_id": device_var.get(),
                        "agent_token": token_var.get(),
                        "shared_secret": secret_var.get(),
                        "loop_mode": bool(loop_var.get()),
                        "auto_start": bool(auto_var.get()),
                    }
                )
                _toast("Saved")

            def _start() -> None:
                raw_token = token_var.get() or ""
                safe_token, warn = _sanitize_agent_token(raw_token)
                if warn:
                    try:
                        log_q.put_nowait(f"[{_now_hms()}] WARNING: {warn}")
                    except Exception:
                        pass
                    token_var.set("")

                requested_device = _clean_cfg_str(device_var.get()).lower()
                token_device = _token_device_id(safe_token) if safe_token else ""
                if safe_token and requested_device and token_device and token_device != requested_device:
                    msg = (
                        f"[{_now_hms()}] WARNING: agent_token is bound to device_id='{token_device}', "
                        f"but the selected device_id is '{requested_device}'. Clear the token and reconfigure the PC agent config for this device."
                    )
                    try:
                        log_q.put_nowait(msg)
                    except Exception:
                        pass
                    token_var.set("")
                    safe_token = ""

                if (not safe_token) and (not _clean_cfg_str(secret_var.get())):
                    _toast("Provide Agent token or Shared secret")
                    return

                _save_cfg(
                    {
                        "server_url": server_var.get(),
                        "device_id": device_var.get(),
                        "agent_token": safe_token,
                        "shared_secret": secret_var.get(),
                        "loop_mode": bool(loop_var.get()),
                        "auto_start": bool(auto_var.get()),
                    }
                )
                sup.start(
                    AgentRunConfig(
                        token=safe_token,
                        shared_secret=secret_var.get() or "",
                        server_url=server_var.get() or "",
                        device_id=device_var.get() or "",
                        loop_mode=bool(loop_var.get()),
                    )
                )
                _toast("Starting agent…")

            def _stop() -> None:
                sup.stop()
                _toast("Stopping…")

            def _refresh() -> None:
                status_var.set("Running" if sup.is_running() else "Not running")
                pid_var.set(str(sup.pid) if sup.pid else "—")
                _toast("Refreshed")

            def _find_local() -> None:
                url = _discover_local_jarvis_server_url()
                if url:
                    server_var.set(url)
                    _toast(f"Found local Jarvis: {url}")
                else:
                    _toast("Local Jarvis not found")

            ttk.Button(btns, text="Save", command=_save).pack(side=tk.LEFT)
            ttk.Button(btns, text="Start", command=_start).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(btns, text="Stop", command=_stop).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(btns, text="Refresh", command=_refresh).pack(side=tk.LEFT, padx=(8, 0))
            ttk.Button(btns, text="Find Local Jarvis", command=_find_local).pack(side=tk.LEFT, padx=(8, 0))

            def _tick() -> None:
                status_var.set("Running" if sup.is_running() else "Not running")
                pid_var.set(str(sup.pid) if sup.pid else "—")
                root.after(700, _tick)

            root.protocol("WM_DELETE_WINDOW", lambda: (sup.stop(), root.destroy()))
            _tick()
            if auto_var.get():
                root.after(250, _start)
            root.mainloop()
            return 0


def main() -> int:
    _setup_logging()
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--daemon", action="store_true", help="Run agent daemon (no UI)")
    ap.add_argument("--ui", action="store_true", help="Force UI mode")
    ap.add_argument(
        "--webview-debug",
        action="store_true",
        help="Enable pywebview debug/devtools for the embedded UI",
    )
    ap.add_argument(
        "--fallback-ui",
        action="store_true",
        help="Force fallback Tk UI (recommended if WebView2 is not installed)",
    )
    ap.add_argument("--token", default=None)
    ap.add_argument("--shared-secret", default=None)
    ap.add_argument("--server", default=None)
    args = ap.parse_args()

    if args.fallback_ui:
        logging.info("--fallback-ui requested; using built-in fallback path when available")

    if args.webview_debug:
        logging.info("--webview-debug requested")

    if args.daemon and not args.ui:
        return int(run_daemon(token=args.token, server_url=args.server, shared_secret=args.shared_secret))

    return int(run_ui())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        _setup_logging()
        try:
            logging.error("Fatal error: %s\n%s", e, traceback.format_exc())
        except Exception:
            pass
        _message_box(
            "Jarvis PC Agent",
            "Jarvis PC Agent failed to start.\n\n"
            f"Error: {e}\n\n"
            f"Log file: {LOG_FILE}",
        )
        raise SystemExit(1)
