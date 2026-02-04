"""PC Agent Desktop Application

Native desktop application for managing the Jarvis PC Agent.

Notes:
- This is an OPTIONAL controller UI.
- The actual agent daemon remains `pc_agent.py` at repo root.
"""

import subprocess
import threading
import json
import os
import platform
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List
import time

import PySimpleGUI as sg
import psutil


def _find_repo_root(start: Path) -> Path:
    """Walk up from start until we find the repo root (pc_agent.py)."""
    for candidate in [start, *start.parents]:
        if (candidate / "pc_agent.py").exists() or (candidate / "dist" / "JarvisPCAgent.exe").exists():
            return candidate
    return start


# Paths
HERE = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else HERE
REPO_ROOT = _find_repo_root(APP_DIR)
AGENT_SCRIPT = REPO_ROOT / "pc_agent.py"
AGENT_EXE = (REPO_ROOT / "dist" / "JarvisPCAgent.exe")
CONFIG_FILE = REPO_ROOT / "data" / "agent_desktop_config.json"
PERMISSIONS_FILE = REPO_ROOT / "data" / "agent_permissions.json"


# Global state
_agent_process: Optional[subprocess.Popen] = None
_agent_running = False
_agent_logs: List[Dict[str, str]] = []
_max_logs = 200
_config: Dict[str, Any] = {}
_permissions: Dict[str, Any] = {}
_stop_requested = False


def load_config() -> Dict[str, Any]:
    try:
        if CONFIG_FILE.exists():
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    return {
        "server_url": os.getenv("JARVIS_SERVER_URL", "https://jarvis-cloud-assistant.onrender.com"),
        "device_id": os.getenv("JARVIS_DEVICE_ID", platform.node() or "primary"),
        "agent_token": "",
        "auto_start": False,
        "loop_mode": False,
        "window_x": 100,
        "window_y": 100,
        "window_width": 900,
        "window_height": 850,
    }


def save_config(config: Dict[str, Any]) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception as e:
        add_log(f"Error saving config: {e}", "ERROR")


def load_permissions() -> Dict[str, Any]:
    try:
        if PERMISSIONS_FILE.exists():
            return json.loads(PERMISSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass

    return {
        "allow_app_control": False,
        "allow_execute_command": False,
        "allow_file_ops": False,
        "allow_screen": False,
        "allow_self_update": False,
    }


def add_log(message: str, level: str = "INFO") -> None:
    global _agent_logs
    timestamp = datetime.now().strftime("%H:%M:%S")
    _agent_logs.append({"timestamp": timestamp, "level": level, "message": message})
    if len(_agent_logs) > _max_logs:
        _agent_logs = _agent_logs[-_max_logs:]


def get_logs_text() -> str:
    if not _agent_logs:
        return "[--:--:--] Waiting for logs...\n"

    lines: List[str] = []
    for log in _agent_logs:
        time_str = log["timestamp"]
        level = log["level"]
        msg = log["message"]
        prefix = "•"
        if level == "ERROR":
            prefix = "X"
        elif level == "WARNING":
            prefix = "!"
        elif level == "SUCCESS":
            prefix = "+"
        lines.append(f"[{time_str}] {prefix} {msg}")
    return "\n".join(lines)


def get_python_exe() -> str:
    candidates = [
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / "venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "bin" / "python",
        REPO_ROOT / "venv" / "bin" / "python",
    ]
    for exe in candidates:
        if exe.exists():
            return str(exe)
    return "python"


def get_agent_command(token: Optional[str], server_url: str) -> List[str]:
    """Return the command to run the agent.

    Prefer the packaged exe when present; otherwise run from source.
    """
    if AGENT_EXE.exists():
        args: List[str] = [str(AGENT_EXE)]
    else:
        args = [get_python_exe(), str(AGENT_SCRIPT)]

    if token:
        args += ["--token", token]
    if server_url:
        args += ["--server", server_url]
    return args


def get_agent_status() -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "running": _agent_running,
        "pid": _agent_process.pid if _agent_process else None,
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "cpu_percent": 0.0,
        "memory_mb": 0.0,
    }

    if _agent_running and _agent_process:
        try:
            proc = psutil.Process(_agent_process.pid)
            status["cpu_percent"] = float(proc.cpu_percent(interval=0.1))
            status["memory_mb"] = float(proc.memory_info().rss / (1024 * 1024))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return status


def _read_stream_lines(stream, level: str) -> None:
    try:
        for line in stream:
            if not line:
                break
            line = line.strip()
            if line:
                add_log(line, level)
    except Exception:
        pass


def _agent_supervisor(token: Optional[str], server_url: str, device_id: str, loop: bool) -> None:
    global _agent_process, _agent_running, _stop_requested

    env = os.environ.copy()
    if server_url:
        env["JARVIS_SERVER_URL"] = server_url
    if device_id:
        env["JARVIS_DEVICE_ID"] = device_id

    while True:
        if _stop_requested:
            return

        args = get_agent_command(token=token, server_url=server_url)

        try:
            add_log("Starting PC Agent...", "INFO")
            _agent_process = subprocess.Popen(
                args,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            _agent_running = True
            add_log(f"Agent started (PID: {_agent_process.pid})", "SUCCESS")

            if _agent_process.stdout:
                threading.Thread(target=_read_stream_lines, args=(_agent_process.stdout, "INFO"), daemon=True).start()
            if _agent_process.stderr:
                threading.Thread(target=_read_stream_lines, args=(_agent_process.stderr, "ERROR"), daemon=True).start()

            exit_code = _agent_process.wait()
            _agent_running = False
            _agent_process = None
            if _stop_requested:
                add_log("Agent stopped", "SUCCESS")
                return

            add_log(f"Agent exited (code {exit_code})", "WARNING")
        except Exception as e:
            _agent_running = False
            _agent_process = None
            add_log(f"Failed to start agent: {e}", "ERROR")

        if not loop:
            return

        add_log("Restarting in 5 seconds (loop mode)...", "INFO")
        for _ in range(5):
            if _stop_requested:
                return
            time.sleep(1)


def start_agent(token: Optional[str], server_url: str, device_id: str, loop: bool) -> None:
    global _stop_requested
    if _agent_running:
        add_log("Agent is already running", "WARNING")
        return

    if not AGENT_EXE.exists() and not AGENT_SCRIPT.exists():
        add_log(f"Agent not found. Missing: {AGENT_EXE} and {AGENT_SCRIPT}", "ERROR")
        return

    _stop_requested = False
    threading.Thread(target=_agent_supervisor, args=(token, server_url, device_id, loop), daemon=True).start()


def stop_agent() -> None:
    global _agent_process, _agent_running, _stop_requested
    if not _agent_running or not _agent_process:
        add_log("Agent is not running", "WARNING")
        return

    _stop_requested = True
    try:
        add_log("Stopping agent...", "INFO")
        _agent_process.terminate()
        try:
            _agent_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            add_log("Force killing agent...", "WARNING")
            _agent_process.kill()
            _agent_process.wait(timeout=5)
    except Exception as e:
        add_log(f"Error stopping agent: {e}", "ERROR")
    finally:
        _agent_running = False
        _agent_process = None


def setup_theme() -> None:
    sg.theme("DarkBlue3")
    sg.set_options(
        font=("Segoe UI", 10),
        element_padding=(5, 5),
        button_color=("#FFFFFF", "#0078D4"),
        border_width=1,
        margins=(10, 10),
    )


def create_layout() -> List:
    return [
        [
            sg.Column(
                [
                    [sg.Text("Jarvis PC Agent", font=("Segoe UI", 18, "bold"))],
                    [sg.Text("Desktop Control Application", font=("Segoe UI", 10), text_color="gray")],
                ],
                vertical_alignment="center",
            )
        ],
        [sg.HorizontalSeparator()],
        [
            sg.Frame(
                "Status",
                [
                    [
                        sg.Column(
                            [
                                [sg.Text("Status:", size=(15, 1)), sg.Text("Not Running", key="-STATUS-TEXT-", font=("Segoe UI", 10, "bold"), text_color="red")],
                                [sg.Text("Process ID:", size=(15, 1)), sg.Text("—", key="-PID-TEXT-")],
                                [sg.Text("Platform:", size=(15, 1)), sg.Text("—", key="-PLATFORM-TEXT-")],
                                [sg.Text("Python:", size=(15, 1)), sg.Text("—", key="-PYTHON-TEXT-")],
                            ]
                        ),
                        sg.Column(
                            [
                                [sg.Text("CPU:", size=(15, 1)), sg.Text("0%", key="-CPU-TEXT-")],
                                [sg.Text("Memory:", size=(15, 1)), sg.Text("0 MB", key="-MEMORY-TEXT-")],
                                [sg.Text("Last Update:", size=(15, 1)), sg.Text("—", key="-UPDATE-TEXT-")],
                            ]
                        ),
                    ]
                ],
                font=("Segoe UI", 11, "bold"),
            )
        ],
        [
            sg.Button("Start Agent", key="-START-", size=(15, 1), button_color=("white", "green")),
            sg.Button("Stop Agent", key="-STOP-", size=(15, 1), button_color=("white", "red"), disabled=True),
            sg.Button("Refresh", key="-REFRESH-", size=(15, 1)),
        ],
        [
            sg.Frame(
                "Configuration",
                [
                    [sg.Text("Server URL:", size=(15, 1)), sg.InputText(key="-SERVER-URL-", size=(50, 1))],
                    [sg.Text("Device ID:", size=(15, 1)), sg.InputText(key="-DEVICE-ID-", size=(50, 1))],
                    [sg.Text("Agent Token:", size=(15, 1)), sg.InputText(key="-TOKEN-", size=(50, 1), password_char="•")],
                    [
                        sg.Checkbox("Loop Mode (auto-restart)", key="-LOOP-MODE-", default=False),
                        sg.Checkbox("Auto-start Agent", key="-AUTO-START-", default=False),
                    ],
                    [sg.Button("Save Config", key="-SAVE-CONFIG-"), sg.Button("Reset", key="-RESET-")],
                ],
                font=("Segoe UI", 11, "bold"),
            )
        ],
        [
            sg.Frame(
                "Permissions (from server)",
                [
                    [
                        sg.Column(
                            [
                                [sg.Text("App Control:", size=(20, 1)), sg.Text("Disabled", key="-PERM-APP-", text_color="red")],
                                [sg.Text("Execute Commands:", size=(20, 1)), sg.Text("Disabled", key="-PERM-CMD-", text_color="red")],
                            ]
                        ),
                        sg.Column(
                            [
                                [sg.Text("File Operations:", size=(20, 1)), sg.Text("Disabled", key="-PERM-FILE-", text_color="red")],
                                [sg.Text("Screen Access:", size=(20, 1)), sg.Text("Disabled", key="-PERM-SCREEN-", text_color="red")],
                            ]
                        ),
                        sg.Column(
                            [[sg.Text("Self-Update:", size=(20, 1)), sg.Text("Disabled", key="-PERM-UPDATE-", text_color="red")]]
                        ),
                    ]
                ],
                font=("Segoe UI", 11, "bold"),
            )
        ],
        [
            sg.Frame(
                "Agent Logs",
                [
                    [
                        sg.Multiline(
                            size=(100, 15),
                            key="-LOGS-",
                            disabled=True,
                            background_color="#1e1e1e",
                            text_color="#00ff00",
                            font=("Courier New", 9),
                        )
                    ],
                    [sg.Button("Clear Logs", key="-CLEAR-LOGS-"), sg.Push(), sg.Text("Auto-refresh: ON")],
                ],
                font=("Segoe UI", 11, "bold"),
            )
        ],
        [sg.HorizontalSeparator()],
        [sg.Text("Ready", key="-STATUS-BAR-", size=(50, 1)), sg.Push(), sg.Button("Exit", key="-EXIT-")],
    ]


def main() -> None:
    global _config, _permissions, _agent_logs

    setup_theme()

    _config = load_config()
    _permissions = load_permissions()

    layout = create_layout()
    window = sg.Window(
        "Jarvis PC Agent Control Panel",
        layout,
        size=(_config.get("window_width", 900), _config.get("window_height", 850)),
        finalize=True,
        location=(_config.get("window_x", 100), _config.get("window_y", 100)),
    )

    window["-SERVER-URL-"].update(_config.get("server_url", ""))
    window["-DEVICE-ID-"].update(_config.get("device_id", ""))
    window["-TOKEN-"].update(_config.get("agent_token", ""))
    window["-LOOP-MODE-"].update(_config.get("loop_mode", False))
    window["-AUTO-START-"].update(_config.get("auto_start", False))

    status = get_agent_status()
    window["-PLATFORM-TEXT-"].update(status["platform"])
    window["-PYTHON-TEXT-"].update(status["python_version"])

    def update_status_thread() -> None:
        last_update = 0.0
        while True:
            time.sleep(1)
            try:
                if time.time() - last_update < 2:
                    continue

                status = get_agent_status()
                window["-STATUS-TEXT-"].update(
                    "Running" if status["running"] else "Not Running",
                    text_color="green" if status["running"] else "red",
                )
                window["-PID-TEXT-"].update(str(status["pid"]) if status["pid"] else "—")
                window["-CPU-TEXT-"].update(f"{status['cpu_percent']:.1f}%")
                window["-MEMORY-TEXT-"].update(f"{status['memory_mb']:.1f} MB")
                window["-UPDATE-TEXT-"].update(datetime.now().strftime("%H:%M:%S"))

                window["-START-"].update(disabled=status["running"])
                window["-STOP-"].update(disabled=not status["running"])

                perms = load_permissions()
                mapping = {
                    "allow_app_control": "-PERM-APP-",
                    "allow_execute_command": "-PERM-CMD-",
                    "allow_file_ops": "-PERM-FILE-",
                    "allow_screen": "-PERM-SCREEN-",
                    "allow_self_update": "-PERM-UPDATE-",
                }
                for key, elem in mapping.items():
                    enabled = bool(perms.get(key, False))
                    window[elem].update("Enabled" if enabled else "Disabled", text_color="green" if enabled else "red")

                last_update = time.time()
            except Exception:
                continue

    threading.Thread(target=update_status_thread, daemon=True).start()

    add_log(f"Repo root: {REPO_ROOT}", "INFO")
    add_log("Application started", "SUCCESS")

    if _config.get("auto_start", False):
        start_agent(
            _config.get("agent_token") or None,
            _config.get("server_url") or "",
            _config.get("device_id") or "",
            bool(_config.get("loop_mode", False)),
        )

    while True:
        event, values = window.read(timeout=500)
        if event in (sg.WINDOW_CLOSED, "-EXIT-"):
            break

        if event == "-START-":
            start_agent(
                values.get("-TOKEN-") or None,
                values.get("-SERVER-URL-") or "",
                values.get("-DEVICE-ID-") or "",
                bool(values.get("-LOOP-MODE-")),
            )
            window["-STATUS-BAR-"].update("Starting agent...")

        elif event == "-STOP-":
            stop_agent()
            window["-STATUS-BAR-"].update("Stopping agent...")

        elif event == "-REFRESH-":
            window["-STATUS-BAR-"].update("Refreshed")

        elif event == "-SAVE-CONFIG-":
            _config["server_url"] = values.get("-SERVER-URL-", "")
            _config["device_id"] = values.get("-DEVICE-ID-", "")
            _config["agent_token"] = values.get("-TOKEN-", "")
            _config["loop_mode"] = bool(values.get("-LOOP-MODE-"))
            _config["auto_start"] = bool(values.get("-AUTO-START-"))
            save_config(_config)
            window["-STATUS-BAR-"].update("Configuration saved")
            add_log("Configuration saved", "SUCCESS")

        elif event == "-RESET-":
            _config = load_config()
            window["-SERVER-URL-"].update(_config.get("server_url", ""))
            window["-DEVICE-ID-"].update(_config.get("device_id", ""))
            window["-TOKEN-"].update(_config.get("agent_token", ""))
            window["-LOOP-MODE-"].update(_config.get("loop_mode", False))
            window["-AUTO-START-"].update(_config.get("auto_start", False))
            window["-STATUS-BAR-"].update("Configuration reset")
            add_log("Configuration reset", "INFO")

        elif event == "-CLEAR-LOGS-":
            _agent_logs = []
            window["-LOGS-"].update("")
            add_log("Logs cleared", "INFO")

        window["-LOGS-"].update(get_logs_text())

    if _agent_running:
        stop_agent()
    window.close()


if __name__ == "__main__":
    main()
