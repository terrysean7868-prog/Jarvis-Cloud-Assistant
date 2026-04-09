import asyncio
import argparse
import base64
import json
import os
import sys
import platform
import re
import signal
import shutil
import subprocess
import time
from pathlib import Path
from datetime import datetime, UTC

import aiohttp

try:
    import psutil
except Exception:
    psutil = None

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# NOTE: This agent is intentionally defensive and execution-only.
# It receives cloud actions, executes them, and returns structured results.

# system_ops may not exist on some platforms; in this repo it should.
try:
    from src.utils.system_operations import system_ops
    SYSTEM_OPS_AVAILABLE = True
except Exception:
    system_ops = None
    SYSTEM_OPS_AVAILABLE = False

def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "y")


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


SERVER_BASE_URL = _env_str("JARVIS_SERVER_URL", "https://jarvis-cloud-assistant.onrender.com").rstrip("/")
# Device IDs are treated case-insensitively by the server.
DEVICE_ID = (_env_str("JARVIS_DEVICE_ID", "") or "").strip().lower()

ALLOW_EXECUTE_COMMAND = _env_bool("JARVIS_AGENT_ALLOW_EXECUTE_COMMAND", "false")
ALLOW_APP_CONTROL = _env_bool("JARVIS_AGENT_ALLOW_APP_CONTROL", "false")
ALLOW_SCREEN = _env_bool("JARVIS_AGENT_ALLOW_SCREEN", "false")
ALLOW_SELF_UPDATE = _env_bool("JARVIS_AGENT_ALLOW_SELF_UPDATE", "false")

# Allow the server (after explicit user approval in UI) to toggle the above flags at runtime.
# This avoids manual env edits for common cases.
ALLOW_REMOTE_PERMISSION_CHANGES = _env_bool("JARVIS_AGENT_ALLOW_REMOTE_PERMISSION_CHANGES", "true")

# Persist approved permissions so future runs work immediately.
_PERMISSIONS_FILE_RAW = _env_str("JARVIS_AGENT_PERMISSIONS_FILE", "").strip()
PERMISSIONS_FILE = Path(_PERMISSIONS_FILE_RAW).expanduser() if _PERMISSIONS_FILE_RAW else None

# File ops are powerful. Keep sandboxed.
ALLOW_FILE_OPS = _env_bool("JARVIS_AGENT_ALLOW_FILE_OPS", "false")
PROJECT_ROOT = Path(_env_str("JARVIS_AGENT_PROJECT_ROOT", str(REPO_ROOT))).resolve()
ALLOWED_PATHS_RAW = _env_str("JARVIS_AGENT_ALLOWED_PATHS", "data,docs,src,modules")
ALLOWED_ROOTS = [(PROJECT_ROOT / p.strip()).resolve() for p in ALLOWED_PATHS_RAW.split(",") if p.strip()]
BLOCKED_DIRS = {".git", "venv", "__pycache__", ".pytest_cache", "node_modules"}
BLOCKED_FILES = {".env", ".env.example", "id_rsa", "id_rsa.pub"}

# Prevent accidental double-launches when the same open_app arrives twice.
# Key: app_name lower, Value: monotonic timestamp
_RECENT_APP_OPENS: dict[str, float] = {}

PING_INTERVAL_S = _env_int("JARVIS_AGENT_PING_INTERVAL", 20)

# Optional strict allowlist of action types.
# If set, ONLY these action types will be executed.
_ALLOWLIST_RAW = _env_str("JARVIS_AGENT_ACTION_ALLOWLIST", "").strip()
ACTION_ALLOWLIST = {a.strip() for a in _ALLOWLIST_RAW.split(",") if a.strip()} if _ALLOWLIST_RAW else None

# Allows the server to request agent shutdown (e.g., on user logout).
_STOP_EVENT = None

_ACTION_MAPPINGS_FILE_RAW = _env_str("JARVIS_AGENT_ACTION_MAPPINGS_FILE", "").strip()
ACTION_MAPPINGS_FILE = Path(_ACTION_MAPPINGS_FILE_RAW).expanduser() if _ACTION_MAPPINGS_FILE_RAW else None
MAX_LEARNED_ACTION_MAPPINGS = max(50, _env_int("JARVIS_AGENT_MAX_ACTION_MAPPINGS", 400))
MIN_LEARNED_MAPPING_CONFIDENCE = 0.25

DEVICE_ACTION_NAMES = {
    "bluetooth_on",
    "bluetooth_off",
    "wifi_on",
    "wifi_off",
    "volume_up",
    "volume_down",
    "volume_mute",
    "volume_unmute",
    "brightness_up",
    "brightness_down",
    "lock_screen",
    "sleep",
    "shutdown",
    "restart",
    "open_app",
    "close_app",
}


def _permissions_file_path() -> Path:
    if PERMISSIONS_FILE:
        return PERMISSIONS_FILE
    return (PROJECT_ROOT / "data" / "agent_permissions.json").resolve()


def _load_saved_permissions() -> None:
    """Load previously approved permissions from disk and apply them."""
    global ALLOW_APP_CONTROL, ALLOW_EXECUTE_COMMAND, ALLOW_FILE_OPS, ALLOW_SCREEN, ALLOW_SELF_UPDATE
    try:
        p = _permissions_file_path()
        if not p.exists():
            return
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        if not isinstance(data, dict):
            return

        if "allow_app_control" in data:
            ALLOW_APP_CONTROL = bool(data["allow_app_control"])
        if "allow_execute_command" in data:
            ALLOW_EXECUTE_COMMAND = bool(data["allow_execute_command"])
        if "allow_file_ops" in data:
            ALLOW_FILE_OPS = bool(data["allow_file_ops"])
        if "allow_screen" in data:
            ALLOW_SCREEN = bool(data["allow_screen"])
        if "allow_self_update" in data:
            ALLOW_SELF_UPDATE = bool(data["allow_self_update"])
    except Exception:
        return


def _save_permissions() -> None:
    """Persist current permissions locally (booleans only)."""
    try:
        p = _permissions_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        safe = {
            "allow_app_control": bool(ALLOW_APP_CONTROL),
            "allow_execute_command": bool(ALLOW_EXECUTE_COMMAND),
            "allow_file_ops": bool(ALLOW_FILE_OPS),
            "allow_screen": bool(ALLOW_SCREEN),
            "allow_self_update": bool(ALLOW_SELF_UPDATE),
            "updated_at": _now_utc_iso(),
        }
        p.write_text(json.dumps(safe, indent=2), encoding="utf-8")
    except Exception:
        return


def _supported_actions_catalog() -> list[str]:
    # Keep this in sync with _execute_action handlers so backend/frontend can reason
    # about agent capabilities without stale hard-coded lists.
    return sorted([
        "inspect_system_state",
        "monitor_performance",
        "analyze_screen",
        "list_device_actions",
        "set_brightness",
        "set_power_plan",
        "set_volume",
        "set_mute",
        "lock_screen",
        "open_settings",
        "sleep",
        "hibernate",
        "shutdown",
        "restart",
        "logoff",
        "open_url",
        "communicate_with_assistant",
        "communicate_with_google_assistant",
        "open_path",
        "get_clipboard",
        "set_clipboard",
        "list_running_apps",
        "screen_info",
        "open_windows",
        "launch_application",
        "list_processes",
        "kill_process",
        "set_wifi",
        "set_bluetooth",
        "set_airplane_mode",
        # Expanded high-level device_action names (dispatched by handle_device_action).
        "bluetooth_on",
        "bluetooth_off",
        "wifi_on",
        "wifi_off",
        "volume_up",
        "volume_down",
        "volume_mute",
        "volume_unmute",
        "brightness_up",
        "brightness_down",
        # UI automation (requires allow_screen)
        "show_desktop",
        "open_task_manager",
        "open_run_dialog",
        "open_start_menu",
        "open_quick_settings",
        "open_notification_center",
        "window_snap_left",
        "window_snap_right",
        "window_maximize",
        "window_minimize",
        "media_play_pause",
        "media_next_track",
        "media_prev_track",
        "media_stop",
        "alt_tab",
        "save_screenshot",
        "find_files",
        # Existing compatibility action types routed in _execute_action.
        "open_app",
        "close_app",
        "switch_app",
        "execute_command",
        "run_command",
        "capture_screen",
        "screen_navigation",
        "type_text",
        "press_key",
        "hotkey",
        "read",
        "write",
        "edit",
        "delete",
        "move",
        "copy",
        "list",
        "mkdir",
        "cleanup",
        "agent_set_permissions",
        "agent_stop",
        "agent_shutdown",
    ])


def _normalize_contract_status(value: str | None) -> str:
    s = str(value or "").strip().lower()
    if s in {"success", "ok", "completed"}:
        return "completed"
    if s in {"error", "failed", "forbidden"}:
        return "failed"
    if not s:
        return "completed"
    return s


def _normalize_incoming_action(action_payload: dict | None, *, job_id: str, action_index: int) -> tuple[dict, str, str]:
    payload = action_payload if isinstance(action_payload, dict) else {}

    # New contract: {action, params, task_id}
    if isinstance(payload.get("action"), str):
        action_name = str(payload.get("action") or "").strip()
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        task_id = str(payload.get("task_id") or f"{job_id}:{action_index}").strip()
        normalized = {"type": action_name, **params}
        return normalized, action_name, task_id

    # Backward-compatible legacy contract: {type, ...}
    action_name = str(payload.get("type") or "").strip()
    task_id = str(payload.get("task_id") or f"{job_id}:{action_index}").strip()
    return payload, action_name, task_id


async def _execute_action_contract(action_payload: dict | None, *, job_id: str, action_index: int) -> dict:
    normalized_action, action_name, task_id = _normalize_incoming_action(
        action_payload,
        job_id=job_id,
        action_index=action_index,
    )
    print(f"[AGENT] Received action: {action_name}", flush=True)

    started = time.perf_counter()
    try:
        raw_result = await _execute_action(normalized_action)
        if not isinstance(raw_result, dict):
            raw_result = {"status": "completed", "value": raw_result}
    except Exception as e:
        raw_result = {"status": "error", "message": str(e)}

    elapsed = float(max(0.0, time.perf_counter() - started))
    raw_status = str((raw_result or {}).get("status") or "")
    status = _normalize_contract_status(raw_status)

    error = None
    if status == "failed":
        error = str((raw_result or {}).get("message") or (raw_result or {}).get("error") or "execution_failed")
        print(f"[AGENT] Execution failed: {action_name} error={error}", flush=True)
    else:
        print(f"[AGENT] Executed successfully: {action_name}", flush=True)

    return {
        "status": status,
        "result": raw_result,
        "error": error,
        "execution_time": round(elapsed, 4),
        "task_id": task_id,
        "action": action_name,
    }


def _current_capabilities() -> dict:
    windows = platform.system().lower() == "windows"
    return {
        "allow_execute_command": bool(ALLOW_EXECUTE_COMMAND),
        "allow_app_control": bool(ALLOW_APP_CONTROL),
        "allow_screen": bool(ALLOW_SCREEN),
        "allow_self_update": bool(ALLOW_SELF_UPDATE),
        "allow_file_ops": bool(ALLOW_FILE_OPS),
        "bluetooth": bool(windows),
        "wifi": bool(windows),
        "volume": bool(windows),
        "brightness": bool(windows),
        "platform": platform.system().lower(),
        "hostname": platform.node(),
        "actions": _supported_actions_catalog(),
        "system_info": _inspect_system_state(),
    }


def _resolve_device_id(override_device_id: str | None = None) -> str:
    explicit = str(override_device_id or "").strip().lower()
    if explicit:
        return explicit
    env_did = str(DEVICE_ID or "").strip().lower()
    if env_did:
        return env_did
    host_did = str(platform.node() or "").strip().lower()
    if host_did:
        return host_did
    return "primary"


def _token_device_id_unverified(token: str | None) -> str:
    raw = str(token or "").strip()
    if not raw or raw.count(".") < 2:
        return ""
    try:
        payload_part = raw.split(".")[1]
        pad = "=" * (-len(payload_part) % 4)
        decoded = base64.urlsafe_b64decode((payload_part + pad).encode("utf-8"))
        payload = json.loads(decoded.decode("utf-8", errors="ignore"))
        if not isinstance(payload, dict):
            return ""
        did = str(payload.get("device_id") or payload.get("sub") or "").strip().lower()
        return did
    except Exception:
        return ""


def _inspect_system_state() -> dict:
    cpu_percent = None
    memory_percent = None
    disk_percent = None
    process_count = None
    if psutil is not None:
        try:
            cpu_percent = psutil.cpu_percent(interval=0.2)
            memory_percent = psutil.virtual_memory().percent
            disk_percent = psutil.disk_usage(str(PROJECT_ROOT)).percent
            process_count = len(psutil.pids())
        except Exception:
            pass

    return {
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "hostname": platform.node(),
        "cpu_percent": cpu_percent,
        "memory_percent": memory_percent,
        "disk_percent": disk_percent,
        "process_count": process_count,
    }


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _get_app_manager():
    # Lazy import to avoid optional Windows deps and other packaging issues.
    try:
        from src.utils.app_manager import app_manager
        return app_manager
    except Exception:
        return None


def _get_screen_access():
    # Lazy import: screen_access may pull OCR/UI dependencies.
    try:
        from src.utils.screen_access import screen_access
        return screen_access
    except Exception:
        return None


def _ws_url_from_base(base: str) -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/ws/agent"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/ws/agent"
    # assume https
    return "wss://" + base + "/ws/agent"


def _is_path_allowed(path: str) -> bool:
    if not path:
        return False
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    rp = p.resolve()

    # Must stay inside PROJECT_ROOT
    try:
        rp.relative_to(PROJECT_ROOT)
    except Exception:
        return False

    if rp.name in BLOCKED_FILES:
        return False
    if any(part in BLOCKED_DIRS for part in rp.parts):
        return False

    for root in ALLOWED_ROOTS:
        try:
            rp.relative_to(root)
            return True
        except Exception:
            continue
    return False


def _is_dangerous_command(command: str) -> bool:
    c = (command or "").strip()
    if not c:
        return False
    cl = c.lower()

    high_risk_patterns = [
        r"\bformat\b",
        r"\bdiskpart\b",
        r"\bmkfs(\.[a-z0-9]+)?\b",
        r"\bfdisk\b",
        r"\bparted\b",
        r"\bgparted\b",
        r"\b(wipefs|dd)\b",
        r"\bbootrec\b",
        r"\bbcdedit\b",
        r"\breg(ed(it)?|\s+add|\s+delete|\s+import)\b",
        r"\bdism\b.*\/(remove-package|disable-feature)",
        r"remove-item\b.*\b(-recurse|-force)\b",
    ]
    for pat in high_risk_patterns:
        try:
            if re.search(pat, cl, re.IGNORECASE):
                return True
        except Exception:
            continue

    if re.search(r"\brm\b\s+.*\s-\s*rf\s+/(?:\s|$)", cl):
        return True
    if "--no-preserve-root" in cl and "rm" in cl and "/" in cl:
        return True

    delete_words = ("rm ", " del ", "erase", "rmdir", " rd ", "remove-item")
    system_markers = (
        "c:\\windows",
        "\\windows\\system32",
        "system32",
        "c:\\program files",
        "c:\\program files (x86)",
        "c:\\programdata",
        "system volume information",
        "/etc/",
        "/bin/",
        "/sbin/",
        "/usr/",
        "/boot/",
        "/system/",
        "/library/",
    )
    if any(dw in cl for dw in delete_words) and any(sm in cl for sm in system_markers):
        return True

    return False


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content or "", encoding="utf-8")


def _cleanup_project(root: Path) -> dict:
    patterns = ["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]
    deleted = 0
    for dirpath, dirnames, _filenames in os.walk(root):
        for pat in patterns:
            if pat in dirnames:
                try:
                    shutil.rmtree(Path(dirpath) / pat)
                    deleted += 1
                except Exception:
                    pass
    return {"status": "success", "deleted": deleted}


def _run_powershell(command: str, timeout_s: float = 10.0) -> tuple[int, str, str]:
    """Run a PowerShell one-liner and return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return int(p.returncode), (p.stdout or ""), (p.stderr or "")
    except Exception as e:
        return 1, "", str(e)


async def _type_text_with_timeout(sa, text: str, interval: float, timeout_s: float) -> tuple[bool, str]:
    """Run UI typing with a bounded timeout so action results never hang forever."""
    try:
        ok = await asyncio.wait_for(
            asyncio.to_thread(sa.type_text, text, interval=interval),
            timeout=max(1.0, float(timeout_s)),
        )
        return bool(ok), ""
    except asyncio.TimeoutError:
        return False, f"Typing timed out after {float(timeout_s):.1f}s"
    except Exception as e:
        return False, str(e)


def _get_windows_brightness() -> int | None:
    """Best-effort current brightness (0-100) on Windows."""
    if platform.system() != "Windows":
        return None
    rc, out, _err = _run_powershell(
        "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1 -ExpandProperty CurrentBrightness)",
        timeout_s=8.0,
    )
    if rc != 0:
        return None
    m = re.search(r"\b(\d{1,3})\b", out or "")
    if not m:
        return None
    try:
        v = int(m.group(1))
    except Exception:
        return None
    return max(0, min(100, v))


def _set_windows_brightness(value: int) -> tuple[bool, str]:
    """Set brightness (0-100) via WMI. Returns (ok, message)."""
    if platform.system() != "Windows":
        return False, "Brightness control is only supported on Windows agents."
    v = max(0, min(100, int(value)))
    # Apply to all supported monitors.
    ps = (
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
        f"| ForEach-Object {{ $_.WmiSetBrightness(1, {v}) }}"
    )
    rc, _out, err = _run_powershell(ps, timeout_s=12.0)
    return (rc == 0), (err.strip() if err else "")


def _get_windows_audio_endpoint_volume():
    """Return (endpoint_volume, err). Uses pycaw when available."""
    if platform.system() != "Windows":
        return None, "Audio control is only supported on Windows agents."
    try:
        # Lazy import to avoid hard dependency at agent startup.
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        endpoint = cast(interface, POINTER(IAudioEndpointVolume))
        return endpoint, ""
    except Exception as e:
        return None, f"Audio control unavailable: {e}"


def _get_windows_volume_percent() -> tuple[int | None, str]:
    endpoint, err = _get_windows_audio_endpoint_volume()
    if not endpoint:
        return None, err
    try:
        scalar = float(endpoint.GetMasterVolumeLevelScalar())
        return max(0, min(100, int(round(scalar * 100.0)))), ""
    except Exception as e:
        return None, str(e)


def _set_windows_volume_percent(value: int) -> tuple[bool, str]:
    endpoint, err = _get_windows_audio_endpoint_volume()
    if not endpoint:
        return False, err
    try:
        v = max(0, min(100, int(value)))
        endpoint.SetMasterVolumeLevelScalar(float(v) / 100.0, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def _get_windows_mute() -> tuple[bool | None, str]:
    endpoint, err = _get_windows_audio_endpoint_volume()
    if not endpoint:
        return None, err
    try:
        muted = bool(endpoint.GetMute())
        return muted, ""
    except Exception as e:
        return None, str(e)


def _set_windows_mute(muted: bool) -> tuple[bool, str]:
    endpoint, err = _get_windows_audio_endpoint_volume()
    if not endpoint:
        return False, err
    try:
        endpoint.SetMute(1 if bool(muted) else 0, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def _set_windows_power_plan(plan_name: str) -> tuple[bool, str, str | None]:
    """Set Windows power plan by partial name match. Returns (ok, message, guid)."""
    if platform.system() != "Windows":
        return False, "Power plan control is only supported on Windows agents.", None

    desired = (plan_name or "").strip().lower()
    if not desired:
        return False, "plan is required", None

    try:
        p = subprocess.run(["powercfg", "/l"], capture_output=True, text=True, timeout=8.0)
    except Exception as e:
        return False, f"Failed to list power plans: {e}", None

    text_out = (p.stdout or "") + "\n" + (p.stderr or "")
    # Example line: "Power Scheme GUID: xxxx-...  (Balanced) *"
    candidates: list[tuple[str, str]] = []
    for line in text_out.splitlines():
        m = re.search(r"Power Scheme GUID:\s*([0-9a-fA-F\-]{36})\s*\(([^\)]+)\)", line)
        if not m:
            continue
        guid = m.group(1).lower()
        name = m.group(2).strip()
        candidates.append((guid, name))

    if not candidates:
        return False, "No power plans found via powercfg.", None

    # Match by substring.
    chosen = None
    for guid, name in candidates:
        if desired in name.lower():
            chosen = (guid, name)
            break
    if chosen is None:
        # OEM images sometimes hide built-in plans from /l. Try well-known GUIDs.
        known = {
            "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
            "power saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
            "high performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
            "ultimate performance": "e9a42b02-d5df-448d-aa00-03f14749eb61",
        }

        desired_key = None
        if "ultimate" in desired:
            desired_key = "ultimate performance"
        elif "high" in desired and "perform" in desired:
            desired_key = "high performance"
        elif "power" in desired and "saver" in desired:
            desired_key = "power saver"
        elif "battery" in desired and "saver" in desired:
            desired_key = "power saver"
        elif desired in ("balanced", "balance"):
            desired_key = "balanced"

        if desired_key and desired_key in known:
            guid = known[desired_key]
            try:
                p2 = subprocess.run(["powercfg", "/setactive", guid], capture_output=True, text=True, timeout=8.0)
                if p2.returncode == 0:
                    return True, f"Set power plan to '{desired_key}'.", guid
                # If the scheme doesn't exist on this image, try recreating it.
                pdup = subprocess.run(["powercfg", "-duplicatescheme", guid], capture_output=True, text=True, timeout=10.0)
                if pdup.returncode == 0:
                    txt = (pdup.stdout or "") + "\n" + (pdup.stderr or "")
                    mnew = re.search(r"\b([0-9a-fA-F\-]{36})\b", txt)
                    new_guid = (mnew.group(1).lower() if mnew else "")
                    if new_guid:
                        p3 = subprocess.run(["powercfg", "/setactive", new_guid], capture_output=True, text=True, timeout=8.0)
                        if p3.returncode == 0:
                            return True, f"Set power plan to '{desired_key}'.", new_guid
            except Exception:
                pass

        names = ", ".join([n for _g, n in candidates][:6])
        return False, f"Power plan '{plan_name}' not found. Available: {names}", None

    guid, name = chosen
    try:
        p2 = subprocess.run(["powercfg", "/setactive", guid], capture_output=True, text=True, timeout=8.0)
        if p2.returncode != 0:
            err = (p2.stderr or p2.stdout or "").strip()
            return False, f"Failed to set power plan '{name}': {err}", guid
    except Exception as e:
        return False, f"Failed to set power plan: {e}", guid

    return True, f"Set power plan to '{name}'.", guid


def _get_windows_active_power_plan() -> tuple[str | None, str | None, str]:
    """Return active power scheme as (guid, name, err)."""
    if platform.system() != "Windows":
        return None, None, "Power plan state is only available on Windows agents."
    try:
        p = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True, timeout=8.0)
    except Exception as e:
        return None, None, str(e)

    text_out = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
    m = re.search(r"Power Scheme GUID:\s*([0-9a-fA-F\-]{36})\s*\(([^\)]+)\)", text_out)
    if not m:
        return None, None, text_out[:200]
    return m.group(1).lower(), m.group(2).strip(), ""


def _within_tolerance(actual: int | None, target: int, tolerance: int = 3) -> bool:
    if actual is None:
        return False
    try:
        return abs(int(actual) - int(target)) <= max(0, int(tolerance))
    except Exception:
        return False


def _open_windows_settings(uri: str) -> None:
    if platform.system() != "Windows":
        return
    u = (uri or "").strip()
    if not u:
        return
    try:
        subprocess.run(["cmd.exe", "/d", "/s", "/c", "start", "", u], timeout=5.0)
    except Exception:
        pass


def _set_windows_clipboard(text: str) -> tuple[bool, str]:
    if platform.system() != "Windows":
        return False, "Clipboard is only supported on Windows agents."
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
            input=(text or ""),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "").strip() or "Failed to set clipboard"
        return True, ""
    except Exception as e:
        return False, str(e)


def _get_windows_clipboard() -> tuple[str | None, str]:
    if platform.system() != "Windows":
        return None, "Clipboard is only supported on Windows agents."
    rc, out, err = _run_powershell("Get-Clipboard -Raw", timeout_s=5.0)
    if rc != 0:
        return None, (err or out or "").strip() or "Failed to read clipboard"
    return (out or "").rstrip("\r\n"), ""


def _set_windows_wifi(enabled: bool) -> tuple[bool, str]:
    """Best-effort toggle Wi-Fi adapter. Often requires admin privileges."""
    if platform.system() != "Windows":
        return False, "Wi-Fi control is only supported on Windows agents."
    ps = r"""
$ad = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
  ($_.Name -match 'wi-?fi|wireless') -or ($_.InterfaceDescription -match 'wi-?fi|wireless')
} | Select-Object -First 1
if (-not $ad) { Write-Output 'NO_ADAPTER'; exit 2 }
$name = $ad.Name
"""
    ps += ("Enable-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop" if enabled else "Disable-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop")
    rc, out, err = _run_powershell(ps, timeout_s=12.0)
    if rc != 0:
        msg = (err or out or "").strip()
        if "NO_ADAPTER" in (out or ""):
            msg = "No Wi-Fi adapter found."
        return False, msg or "Failed to toggle Wi-Fi"
    return True, ""


def _set_windows_wifi_netsh(enabled: bool) -> tuple[bool, str]:
    """Toggle Wi-Fi using netsh interface command, with adapter name probing."""
    if platform.system() != "Windows":
        return False, "Wi-Fi control is only supported on Windows agents."
    state = "enabled" if enabled else "disabled"
    candidates = ["Wi-Fi", "WiFi", "WLAN", "Wireless Network Connection"]
    errors = []
    for name in candidates:
        try:
            p = subprocess.run(
                ["netsh", "interface", "set", "interface", f'name={name}', f"admin={state}"],
                capture_output=True,
                text=True,
                timeout=8.0,
            )
            if int(p.returncode) == 0:
                return True, ""
            err = (p.stderr or p.stdout or "").strip()
            if err:
                errors.append(f"{name}: {err}")
        except Exception as e:
            errors.append(f"{name}: {e}")
    joined = " | ".join(errors[:3]).strip()
    return False, joined or "Failed to toggle Wi-Fi via netsh"


def _set_windows_bluetooth(enabled: bool) -> tuple[bool, str]:
    """Best-effort Bluetooth toggle. Commonly requires admin privileges; falls back to Settings."""
    if platform.system() != "Windows":
        return False, "Bluetooth control is only supported on Windows agents."

    # Attempt to enable/disable Bluetooth PnP devices.
    ps = r"""
$dev = Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object {
  $_.Status -ne 'Unknown'
} | Select-Object -First 1
if (-not $dev) { Write-Output 'NO_BT'; exit 2 }
$id = $dev.InstanceId
"""
    ps += ("Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop" if enabled else "Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop")
    rc, out, err = _run_powershell(ps, timeout_s=12.0)
    if rc != 0:
        msg = (err or out or "").strip()
        if "NO_BT" in (out or ""):
            msg = "No Bluetooth device found."
        return False, msg or "Failed to toggle Bluetooth"
    return True, ""


def _get_windows_wifi_enabled() -> tuple[bool | None, str]:
    if platform.system() != "Windows":
        return None, "Wi-Fi state is only available on Windows agents."

    ps = r"""
$ad = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
  ($_.Name -match 'wi-?fi|wireless') -or ($_.InterfaceDescription -match 'wi-?fi|wireless')
} | Select-Object -First 1
if (-not $ad) { Write-Output 'NO_ADAPTER'; exit 2 }
Write-Output $ad.Status
"""
    rc, out, err = _run_powershell(ps, timeout_s=8.0)
    if rc == 0:
        status = str(out or "").strip().lower()
        if status:
            return status == "up", ""
    # Fallback: netsh state check.
    try:
        p = subprocess.run(["netsh", "interface", "show", "interface"], capture_output=True, text=True, timeout=8.0)
        if int(p.returncode) == 0:
            txt = str(p.stdout or "")
            for raw_line in txt.splitlines():
                line = raw_line.strip()
                ll = line.lower()
                if not line or "admin state" in ll:
                    continue
                if ("wi-fi" in ll) or ("wifi" in ll) or ("wireless" in ll) or ("wlan" in ll):
                    up = ll.startswith("enabled")
                    return up, ""
    except Exception as e:
        return None, str(e)

    msg = (err or out or "").strip()
    if "NO_ADAPTER" in str(out or ""):
        msg = "No Wi-Fi adapter found."
    return None, msg or "Unable to read Wi-Fi state"


def _get_windows_bluetooth_enabled() -> tuple[bool | None, str]:
    if platform.system() != "Windows":
        return None, "Bluetooth state is only available on Windows agents."

    ps = r"""
$dev = Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | Where-Object {
  $_.Status -ne 'Unknown'
} | Select-Object -First 1
if (-not $dev) { Write-Output 'NO_BT'; exit 2 }
Write-Output $dev.Status
"""
    rc, out, err = _run_powershell(ps, timeout_s=8.0)
    if rc != 0:
        msg = (err or out or "").strip()
        if "NO_BT" in str(out or ""):
            msg = "No Bluetooth device found."
        return None, msg or "Unable to read Bluetooth state"

    status = str(out or "").strip().lower()
    if not status:
        return None, "Unable to read Bluetooth state"
    return status in {"ok", "up", "started"}, ""


def _find_nircmd_exe() -> str | None:
    p = shutil.which("nircmd") or shutil.which("nircmd.exe")
    if p:
        return p
    program_files = [
        os.getenv("ProgramFiles", r"C:\Program Files"),
        os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    for root in program_files:
        if not root:
            continue
        cand = Path(root) / "NirCmd" / "nircmd.exe"
        if cand.exists():
            return str(cand)
    return None


def _run_nircmd(args: list[str], timeout_s: float = 5.0) -> tuple[bool, str]:
    exe = _find_nircmd_exe()
    if not exe:
        return False, "nircmd not found"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout_s)
        if int(p.returncode) != 0:
            return False, (p.stderr or p.stdout or "").strip() or "nircmd command failed"
        return True, ""
    except Exception as e:
        return False, str(e)


def _action_mappings_file_path() -> Path:
    if ACTION_MAPPINGS_FILE:
        return ACTION_MAPPINGS_FILE
    if platform.system() == "Windows":
        local_app_data = str(os.getenv("LOCALAPPDATA") or "").strip()
        if local_app_data:
            return (Path(local_app_data) / "Jarvis" / "agent_action_mappings.json").resolve()
    return (Path.home() / ".jarvis" / "agent_action_mappings.json").resolve()


def _normalize_device_action_name(name: str) -> str:
    n = str(name or "").strip().lower()
    n = n.replace("-", "_").replace(" ", "_")
    n = re.sub(r"[^a-z0-9_]+", "", n)
    n = re.sub(r"_+", "_", n).strip("_")
    return n


def _build_semantic_action_aliases() -> dict[str, str]:
    aliases = {
        "bluetooth_on": "bluetooth_on",
        "bluetooth_off": "bluetooth_off",
        "turn_on_bluetooth": "bluetooth_on",
        "enable_bluetooth": "bluetooth_on",
        "turn_off_bluetooth": "bluetooth_off",
        "disable_bluetooth": "bluetooth_off",
        "wifi_on": "wifi_on",
        "wifi_off": "wifi_off",
        "wi_fi_on": "wifi_on",
        "wi_fi_off": "wifi_off",
        "turn_on_wifi": "wifi_on",
        "enable_wifi": "wifi_on",
        "turn_off_wifi": "wifi_off",
        "disable_wifi": "wifi_off",
        "volume_up": "volume_up",
        "volume_down": "volume_down",
        "increase_volume": "volume_up",
        "decrease_volume": "volume_down",
        "volume_mute": "volume_mute",
        "volume_unmute": "volume_unmute",
        "mute": "volume_mute",
        "unmute": "volume_unmute",
        "brightness_up": "brightness_up",
        "brightness_down": "brightness_down",
        "increase_brightness": "brightness_up",
        "decrease_brightness": "brightness_down",
        "lock_screen": "lock_screen",
        "lock": "lock_screen",
        "sleep": "sleep",
        "shutdown": "shutdown",
        "restart": "restart",
        "open_app": "open_app",
        "close_app": "close_app",
    }
    return { _normalize_device_action_name(k): v for k, v in aliases.items() }


_SEMANTIC_DEVICE_ACTION_ALIASES = _build_semantic_action_aliases()


def _load_learned_action_mappings() -> dict[str, dict]:
    try:
        p = _action_mappings_file_path()
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8") or "{}")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            nk = _normalize_device_action_name(str(k or ""))
            if not nk:
                continue
            out[nk] = {
                "resolved_action": _normalize_device_action_name(str(v.get("resolved_action") or "")),
                "confidence": float(v.get("confidence") or 0.0),
                "usage_count": int(v.get("usage_count") or 0),
                "success_count": int(v.get("success_count") or 0),
                "failure_count": int(v.get("failure_count") or 0),
                "last_used_at": str(v.get("last_used_at") or ""),
            }
        return out
    except Exception:
        return {}


def _save_learned_action_mappings(mappings: dict[str, dict]) -> None:
    try:
        if not isinstance(mappings, dict):
            return
        ordered = sorted(
            mappings.items(),
            key=lambda kv: (
                float((kv[1] or {}).get("confidence") or 0.0),
                int((kv[1] or {}).get("success_count") or 0),
                int((kv[1] or {}).get("usage_count") or 0),
            ),
            reverse=True,
        )
        trimmed = dict(ordered[:MAX_LEARNED_ACTION_MAPPINGS])
        p = _action_mappings_file_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")
    except Exception:
        return


def _resolve_category_action(normalized_action: str, params: dict) -> str:
    if normalized_action in DEVICE_ACTION_NAMES:
        return normalized_action

    s = normalized_action
    categories = {
        "wifi": ("wifi", "wi_fi", "wireless", "wlan"),
        "bluetooth": ("bluetooth", "bt"),
        "volume": ("volume", "audio", "sound", "speaker", "mute", "unmute"),
        "brightness": ("brightness", "display", "screen_light"),
        "system": ("lock", "sleep", "shutdown", "restart", "reboot", "power"),
        "app": ("open_app", "close_app", "launch_app", "kill_app", "switch_app", "application", "app"),
    }

    cat = ""
    for key, toks in categories.items():
        if any(tok in s for tok in toks):
            cat = key
            break

    if not cat:
        return ""

    on = any(x in s for x in ("_on", "enable", "turn_on", "start"))
    off = any(x in s for x in ("_off", "disable", "turn_off", "stop"))
    up = any(x in s for x in ("_up", "increase", "raise", "higher"))
    down = any(x in s for x in ("_down", "decrease", "lower"))

    if cat == "wifi":
        if on:
            return "wifi_on"
        if off:
            return "wifi_off"
    if cat == "bluetooth":
        if on:
            return "bluetooth_on"
        if off:
            return "bluetooth_off"
    if cat == "volume":
        if "unmute" in s:
            return "volume_unmute"
        if "mute" in s:
            return "volume_mute"
        if up:
            return "volume_up"
        if down:
            return "volume_down"
    if cat == "brightness":
        if up:
            return "brightness_up"
        if down:
            return "brightness_down"
    if cat == "system":
        if "lock" in s:
            return "lock_screen"
        if "sleep" in s:
            return "sleep"
        if "restart" in s or "reboot" in s:
            return "restart"
        if "shutdown" in s or "power_off" in s:
            return "shutdown"
    if cat == "app":
        if "close" in s or "kill" in s or "exit" in s:
            return "close_app"
        if "open" in s or "launch" in s or "start" in s:
            return "open_app"

    # Intent-like params can disambiguate semantic variants.
    try:
        direction = _normalize_device_action_name(str((params or {}).get("direction") or ""))
        if cat == "volume":
            if direction in {"up", "increase"}:
                return "volume_up"
            if direction in {"down", "decrease"}:
                return "volume_down"
        if cat == "brightness":
            if direction in {"up", "increase"}:
                return "brightness_up"
            if direction in {"down", "decrease"}:
                return "brightness_down"
    except Exception:
        pass
    return ""


def _select_learned_mapping(normalized_action: str, mappings: dict[str, dict]) -> str:
    item = mappings.get(normalized_action) if isinstance(mappings.get(normalized_action), dict) else None
    if not item:
        return ""
    resolved = _normalize_device_action_name(str(item.get("resolved_action") or ""))
    if resolved not in DEVICE_ACTION_NAMES:
        return ""
    conf = float(item.get("confidence") or 0.0)
    if conf < MIN_LEARNED_MAPPING_CONFIDENCE:
        return ""
    return resolved


def _update_learned_mapping(
    *,
    original_action: str,
    resolved_action: str,
    succeeded: bool,
    mappings: dict[str, dict],
) -> None:
    key = _normalize_device_action_name(original_action)
    if not key:
        return
    if resolved_action not in DEVICE_ACTION_NAMES:
        return
    item = mappings.get(key) if isinstance(mappings.get(key), dict) else {}
    usage = int(item.get("usage_count") or 0) + 1
    succ = int(item.get("success_count") or 0) + (1 if succeeded else 0)
    fail = int(item.get("failure_count") or 0) + (0 if succeeded else 1)
    conf = float(succ) / float(max(1, usage))
    mappings[key] = {
        "resolved_action": resolved_action,
        "confidence": round(conf, 4),
        "usage_count": usage,
        "success_count": succ,
        "failure_count": fail,
        "last_used_at": _now_utc_iso(),
    }

    # Prune stale low-confidence entries when repeatedly failing.
    if fail >= 3 and conf < MIN_LEARNED_MAPPING_CONFIDENCE:
        try:
            del mappings[key]
        except Exception:
            pass


_LEARNED_DEVICE_ACTION_MAPPINGS = _load_learned_action_mappings()


def resolve_and_execute(action: str, params: dict) -> dict:
    """Central intelligent resolver for device actions with learning and fallback tiers."""
    original_action = str(action or "")
    normalized = _normalize_device_action_name(original_action)
    p = params if isinstance(params, dict) else {}

    fallback_used = False
    verification = {
        "attempted": False,
        "state_changed": None,
        "note": "",
    }

    resolved = ""
    tier = ""

    learned = _select_learned_mapping(normalized, _LEARNED_DEVICE_ACTION_MAPPINGS)
    if learned:
        resolved = learned
        tier = "learned"

    if not resolved:
        aliased = _SEMANTIC_DEVICE_ACTION_ALIASES.get(normalized) or ""
        if aliased in DEVICE_ACTION_NAMES:
            resolved = aliased
            tier = "direct" if normalized == resolved else "alias"

    if not resolved:
        by_category = _resolve_category_action(normalized, p)
        if by_category:
            resolved = by_category
            tier = "category"
            fallback_used = True

    if not resolved:
        return {
            "success": False,
            "original_action": original_action,
            "resolved_action": "",
            "message": "Unsupported device action.",
            "error": f"Unable to resolve action '{original_action}'",
            "verification": verification,
            "fallback_used": True,
        }

    before_state = None
    after_state = None

    try:
        if resolved in {"wifi_on", "wifi_off"}:
            verification["attempted"] = True
            before_state, _ = _get_windows_wifi_enabled()
        elif resolved in {"bluetooth_on", "bluetooth_off"}:
            verification["attempted"] = True
            before_state, _ = _get_windows_bluetooth_enabled()
        elif resolved in {"volume_up", "volume_down"}:
            verification["attempted"] = True
            before_state, _ = _get_windows_volume_percent()
        elif resolved in {"volume_mute", "volume_unmute"}:
            verification["attempted"] = True
            before_state, _ = _get_windows_mute()
        elif resolved in {"brightness_up", "brightness_down"}:
            verification["attempted"] = True
            before_state = _get_windows_brightness()
    except Exception:
        before_state = None

    base = handle_device_action(resolved, p)
    ok = bool(base.get("success"))

    try:
        if verification["attempted"]:
            if resolved in {"wifi_on", "wifi_off"}:
                after_state, state_err = _get_windows_wifi_enabled()
                target_state = resolved.endswith("_on")
                if (before_state is not None) and (after_state is not None):
                    changed = bool(before_state != after_state)
                    reached_target = bool(after_state == target_state)
                    verification["state_changed"] = bool(changed and reached_target)
                    verification["note"] = f"before={before_state}, after={after_state}, target={target_state}"
                    ok = bool(ok and changed and reached_target)
                    if not ok and not base.get("error"):
                        base["error"] = "wifi_state_not_changed"
                        base["message"] = "Wi-Fi state did not change to requested target."
                else:
                    verification["state_changed"] = False
                    verification["note"] = f"state_unverified:{state_err or 'unknown'}"
                    ok = False
                    if not base.get("error"):
                        base["error"] = "wifi_state_unverified"
                        base["message"] = "Unable to verify Wi-Fi state after execution."
            elif resolved in {"bluetooth_on", "bluetooth_off"}:
                after_state, state_err = _get_windows_bluetooth_enabled()
                target_state = resolved.endswith("_on")
                if (before_state is not None) and (after_state is not None):
                    changed = bool(before_state != after_state)
                    reached_target = bool(after_state == target_state)
                    verification["state_changed"] = bool(changed and reached_target)
                    verification["note"] = f"before={before_state}, after={after_state}, target={target_state}"
                    ok = bool(ok and changed and reached_target)
                    if not ok and not base.get("error"):
                        base["error"] = "bluetooth_state_not_changed"
                        base["message"] = "Bluetooth state did not change to requested target."
                else:
                    verification["state_changed"] = False
                    verification["note"] = f"state_unverified:{state_err or 'unknown'}"
                    ok = False
                    if not base.get("error"):
                        base["error"] = "bluetooth_state_unverified"
                        base["message"] = "Unable to verify Bluetooth state after execution."
            elif resolved in {"volume_up", "volume_down"}:
                after_state, _ = _get_windows_volume_percent()
            elif resolved in {"volume_mute", "volume_unmute"}:
                after_state, _ = _get_windows_mute()
            elif resolved in {"brightness_up", "brightness_down"}:
                after_state = _get_windows_brightness()

            if resolved in {"wifi_on", "wifi_off", "bluetooth_on", "bluetooth_off"}:
                pass
            elif (before_state is not None) and (after_state is not None):
                verification["state_changed"] = bool(before_state != after_state)
                verification["note"] = f"before={before_state}, after={after_state}"
            elif ok:
                verification["state_changed"] = True
                verification["note"] = "command_succeeded"
            else:
                verification["state_changed"] = False
                verification["note"] = "command_failed"
    except Exception:
        pass

    _update_learned_mapping(
        original_action=normalized,
        resolved_action=resolved,
        succeeded=ok,
        mappings=_LEARNED_DEVICE_ACTION_MAPPINGS,
    )
    _save_learned_action_mappings(_LEARNED_DEVICE_ACTION_MAPPINGS)

    return {
        "success": ok,
        "original_action": original_action,
        "resolved_action": resolved,
        "message": str(base.get("message") or ""),
        "error": base.get("error"),
        "verification": verification,
        "fallback_used": bool(fallback_used or tier in {"learned", "alias", "category"}),
        "resolution_tier": tier,
        "result": base,
    }


def _device_action_payload(success: bool, action_name: str, message: str, error: str | None = None, **extra) -> dict:
    payload = {
        "success": bool(success),
        "action": str(action_name or "").strip(),
        "message": str(message or "").strip(),
        "error": (None if success else str(error or message or "execution_failed")),
    }
    payload.update(extra)
    return payload


def handle_device_action(action: str, params: dict) -> dict:
    """Central dispatcher for device_action names with structured success/error payloads."""
    action_name = str(action or "").strip().lower()
    p = params if isinstance(params, dict) else {}

    def _done(ok: bool, message: str, error: str | None = None, **extra) -> dict:
        if ok:
            print(f"[DEVICE_ACTION] {action_name} -> success", flush=True)
        else:
            print(f"[DEVICE_ACTION] {action_name} -> failed: {error or message}", flush=True)
        return _device_action_payload(ok, action_name, message, error=error, **extra)

    try:
        if action_name in ("bluetooth_on", "bluetooth_off"):
            ok, err = _set_windows_bluetooth(action_name == "bluetooth_on")
            if not ok:
                return _done(False, "Failed to toggle Bluetooth.", err)
            return _done(True, f"Bluetooth {'enabled' if action_name.endswith('_on') else 'disabled'}.")

        if action_name in ("wifi_on", "wifi_off"):
            enabled = action_name == "wifi_on"
            ok, err = _set_windows_wifi_netsh(enabled)
            if not ok:
                ok, err = _set_windows_wifi(enabled)
            if not ok:
                return _done(False, "Failed to toggle Wi-Fi.", err)
            return _done(True, f"Wi-Fi {'enabled' if enabled else 'disabled'}.")

        if action_name in ("volume_up", "volume_down"):
            try:
                step = int(float(p.get("step", 10)))
            except Exception:
                step = 10
            step = max(1, min(30, abs(step)))
            if action_name == "volume_down":
                step = -step

            current, err_cur = _get_windows_volume_percent()
            if current is not None:
                target = max(0, min(100, int(current + step)))
                ok, err = _set_windows_volume_percent(target)
                if not ok:
                    return _done(False, "Failed to adjust volume.", err)
                after, _ = _get_windows_volume_percent()
                return _done(True, "Volume adjusted.", before_value=current, after_value=after, step=step)

            # pycaw unavailable: nircmd fallback (65535 scale)
            unit_step = int(65535 * (abs(step) / 100.0))
            if unit_step <= 0:
                unit_step = 655
            if step < 0:
                unit_step = -unit_step
            ok, err = _run_nircmd(["changesysvolume", str(unit_step)])
            if not ok:
                return _done(False, "Failed to adjust volume.", err or err_cur)
            return _done(True, "Volume adjusted.", step=step)

        if action_name in ("volume_mute", "volume_unmute"):
            muted = action_name == "volume_mute"
            ok, err = _set_windows_mute(muted)
            if not ok:
                ok2, err2 = _run_nircmd(["mutesysvolume", "1" if muted else "0"])
                if not ok2:
                    return _done(False, "Failed to change mute state.", err or err2)
            return _done(True, f"Volume {'muted' if muted else 'unmuted'}.", muted=muted)

        if action_name in ("brightness_up", "brightness_down"):
            current = _get_windows_brightness()
            if current is None:
                return _done(False, "Failed to read brightness.", "Current brightness unavailable")
            try:
                step = int(float(p.get("step", 10)))
            except Exception:
                step = 10
            step = max(1, min(40, abs(step)))
            target = current + step if action_name == "brightness_up" else current - step
            target = max(0, min(100, int(target)))
            ok, err = _set_windows_brightness(target)
            if not ok:
                return _done(False, "Failed to adjust brightness.", err)
            after = _get_windows_brightness()
            return _done(True, "Brightness adjusted.", before_value=current, after_value=after, value=target)

        if action_name == "lock_screen":
            if platform.system() != "Windows":
                return _done(False, "Lock screen is unsupported on this OS.", "Windows only")
            subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=5.0)
            return _done(True, "Screen locked.")

        if action_name == "sleep":
            if platform.system() != "Windows":
                return _done(False, "Sleep is unsupported on this OS.", "Windows only")
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            return _done(True, "System entering sleep.")

        if action_name == "shutdown":
            if platform.system() != "Windows":
                return _done(False, "Shutdown is unsupported on this OS.", "Windows only")
            subprocess.Popen(["shutdown", "/s", "/t", "0"])
            return _done(True, "System shutdown initiated.")

        if action_name == "restart":
            if platform.system() != "Windows":
                return _done(False, "Restart is unsupported on this OS.", "Windows only")
            subprocess.Popen(["shutdown", "/r", "/t", "0"])
            return _done(True, "System restart initiated.")

        if action_name in ("open_app", "close_app"):
            if not ALLOW_APP_CONTROL:
                return _done(False, "App control disabled on agent.", "App control disabled")
            mgr = _get_app_manager()
            if not mgr:
                return _done(False, "App manager not available on this agent.", "App manager unavailable")
            app_name = p.get("app_name") or p.get("app") or p.get("name") or ""
            app_name = str(app_name).strip()
            if not app_name:
                return _done(False, "app_name is required.", "Missing app_name")

            if action_name == "open_app":
                n = app_name.lower()
                now = time.monotonic()
                last = _RECENT_APP_OPENS.get(n)
                if last is not None and (now - last) < 4.0:
                    return _done(True, "Skipped duplicate open_app.", duplicate=True, app=app_name)
                _RECENT_APP_OPENS[n] = now
                app_result = mgr.open_app(app_name, p.get("args") or [])
            else:
                app_result = mgr.close_app(app_name)

            if isinstance(app_result, dict):
                st = str(app_result.get("status") or "").strip().lower()
                msg = str(app_result.get("message") or "").strip() or f"{action_name} executed"
                if st in {"error", "failed", "forbidden"}:
                    return _done(False, msg, msg, app=app_name)
                return _done(True, msg, app=app_name)

            return _done(True, f"{action_name} executed.", app=app_name)

        return _done(False, "Unsupported device action.", f"Unsupported action '{action_name}'")
    except Exception as e:
        return _done(False, "Device action failed.", str(e))


def _confirmation_required(action: dict) -> bool:
    return not bool((action or {}).get("confirm", False))


def _as_ms_settings_uri(x: str) -> str | None:
    s = str(x or "").strip()
    if not s:
        return None
    if s.startswith("ms-settings:"):
        return s
    # Allow shorthand like "display" or "notifications".
    if re.match(r"^[a-z0-9\-]+$", s.lower()):
        return "ms-settings:" + s.lower()
    return None


async def _execute_action(action: dict) -> dict:
    global ALLOW_APP_CONTROL, ALLOW_EXECUTE_COMMAND, ALLOW_FILE_OPS, ALLOW_SCREEN, ALLOW_SELF_UPDATE
    t = (action or {}).get("type")

    if t == "device_action":
        name = (action or {}).get("name") or (action or {}).get("action") or (action or {}).get("device_action") or ""
        name = str(name or "").strip().lower()
        if not name or name == "device_action":
            return {"status": "error", "action_type": "device_action", "message": "Invalid device action name"}

        params = {}
        for key in ("params", "args"):
            raw = (action or {}).get(key)
            if isinstance(raw, dict):
                params.update(raw)
        if isinstance(action, dict):
            for k, v in action.items():
                if k in {"type", "name", "action", "params", "args"}:
                    continue
                params.setdefault(k, v)

        dres = resolve_and_execute(name, params)

        # Compatibility fallback: some callers wrap native action types inside
        # type=device_action (e.g. set_volume/open_url/set_wifi). If the
        # semantic resolver cannot map them, dispatch by direct action type.
        if not bool(dres.get("success")):
            normalized_name = _normalize_device_action_name(name)
            direct_type = normalized_name
            if direct_type and direct_type != "device_action" and direct_type in set(_supported_actions_catalog()):
                direct_action = {"type": direct_type, **params}
                direct_res = await _execute_action(direct_action)
                direct_status = _normalize_contract_status(str((direct_res or {}).get("status") or ""))
                direct_ok = bool(direct_status == "completed" or bool((direct_res or {}).get("success")))
                return {
                    "status": "success" if direct_ok else "error",
                    "action_type": "device_action",
                    "device_action": direct_type,
                    "message": str((direct_res or {}).get("message") or ("" if direct_ok else "Device action execution failed.")),
                    "error": None if direct_ok else (direct_res or {}).get("error") or (direct_res or {}).get("message"),
                    "result": direct_res,
                }

        return {
            "status": "success" if bool(dres.get("success")) else "error",
            "action_type": "device_action",
            "device_action": str(dres.get("resolved_action") or name),
            "message": str(dres.get("message") or ""),
            "error": dres.get("error"),
            "result": dres,
        }

    if t in DEVICE_ACTION_NAMES:
        params = {}
        if isinstance(action, dict):
            for k, v in action.items():
                if k == "type":
                    continue
                params[k] = v
        dres = resolve_and_execute(str(t), params)
        return {
            "status": "success" if bool(dres.get("success")) else "error",
            "action_type": "device_action",
            "device_action": str(dres.get("resolved_action") or t),
            "message": str(dres.get("message") or ""),
            "error": dres.get("error"),
            "result": dres,
        }

    if t == "list_device_actions":
        # Read-only: returns a catalog of common supported universal actions.
        # Capability gating still applies when executing the actual action.
        return {
            "status": "success",
            "action_type": t,
            "actions": _supported_actions_catalog(),
        }

    if t == "inspect_system_state":
        return {
            "status": "success",
            "action_type": t,
            "state": _inspect_system_state(),
        }

    if t == "monitor_performance":
        samples = int((action or {}).get("samples") or 5)
        interval = float((action or {}).get("interval") or 0.5)
        samples = max(1, min(30, samples))
        interval = max(0.1, min(5.0, interval))

        if psutil is None:
            return {"status": "error", "action_type": t, "message": "psutil not available"}

        data = []
        for _ in range(samples):
            data.append(
                {
                    "cpu_percent": psutil.cpu_percent(interval=interval),
                    "memory_percent": psutil.virtual_memory().percent,
                }
            )
        return {"status": "success", "action_type": t, "samples": data}

    if t == "analyze_screen":
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}
        try:
            text = sa.read_screen_text()
            return {
                "status": "success",
                "action_type": t,
                "text_excerpt": (text or "")[:1200],
                "length": len(text or ""),
            }
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if ACTION_ALLOWLIST is not None and t not in ACTION_ALLOWLIST:
        return {"status": "forbidden", "action_type": t, "message": "Action blocked by agent allowlist"}

    if t == "find_files":
        if not ALLOW_FILE_OPS:
            return {"status": "forbidden", "action_type": t, "message": "File ops disabled on agent"}

        query = str((action or {}).get("query") or (action or {}).get("name") or "").strip()
        if not query:
            return {"status": "error", "action_type": t, "message": "Provide query"}

        try:
            max_results = int((action or {}).get("max_results", 25))
        except Exception:
            max_results = 25
        max_results = max(1, min(200, max_results))

        exts = (action or {}).get("extensions") or (action or {}).get("ext") or None
        norm_exts: set[str] | None = None
        if isinstance(exts, (list, tuple)):
            norm_exts = set()
            for e in exts:
                s = str(e or "").strip().lower()
                if not s:
                    continue
                if not s.startswith("."):
                    s = "." + s
                norm_exts.add(s)
            if not norm_exts:
                norm_exts = None

        root = (action or {}).get("root") or (action or {}).get("path") or None
        roots: list[Path] = []
        if root:
            if not _is_path_allowed(str(root)):
                return {"status": "forbidden", "action_type": t, "message": "Root path is not allowed", "root": str(root)}
            rp = (Path(str(root)) if Path(str(root)).is_absolute() else (PROJECT_ROOT / str(root))).resolve()
            roots = [rp]
        else:
            roots = list(ALLOWED_ROOTS)

        q = query.lower()
        results: list[dict] = []
        try:
            for base in roots:
                if not base.exists() or not base.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(base):
                    # Prune blocked directories.
                    dirnames[:] = [d for d in dirnames if d not in BLOCKED_DIRS]
                    for fn in filenames:
                        if fn in BLOCKED_FILES:
                            continue
                        if norm_exts is not None:
                            if Path(fn).suffix.lower() not in norm_exts:
                                continue
                        if q not in fn.lower():
                            continue
                        p = (Path(dirpath) / fn).resolve()
                        # Must remain inside allowed roots.
                        if not _is_path_allowed(str(p)):
                            continue
                        try:
                            rel = p.relative_to(PROJECT_ROOT)
                            rel_s = str(rel).replace("\\", "/")
                        except Exception:
                            rel_s = str(p)
                        results.append({"path": rel_s, "name": fn})
                        if len(results) >= max_results:
                            raise StopIteration
        except StopIteration:
            pass
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

        return {"status": "success", "action_type": t, "query": query, "count": len(results), "results": results}

    if t == "save_screenshot":
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}

        reg = (action or {}).get("region")
        region = None
        if isinstance(reg, dict):
            try:
                rx = int(reg.get("x", 0))
                ry = int(reg.get("y", 0))
                rw = int(reg.get("width", 0))
                rh = int(reg.get("height", 0))
                if rw > 0 and rh > 0:
                    region = (rx, ry, rw, rh)
            except Exception:
                region = None

        rel_path = str((action or {}).get("path") or (action or {}).get("file") or "").strip()
        if not rel_path:
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            rel_path = f"data/tmp/screenshot_{ts}.png"

        # Force png extension (stable across environments)
        if not rel_path.lower().endswith(".png"):
            rel_path = rel_path + ".png"

        if not _is_path_allowed(rel_path):
            return {"status": "forbidden", "action_type": t, "message": "Path is not allowed", "path": rel_path}

        p = (Path(rel_path) if Path(rel_path).is_absolute() else (PROJECT_ROOT / rel_path)).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)

        try:
            img = sa.capture_screen(region=region)
            if not img:
                return {"status": "error", "action_type": t, "message": "Failed to capture screen"}
            img.save(str(p), format="PNG")
            try:
                rel_out = str(p.relative_to(PROJECT_ROOT)).replace("\\", "/")
            except Exception:
                rel_out = str(p)
            return {
                "status": "success",
                "action_type": t,
                "path": rel_out,
                "width": getattr(img, "width", None),
                "height": getattr(img, "height", None),
            }
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t in ("alt_tab", "switch_window"):
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}
        try:
            count = int((action or {}).get("count", 1))
        except Exception:
            count = 1
        count = max(1, min(20, count))
        ok_all = True
        for _ in range(count):
            ok_all = bool(sa.hotkey("alt+tab")) and ok_all
            await asyncio.sleep(0.18)
        return {"status": "success" if ok_all else "error", "action_type": "alt_tab", "count": count}

    if t in ("read", "write", "edit", "delete", "move", "list", "mkdir", "copy", "cleanup"):
        if not ALLOW_FILE_OPS:
            return {"status": "forbidden", "action_type": t, "message": "File ops disabled on agent"}

        # normalize paths
        src = action.get("path") or action.get("source") or ""
        dst = action.get("dest") or action.get("destination") or ""

        if t in ("read", "write", "edit", "delete", "mkdir", "list"):
            if not _is_path_allowed(src):
                return {"status": "forbidden", "action_type": t, "path": src}

        if t in ("move", "copy"):
            if not (_is_path_allowed(src) and _is_path_allowed(dst)):
                return {"status": "forbidden", "action_type": t, "source": src, "destination": dst}

        try:
            if t == "read":
                p = Path(src) if Path(src).is_absolute() else (PROJECT_ROOT / src)
                rp = p.resolve()
                if not rp.exists() or not rp.is_file():
                    return {"status": "error", "message": f"File not found: {src}"}
                return {"status": "success", "path": str(rp), "content": _read_text_file(rp)}

            if t in ("write", "edit"):
                content = action.get("content", "")
                p = Path(src) if Path(src).is_absolute() else (PROJECT_ROOT / src)
                rp = p.resolve()
                existed = rp.exists()
                _write_text_file(rp, content)
                return {"status": "edited" if existed else "written", "path": str(rp)}

            if t == "delete":
                p = Path(src) if Path(src).is_absolute() else (PROJECT_ROOT / src)
                rp = p.resolve()
                if rp.exists() and rp.is_file():
                    rp.unlink()
                    return {"status": "deleted", "path": str(rp)}
                return {"status": "not_found", "path": str(rp)}

            if t == "mkdir":
                p = Path(src) if Path(src).is_absolute() else (PROJECT_ROOT / src)
                rp = p.resolve()
                rp.mkdir(parents=True, exist_ok=True)
                return {"status": "success", "path": str(rp)}

            if t == "list":
                p = Path(src) if Path(src).is_absolute() else (PROJECT_ROOT / src)
                rp = p.resolve()
                if not rp.exists() or not rp.is_dir():
                    return {"status": "error", "message": f"Directory not found: {src}"}
                items = []
                for it in rp.iterdir():
                    items.append({"name": it.name, "type": "directory" if it.is_dir() else "file"})
                return {"status": "success", "path": str(rp), "items": items}

            if t == "move":
                sp = Path(src) if Path(src).is_absolute() else (PROJECT_ROOT / src)
                dp = Path(dst) if Path(dst).is_absolute() else (PROJECT_ROOT / dst)
                srp, drp = sp.resolve(), dp.resolve()
                drp.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(srp), str(drp))
                return {"status": "moved", "from": str(srp), "to": str(drp)}

            if t == "copy":
                sp = Path(src) if Path(src).is_absolute() else (PROJECT_ROOT / src)
                dp = Path(dst) if Path(dst).is_absolute() else (PROJECT_ROOT / dst)
                srp, drp = sp.resolve(), dp.resolve()
                drp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(srp), str(drp))
                return {"status": "copied", "from": str(srp), "to": str(drp)}

            if t == "cleanup":
                return _cleanup_project(PROJECT_ROOT)

        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t in ("open_app", "close_app", "switch_app"):
        if not ALLOW_APP_CONTROL:
            return {"status": "forbidden", "action_type": t, "message": "App control disabled on agent"}
        mgr = _get_app_manager()
        if not mgr:
            return {"status": "error", "action_type": t, "message": "App manager not available on this agent"}
        name = action.get("app_name") or action.get("app") or ""
        if t == "open_app":
            n = str(name or "").strip().lower()
            now = asyncio.get_running_loop().time()
            last = _RECENT_APP_OPENS.get(n)
            if last is not None and (now - last) < 4.0:
                return {"status": "skipped", "action_type": t, "app": n, "message": "Skipped duplicate open_app"}
            _RECENT_APP_OPENS[n] = now
            return await asyncio.to_thread(mgr.open_app, name, action.get("args") or [])
        if t == "close_app":
            return await asyncio.to_thread(mgr.close_app, name)
        if t == "switch_app":
            return await asyncio.to_thread(mgr.switch_to_app, name)

    if t == "list_running_apps":
        if not ALLOW_APP_CONTROL:
            return {"status": "forbidden", "action_type": t, "message": "App control disabled on agent"}
        mgr = _get_app_manager()
        if not mgr:
            return {"status": "error", "action_type": t, "message": "App manager not available on this agent"}
        try:
            apps = await asyncio.to_thread(mgr.list_running_apps)
            return {"status": "success", "action_type": t, "apps": apps if isinstance(apps, list) else []}
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t in ("execute_command", "run_command"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "Command execution disabled on agent"}
        mgr = _get_app_manager()
        if not mgr:
            return {"status": "error", "action_type": t, "message": "Command runner not available on this agent"}
        cmd = action.get("command") or ""
        wait = bool(action.get("wait", True))
        if _is_dangerous_command(str(cmd)):
            return {"status": "forbidden", "action_type": t, "message": "Blocked dangerous command (OS/system safety)."}
        return mgr.execute_command(cmd, wait)

    if t == "open_settings":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        uri = _as_ms_settings_uri((action or {}).get("uri") or (action or {}).get("page") or (action or {}).get("settings") or "")
        if not uri:
            return {"status": "error", "action_type": t, "message": "Provide uri like 'ms-settings:display'"}
        _open_windows_settings(uri)
        return {"status": "success", "action_type": t, "uri": uri}

    if t == "lock_screen":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        if platform.system() != "Windows":
            return {"status": "error", "action_type": t, "message": "lock_screen is only supported on Windows agents."}
        try:
            # Locks the current interactive session.
            subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=5.0)
            return {"status": "success", "action_type": t}
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t in ("shutdown", "restart", "sleep", "hibernate", "logoff"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        if platform.system() != "Windows":
            return {"status": "error", "action_type": t, "message": f"{t} is only supported on Windows agents."}
        if _confirmation_required(action):
            return {"status": "error", "action_type": t, "message": "Confirmation required. Re-run with confirm=true."}
        try:
            if t == "shutdown":
                subprocess.Popen(["shutdown", "/s", "/t", "0"])
            elif t == "restart":
                subprocess.Popen(["shutdown", "/r", "/t", "0"])
            elif t == "logoff":
                subprocess.Popen(["shutdown", "/l"])
            elif t == "sleep":
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            elif t == "hibernate":
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "1,1,0"])
            return {"status": "success", "action_type": t}
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t == "open_url":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        url = str((action or {}).get("url") or "").strip()
        if not url:
            return {"status": "error", "action_type": t, "message": "url is required"}
        try:
            subprocess.Popen(["cmd.exe", "/d", "/s", "/c", "start", "", url])
            return {"status": "success", "action_type": t, "url": url}
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t in ("communicate_with_assistant", "communicate_with_google_assistant"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}

        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}

        target = str((action or {}).get("assistant_target") or "").strip().lower()
        if t == "communicate_with_google_assistant" and not target:
            target = "google_assistant"

        target_url_map = {
            "google_assistant": "https://assistant.google.com/",
            "chatgpt": "https://chat.openai.com/",
            "copilot": "https://copilot.microsoft.com/",
            "gemini": "https://gemini.google.com/",
        }
        default_url = target_url_map.get(target or "google_assistant", "https://assistant.google.com/")
        url = str((action or {}).get("url") or default_url).strip() or default_url
        prompt = str((action or {}).get("prompt") or (action or {}).get("text") or "").strip()
        if not prompt:
            prompt = "What can you do to help improve this digital assistant system?"

        try:
            open_wait_ms = int((action or {}).get("open_wait_ms", 1800) or 1800)
        except Exception:
            open_wait_ms = 1800
        try:
            response_wait_ms = int((action or {}).get("response_wait_ms", 2800) or 2800)
        except Exception:
            response_wait_ms = 2800

        open_wait_s = max(0.4, min(8.0, open_wait_ms / 1000.0))
        response_wait_s = max(0.8, min(10.0, response_wait_ms / 1000.0))

        try:
            subprocess.Popen(["cmd.exe", "/d", "/s", "/c", "start", "", url])
        except Exception as e:
            return {"status": "error", "action_type": t, "message": f"Failed to open Assistant URL: {e}"}

        await asyncio.sleep(open_wait_s)

        # Focus browser URL box then navigate directly to target assistant page.
        try:
            _ = bool(sa.hotkey("ctrl+l"))
            await asyncio.sleep(0.15)
            ok_url, err_url = await _type_text_with_timeout(sa, url, interval=0.03, timeout_s=10.0)
            if not ok_url:
                return {
                    "status": "error",
                    "action_type": t,
                    "message": err_url or "Failed to type assistant URL",
                }
            _ = bool(sa.press_key("enter", presses=1))
        except Exception as e:
            return {"status": "error", "action_type": t, "message": f"Failed to focus Assistant tab: {e}"}

        await asyncio.sleep(1.1)

        # Best-effort: type prompt and submit.
        try:
            ok_prompt, err_prompt = await _type_text_with_timeout(sa, prompt, interval=0.04, timeout_s=30.0)
            if not ok_prompt:
                return {
                    "status": "error",
                    "action_type": t,
                    "message": err_prompt or "Failed to type prompt",
                    "prompt": prompt,
                    "assistant_url": url,
                }
            submitted = bool(sa.press_key("enter", presses=1))
        except Exception as e:
            return {"status": "error", "action_type": t, "message": f"Failed to submit prompt: {e}", "prompt": prompt}

        await asyncio.sleep(response_wait_s)

        observed_text = ""
        try:
            observed_text = str(sa.read_screen_text(None) or "").strip()
        except Exception:
            observed_text = ""

        preview = observed_text[:900] if observed_text else ""
        return {
            "status": "success" if submitted else "partial",
            "action_type": t,
            "assistant_target": target or "google_assistant",
            "assistant_url": url,
            "prompt": prompt,
            "submitted": bool(submitted),
            "observed_text_preview": preview,
            "observed_text_chars": len(observed_text),
            "response_summary": preview[:280] if preview else "",
            "message": "Prompt submitted to assistant with best-effort screen text capture.",
        }

    if t == "open_path":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        p = str((action or {}).get("path") or "").strip()
        if not p:
            return {"status": "error", "action_type": t, "message": "path is required"}
        try:
            subprocess.Popen(["explorer.exe", p])
            return {"status": "success", "action_type": t, "path": p}
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t == "get_clipboard":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        val, err = _get_windows_clipboard()
        if val is None:
            return {"status": "error", "action_type": t, "message": err}
        return {"status": "success", "action_type": t, "text": val}

    if t == "set_clipboard":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        text = str((action or {}).get("text") or (action or {}).get("value") or "")
        ok, err = _set_windows_clipboard(text)
        if not ok:
            return {"status": "error", "action_type": t, "message": err}
        return {"status": "success", "action_type": t, "set": len(text)}

    if t == "list_processes":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        if not SYSTEM_OPS_AVAILABLE or not system_ops:
            return {"status": "error", "action_type": t, "message": "system_ops not available"}
        flt = (action or {}).get("filter") or (action or {}).get("name") or None
        try:
            return system_ops.list_processes(filter_name=str(flt) if flt else None)
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t == "screen_info":
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        if not SYSTEM_OPS_AVAILABLE or not system_ops:
            return {"status": "error", "action_type": t, "message": "system_ops not available"}
        try:
            return system_ops.get_screen_info()
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t == "open_windows":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        if not SYSTEM_OPS_AVAILABLE or not system_ops:
            return {"status": "error", "action_type": t, "message": "system_ops not available"}
        try:
            return system_ops.get_open_windows()
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t == "launch_application":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        if not SYSTEM_OPS_AVAILABLE or not system_ops:
            return {"status": "error", "action_type": t, "message": "system_ops not available"}
        app_path = str((action or {}).get("app_path") or "").strip()
        if not app_path:
            return {"status": "error", "action_type": t, "message": "app_path required"}
        args = (action or {}).get("args") or []
        if not isinstance(args, list):
            args = []
        try:
            return system_ops.launch_application(app_path, args)
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t == "kill_process":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        pid = (action or {}).get("pid")
        name = (action or {}).get("name") or (action or {}).get("process_name")
        if pid is not None:
            try:
                pid_int = int(pid)
            except Exception:
                return {"status": "error", "action_type": t, "message": "Invalid pid"}
            try:
                p = subprocess.run(["taskkill", "/PID", str(pid_int), "/F"], capture_output=True, text=True, timeout=8.0)
                if p.returncode != 0:
                    return {"status": "error", "action_type": t, "message": (p.stderr or p.stdout or "").strip()}
                return {"status": "success", "action_type": t, "pid": pid_int}
            except Exception as e:
                return {"status": "error", "action_type": t, "message": str(e)}

        if not name:
            return {"status": "error", "action_type": t, "message": "Provide pid or name"}
        if not SYSTEM_OPS_AVAILABLE or not system_ops:
            return {"status": "error", "action_type": t, "message": "system_ops not available"}
        try:
            return system_ops.kill_process(str(name))
        except Exception as e:
            return {"status": "error", "action_type": t, "message": str(e)}

    if t == "set_wifi":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        enabled = bool((action or {}).get("enabled", True))
        ok, err = _set_windows_wifi(enabled)
        if not ok:
            _open_windows_settings("ms-settings:network-wifi")
            return {"status": "error", "action_type": t, "enabled": enabled, "message": (err + " (Opened Wi-Fi settings.)").strip()}
        return {"status": "success", "action_type": t, "enabled": enabled}

    if t == "set_bluetooth":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        enabled = bool((action or {}).get("enabled", True))
        ok, err = _set_windows_bluetooth(enabled)
        if not ok:
            _open_windows_settings("ms-settings:bluetooth")
            return {"status": "error", "action_type": t, "enabled": enabled, "message": (err + " (Opened Bluetooth settings.)").strip()}
        return {"status": "success", "action_type": t, "enabled": enabled}

    if t == "set_airplane_mode":
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        enabled = bool((action or {}).get("enabled", True))
        # There is no stable, universally safe Windows CLI toggle for Airplane Mode.
        # Fall back to opening Network settings and report guided/manual step required.
        _open_windows_settings("ms-settings:network")
        return {
            "status": "error",
            "action_type": t,
            "enabled": enabled,
            "message": "Airplane mode toggle is not directly supported on this agent. Opened Network settings.",
        }

    if t in ("show_desktop", "open_task_manager", "open_run_dialog", "open_start_menu"):
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}
        keys = None
        if t == "show_desktop":
            keys = "win+d"
        elif t == "open_task_manager":
            keys = "ctrl+shift+esc"
        elif t == "open_run_dialog":
            keys = "win+r"
        elif t == "open_start_menu":
            keys = "win"
        ok = bool(sa.hotkey(keys)) if keys else False
        return {"status": "success" if ok else "error", "action_type": t, "keys": keys}

    if t in (
        "open_quick_settings",
        "open_notification_center",
        "window_snap_left",
        "window_snap_right",
        "window_maximize",
        "window_minimize",
        "media_play_pause",
        "media_next_track",
        "media_prev_track",
        "media_stop",
    ):
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}

        # Prefer hotkeys where possible; use media keys via press_key.
        if t == "open_quick_settings":
            # Win11: quick settings. Win10: action center.
            ok = bool(sa.hotkey("win+a"))
            return {"status": "success" if ok else "error", "action_type": t, "keys": "win+a"}
        if t == "open_notification_center":
            # Win11: notification center.
            ok = bool(sa.hotkey("win+n"))
            return {"status": "success" if ok else "error", "action_type": t, "keys": "win+n"}
        if t == "window_snap_left":
            ok = bool(sa.hotkey("win+left"))
            return {"status": "success" if ok else "error", "action_type": t, "keys": "win+left"}
        if t == "window_snap_right":
            ok = bool(sa.hotkey("win+right"))
            return {"status": "success" if ok else "error", "action_type": t, "keys": "win+right"}
        if t == "window_maximize":
            ok = bool(sa.hotkey("win+up"))
            return {"status": "success" if ok else "error", "action_type": t, "keys": "win+up"}
        if t == "window_minimize":
            ok = bool(sa.hotkey("win+down"))
            return {"status": "success" if ok else "error", "action_type": t, "keys": "win+down"}

        # Media keys. pyautogui supports these on Windows: playpause/nexttrack/prevtrack/stop.
        key = None
        if t == "media_play_pause":
            key = "playpause"
        elif t == "media_next_track":
            key = "nexttrack"
        elif t == "media_prev_track":
            key = "prevtrack"
        elif t == "media_stop":
            key = "stop"
        ok = bool(sa.press_key(key, presses=1)) if key else False
        return {"status": "success" if ok else "error", "action_type": t, "key": key}

    if t in ("set_brightness", "adjust_brightness"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}

        before_value = _get_windows_brightness()

        # Accept either absolute value (0-100) or delta.
        try:
            value = action.get("value", None)
            if value is None:
                value = action.get("percent", None)
        except Exception:
            value = None

        delta = action.get("delta", None)

        if value is None and delta is None:
            return {"status": "error", "action_type": t, "message": "Provide value (0-100) or delta."}

        target = None
        if value is not None:
            try:
                target = int(float(value))
            except Exception:
                target = None
        else:
            try:
                d = int(float(delta))
            except Exception:
                return {"status": "error", "action_type": t, "message": "Invalid delta"}
            current = _get_windows_brightness()
            if current is None:
                current = 50
            target = current + d

        if target is None:
            return {"status": "error", "action_type": t, "message": "Invalid brightness"}

        target = max(0, min(100, int(target)))
        ok, err = _set_windows_brightness(target)
        if not ok:
            # Fallback: open Display settings so the user can adjust manually.
            _open_windows_settings("ms-settings:display")
            msg = (err or "Failed to set brightness")
            return {
                "status": "error",
                "action_type": t,
                "message": (msg + " (Opened Display settings.)").strip(),
                "before_value": before_value,
                "value": target,
            }
        after_value = _get_windows_brightness()
        verified = _within_tolerance(after_value, target, tolerance=4)
        return {
            "status": "success" if verified else "partial",
            "action_type": "set_brightness",
            "before_value": before_value,
            "value": target,
            "after_value": after_value,
            "verified": verified,
        }

    if t in ("set_volume", "adjust_volume"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}

        before_value, _before_err = _get_windows_volume_percent()

        try:
            value = action.get("value", None)
            if value is None:
                value = action.get("percent", None)
        except Exception:
            value = None

        delta = action.get("delta", None)

        if value is None and delta is None:
            return {"status": "error", "action_type": t, "message": "Provide value (0-100) or delta."}

        target = None
        if value is not None:
            try:
                target = int(float(value))
            except Exception:
                target = None
        else:
            try:
                d = int(float(delta))
            except Exception:
                return {"status": "error", "action_type": t, "message": "Invalid delta"}
            current, err = _get_windows_volume_percent()
            if current is None:
                # Default midpoint if we can't read current.
                current = 50
            target = current + d

        if target is None:
            return {"status": "error", "action_type": t, "message": "Invalid volume"}

        target = max(0, min(100, int(target)))
        ok, err = _set_windows_volume_percent(target)
        if not ok:
            _open_windows_settings("ms-settings:sound")
            msg = (err or "Failed to set volume")
            return {
                "status": "error",
                "action_type": t,
                "message": (msg + " (Opened Sound settings.)").strip(),
                "before_value": before_value,
                "value": target,
            }
        after_value, _after_err = _get_windows_volume_percent()
        verified = _within_tolerance(after_value, target, tolerance=4)
        return {
            "status": "success" if verified else "partial",
            "action_type": "set_volume",
            "before_value": before_value,
            "value": target,
            "after_value": after_value,
            "verified": verified,
        }

    if t in ("set_mute", "toggle_mute"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}

        before_mute, _before_err = _get_windows_mute()

        muted = (action or {}).get("muted", None)
        if muted is None and t == "toggle_mute":
            current, err = _get_windows_mute()
            if current is None:
                _open_windows_settings("ms-settings:sound")
                return {"status": "error", "action_type": t, "message": (err or "Failed to read mute state") + " (Opened Sound settings.)"}
            muted = not bool(current)

        if muted is None:
            return {"status": "error", "action_type": t, "message": "Provide muted=true/false."}

        ok, err = _set_windows_mute(bool(muted))
        if not ok:
            _open_windows_settings("ms-settings:sound")
            msg = (err or "Failed to set mute")
            return {
                "status": "error",
                "action_type": t,
                "message": (msg + " (Opened Sound settings.)").strip(),
                "before_muted": before_mute,
                "muted": bool(muted),
            }

        after_mute, _after_err = _get_windows_mute()
        verified = (after_mute is not None) and (bool(after_mute) == bool(muted))
        return {
            "status": "success" if verified else "partial",
            "action_type": "set_mute",
            "before_muted": before_mute,
            "muted": bool(muted),
            "after_muted": after_mute,
            "verified": verified,
        }

    if t in ("set_power_plan", "set_energy_saver"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        before_guid, before_name, _before_err = _get_windows_active_power_plan()
        plan = (action or {}).get("plan") or ("power saver" if bool((action or {}).get("enabled", True)) else "balanced")
        ok, msg, guid = _set_windows_power_plan(str(plan))
        if not ok:
            _open_windows_settings("ms-settings:powersleep")
            msg = (msg + " (Opened Power & sleep settings.)").strip()
            return {
                "status": "error",
                "action_type": "set_power_plan",
                "plan": str(plan),
                "guid": guid,
                "before_guid": before_guid,
                "before_name": before_name,
                "message": msg,
            }

        after_guid, after_name, _after_err = _get_windows_active_power_plan()
        verified = bool(after_guid and guid and (str(after_guid).lower() == str(guid).lower()))
        return {
            "status": "success" if verified else "partial",
            "action_type": "set_power_plan",
            "plan": str(plan),
            "guid": guid,
            "before_guid": before_guid,
            "before_name": before_name,
            "after_guid": after_guid,
            "after_name": after_name,
            "verified": verified,
            "message": msg,
        }

    if t in ("capture_screen", "screen_navigation"):
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}
        if t == "capture_screen":
            reg = action.get("region")
            region = None
            if isinstance(reg, dict):
                try:
                    rx = int(reg.get("x", 0))
                    ry = int(reg.get("y", 0))
                    rw = int(reg.get("width", 0))
                    rh = int(reg.get("height", 0))
                    if rw > 0 and rh > 0:
                        region = (rx, ry, rw, rh)
                except Exception:
                    region = None
            return {
                "status": "success",
                "action_type": "capture_screen",
                "screenshot": sa.take_screenshot_info(region=region, include_base64=False),
            }

        # Implement a minimal, explicit subset of screen navigation commands.
        command = (action or {}).get("command") or ""
        try:
            cmd = str(command).strip().lower()
        except Exception:
            cmd = ""

        try:
            if cmd in ("capture_screen", "screenshot"):
                reg = (action or {}).get("region")
                region = None
                if isinstance(reg, dict):
                    try:
                        rx = int(reg.get("x", 0))
                        ry = int(reg.get("y", 0))
                        rw = int(reg.get("width", 0))
                        rh = int(reg.get("height", 0))
                        if rw > 0 and rh > 0:
                            region = (rx, ry, rw, rh)
                    except Exception:
                        region = None
                return {
                    "status": "success",
                    "action_type": "screen_navigation",
                    "command": cmd,
                    "screenshot": sa.take_screenshot_info(region=region, include_base64=False),
                }

            if cmd in ("read_screen", "ocr"):
                reg = (action or {}).get("region")
                region = None
                if isinstance(reg, dict):
                    try:
                        rx = int(reg.get("x", 0))
                        ry = int(reg.get("y", 0))
                        rw = int(reg.get("width", 0))
                        rh = int(reg.get("height", 0))
                        if rw > 0 and rh > 0:
                            region = (rx, ry, rw, rh)
                    except Exception:
                        region = None
                text = sa.read_screen_text(region)
                return {"status": "success", "action_type": "screen_navigation", "command": cmd, "text": text}

            if cmd == "find_text":
                needle = str((action or {}).get("text") or "").strip()
                pos = sa.find_text_on_screen(needle) if needle else None
                if pos:
                    return {"status": "success", "action_type": "screen_navigation", "command": cmd, "text": needle, "position": pos}
                return {"status": "error", "action_type": "screen_navigation", "command": cmd, "message": "Text not found"}

            if cmd == "move_mouse":
                x = int((action or {}).get("x", 0))
                y = int((action or {}).get("y", 0))
                try:
                    duration = float((action or {}).get("duration", 0.5))
                except Exception:
                    duration = 0.5
                ok = bool(sa.move_mouse(x, y, duration=duration))
                return {"status": "success" if ok else "error", "action_type": "screen_navigation", "command": cmd, "x": x, "y": y}

            if cmd == "click":
                button = str((action or {}).get("button") or "left").strip().lower() or "left"
                x = (action or {}).get("x")
                y = (action or {}).get("y")
                if x is not None and y is not None:
                    ok = bool(sa.click_at_position(int(x), int(y), button=button))
                    return {"status": "success" if ok else "error", "action_type": "screen_navigation", "command": cmd, "x": int(x), "y": int(y), "button": button}
                # No coordinates -> not supported (avoid accidental clicks)
                return {"status": "error", "action_type": "screen_navigation", "command": cmd, "message": "x and y are required for click"}

            if cmd in ("type", "type_text"):
                text = str((action or {}).get("text") or "")
                try:
                    interval = float((action or {}).get("interval", 0.02))
                except Exception:
                    interval = 0.02
                ok = bool(sa.type_text(text, interval=interval))
                return {"status": "success" if ok else "error", "action_type": "screen_navigation", "command": cmd, "typed": len(text)}

            if cmd == "press_key":
                key = str((action or {}).get("key") or "").strip()
                try:
                    presses = int((action or {}).get("presses", 1))
                except Exception:
                    presses = 1
                presses = max(1, min(20, presses))
                ok = bool(sa.press_key(key, presses=presses)) if key else False
                return {"status": "success" if ok else "error", "action_type": "screen_navigation", "command": cmd, "key": key, "presses": presses}

            if cmd == "scroll":
                # Note: ScreenAccess.scroll uses x/y as direction multipliers, not coordinates.
                # We accept either explicit direction, or infer direction from the sign of x/y.
                direction = str((action or {}).get("direction") or "").strip().lower()
                raw_x = int((action or {}).get("x", 0) or 0)
                raw_y = int((action or {}).get("y", 0) or 0)

                x = 0
                y = 0
                if direction in ("down", "scroll_down"):
                    y = 1
                elif direction in ("up", "scroll_up"):
                    y = -1
                elif direction in ("right", "scroll_right"):
                    x = 1
                elif direction in ("left", "scroll_left"):
                    x = -1
                else:
                    x = 0 if raw_x == 0 else (1 if raw_x > 0 else -1)
                    y = 0 if raw_y == 0 else (1 if raw_y > 0 else -1)

                try:
                    clicks = int((action or {}).get("clicks", 3))
                except Exception:
                    clicks = 3
                clicks = max(1, min(50, clicks))
                ok = bool(sa.scroll(x, y, clicks))
                return {"status": "success" if ok else "error", "action_type": "screen_navigation", "command": cmd, "direction": direction or None, "x": x, "y": y, "clicks": clicks}

            if cmd == "hotkey":
                keys = (action or {}).get("keys")
                if keys is None:
                    keys = (action or {}).get("key")
                ok = bool(sa.hotkey(keys))
                return {"status": "success" if ok else "error", "action_type": "screen_navigation", "command": cmd, "keys": keys}

            if cmd == "get_mouse_position":
                pos = sa.get_mouse_position()
                return {"status": "success", "action_type": "screen_navigation", "command": cmd, "position": pos}

            return {"status": "error", "action_type": "screen_navigation", "command": cmd, "message": f"Unknown screen_navigation command: {cmd}"}
        except Exception as e:
            return {"status": "error", "action_type": "screen_navigation", "command": cmd, "message": str(e)}

    if t in ("type_text", "press_key", "hotkey"):
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}

        if t == "type_text":
            text = str(action.get("text") or "")
            try:
                before_ms = int((action or {}).get("before_ms", 0) or 0)
            except Exception:
                before_ms = 0
            if before_ms > 0:
                await asyncio.sleep(min(5.0, max(0.0, before_ms / 1000.0)))
            try:
                interval = float(action.get("interval", 0.05))
            except Exception:
                interval = 0.05

            interval = max(0.02, min(0.2, interval))

            # Chunk long text to reduce missed keystrokes (common in Notepad with fast typing).
            if len(text) > 500:
                interval = max(interval, 0.04)
                typed = 0
                chunk = 220
                for i in range(0, len(text), chunk):
                    part = text[i:i + chunk]
                    timeout_s = max(6.0, min(45.0, (len(part) * interval * 2.5) + 3.0))
                    ok_part, err_part = await _type_text_with_timeout(sa, part, interval=interval, timeout_s=timeout_s)
                    if not ok_part:
                        return {
                            "status": "error",
                            "action_type": t,
                            "typed": typed,
                            "message": err_part or "Typing failed",
                        }
                    typed += len(part)
                    await asyncio.sleep(0.08)
                return {"status": "success", "action_type": t, "typed": typed}

            timeout_s = max(6.0, min(120.0, (len(text) * interval * 2.5) + 4.0))
            ok, err = await _type_text_with_timeout(sa, text, interval=interval, timeout_s=timeout_s)
            if not ok:
                return {
                    "status": "error",
                    "action_type": t,
                    "typed": 0,
                    "message": err or "Typing failed",
                }
            return {"status": "success", "action_type": t, "typed": len(text)}

        if t == "hotkey":
            keys = (action or {}).get("keys")
            # allow "ctrl+a" style too
            if keys is None:
                keys = (action or {}).get("key")
            ok = bool(sa.hotkey(keys))
            return {"status": "success" if ok else "error", "action_type": t, "keys": keys}

        key = str(action.get("key") or "").strip()
        try:
            presses = int(action.get("presses", 1))
        except Exception:
            presses = 1
        presses = max(1, min(20, presses))
        ok = bool(sa.press_key(key, presses=presses)) if key else False
        return {"status": "success" if ok else "error", "action_type": t, "key": key, "presses": presses}

    if t.startswith("system_") or t in ("process_kill",):
        if not SYSTEM_OPS_AVAILABLE or not system_ops:
            return {"status": "error", "action_type": t, "message": "system_ops not available"}

    if t == "system_info":
        return system_ops.get_system_info()

    if t in ("self_update", "self_add"):
        # Execution-only node: no local intelligence/autonomous code generation.
        return {
            "status": "forbidden",
            "action_type": t,
            "message": "Action is cloud-intelligence only and is not supported on execution-only agent",
        }

    if t == "agent_set_permissions":
        if ACTION_ALLOWLIST is not None and t not in ACTION_ALLOWLIST:
            return {"status": "forbidden", "action_type": t, "message": "Action blocked by agent allowlist"}
        if not ALLOW_REMOTE_PERMISSION_CHANGES:
            return {"status": "forbidden", "action_type": t, "message": "Remote permission changes disabled on agent"}

        perms = (action or {}).get("permissions") or {}
        if not isinstance(perms, dict) or not perms:
            return {"status": "error", "action_type": t, "message": "permissions dict is required"}

        allowed_keys = {
            "allow_app_control",
            "allow_execute_command",
            "allow_file_ops",
            "allow_screen",
            "allow_self_update",
        }
        applied = {}
        for k, v in perms.items():
            if k not in allowed_keys:
                continue
            applied[k] = bool(v)

        if "allow_app_control" in applied:
            ALLOW_APP_CONTROL = applied["allow_app_control"]
        if "allow_execute_command" in applied:
            ALLOW_EXECUTE_COMMAND = applied["allow_execute_command"]
        if "allow_file_ops" in applied:
            ALLOW_FILE_OPS = applied["allow_file_ops"]
        if "allow_screen" in applied:
            ALLOW_SCREEN = applied["allow_screen"]
        if "allow_self_update" in applied:
            ALLOW_SELF_UPDATE = applied["allow_self_update"]

        # Persist so next run works without re-approving.
        _save_permissions()

        return {"status": "success", "action_type": t, "applied": applied, "capabilities": _current_capabilities()}

    if t in ("agent_shutdown", "agent_stop"):
        if ACTION_ALLOWLIST is not None and t not in ACTION_ALLOWLIST:
            return {"status": "forbidden", "action_type": t, "message": "Action blocked by agent allowlist"}
        ev = globals().get("_STOP_EVENT")
        try:
            if ev is not None:
                ev.set()
        except Exception:
            pass
        return {"status": "success", "action_type": t, "message": "Agent stopping"}

    return {"status": "ignored", "action_type": t}


async def run_agent(
    agent_token: str | None = None,
    server_base_url: str | None = None,
    device_id: str | None = None,
    shared_secret: str | None = None,
):
    # Apply any previously approved permissions before connecting.
    _load_saved_permissions()

    base = (server_base_url or SERVER_BASE_URL).rstrip("/")
    ws_url = _ws_url_from_base(base)
    selected_device_id = _resolve_device_id(device_id)

    selected_shared_secret = str(shared_secret or "").strip()
    if not agent_token and not selected_shared_secret:
        raise SystemExit("Missing agent auth. Provide --token (recommended) or --shared-secret.")

    if agent_token:
        token_did = _token_device_id_unverified(agent_token)
        if token_did and selected_device_id and token_did != selected_device_id:
            raise SystemExit(
                f"Token/device mismatch: token is bound to '{token_did}' but selected device_id is '{selected_device_id}'. Clear token and request a new /api/agent/config token for this device."
            )

    print(f"[AGENT] Connecting to {ws_url} as device_id={selected_device_id}", flush=True)

    stop_event = asyncio.Event()
    globals()["_STOP_EVENT"] = stop_event

    def _stop(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    # Best-effort resend buffer for transient websocket disconnects.
    pending_results: list[dict] = []

    async with aiohttp.ClientSession() as session:
        while not stop_event.is_set():
            try:
                async with session.ws_connect(ws_url, heartbeat=PING_INTERVAL_S) as ws:
                    capabilities = _current_capabilities()
                    auth_msg = {
                        "type": "auth",
                        "capabilities": capabilities,
                    }
                    # Preferred auth: server-issued JWT token.
                    if agent_token:
                        auth_msg["token"] = agent_token
                        # device_id is optional in token mode; server derives it from token.
                        auth_msg["device_id"] = selected_device_id
                    else:
                        auth_msg["device_id"] = selected_device_id
                        auth_msg["secret"] = selected_shared_secret

                    await ws.send_str(json.dumps(auth_msg))
                    try:
                        ack = await ws.receive(timeout=15)
                    except asyncio.TimeoutError:
                        raise RuntimeError("Timed out waiting for server ack")

                    if ack.type == aiohttp.WSMsgType.TEXT:
                        ack_text = (ack.data or "").strip()
                        # Server may send a structured error payload.
                        try:
                            ack_obj = json.loads(ack_text) if ack_text else {}
                        except Exception:
                            ack_obj = {}

                        if isinstance(ack_obj, dict) and ack_obj.get("type") == "error":
                            reason = ack_obj.get("reason") or ack_obj.get("message") or "auth_failed"
                            hint = ""
                            if str(reason) in {"invalid_agent_token", "auth_failed"}:
                                hint = " (Hint: paste the agent_token from /api/agent/config, not your login/session JWT.)"
                            raise RuntimeError(f"Server rejected connection: {reason}{hint}")

                        print(f"[AGENT] Connected: {ack_text}", flush=True)

                    elif ack.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                        close_code = getattr(ws, "close_code", None)
                        raise RuntimeError(f"Connection closed before ack (close_code={close_code})")
                    elif ack.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError(f"WebSocket error before ack: {ws.exception()}")
                    else:
                        raise RuntimeError(f"No ack from server (msg_type={ack.type})")

                    effective_device_id = selected_device_id
                    try:
                        if isinstance(ack_obj, dict) and ack_obj.get("device_id"):
                            effective_device_id = str(ack_obj.get("device_id")).strip().lower() or effective_device_id
                    except Exception:
                        pass
                    print(f"[AGENT] Connected: device_id={effective_device_id}", flush=True)

                    # Flush any buffered results from prior disconnected sends.
                    if pending_results:
                        still_pending: list[dict] = []
                        for item in pending_results:
                            try:
                                await ws.send_str(json.dumps(item))
                            except Exception:
                                still_pending.append(item)
                        pending_results = still_pending

                    async def pinger():
                        try:
                            while True:
                                await asyncio.sleep(PING_INTERVAL_S)
                                await ws.send_str(json.dumps({"type": "ping", "device_id": effective_device_id, "ts": _now_utc_iso()}))
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            print(f"[AGENT] Ping failed: {exc}", flush=True)
                            try:
                                await ws.close()
                            except Exception:
                                pass
                            raise

                    ping_task = asyncio.create_task(pinger())

                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    payload = json.loads(msg.data)
                                except Exception:
                                    continue

                                if payload.get("type") != "job":
                                    continue

                                job_id = payload.get("job_id")
                                actions = payload.get("actions") or []

                                results = []
                                for idx, a in enumerate(actions):
                                    try:
                                        results.append(await _execute_action_contract(a, job_id=str(job_id or ""), action_index=idx))
                                    except Exception as e:
                                        results.append(
                                            {
                                                "status": "failed",
                                                "result": None,
                                                "error": str(e),
                                                "execution_time": 0.0,
                                                "task_id": f"{str(job_id or '')}:{idx}",
                                                "action": str((a or {}).get("action") or (a or {}).get("type") or "unknown"),
                                            }
                                        )

                                # If permissions/capabilities changed, publish updated capabilities immediately.
                                try:
                                    cap = None
                                    for r in results:
                                        if isinstance(r, dict) and isinstance(r.get("capabilities"), dict):
                                            cap = r.get("capabilities")
                                            break
                                    if cap:
                                        await ws.send_str(json.dumps({
                                            "type": "capabilities",
                                            "device_id": effective_device_id,
                                            "capabilities": cap,
                                            "updated_at": _now_utc_iso(),
                                        }))
                                except Exception:
                                    pass

                                result_payload = {
                                    "type": "result",
                                    "device_id": effective_device_id,
                                    "job_id": job_id,
                                    "results": results,
                                    "completed_at": _now_utc_iso(),
                                }
                                try:
                                    await ws.send_str(json.dumps(result_payload))
                                except Exception:
                                    pending_results.append(result_payload)

                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass

            except Exception as e:
                msg = str(e or "")
                try:
                    msg = msg.replace("refuced", "refused")
                except Exception:
                    pass
                print(f"[AGENT] Connection error: {msg}", flush=True)
                ml = (msg or "").lower()
                if "server rejected connection:" in ml and (
                    "invalid_agent_token" in ml
                    or "device_not_authorized" in ml
                    or "invalid_shared_secret" in ml
                ):
                    raise SystemExit(msg)
                try:
                    if (
                        "refused" in ml
                        or "connect call failed" in ml
                        or "cannot connect to host" in ml
                        or "winerror 10061" in ml
                    ):
                        print(
                            f"[AGENT] Hint: server is unreachable. Check JARVIS_SERVER_URL ({base}) and ensure backend is running.",
                            flush=True,
                        )
                except Exception:
                    pass
                await asyncio.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis PC Agent")
    parser.add_argument("--token", dest="token", default=None, help="Agent token from /api/agent/config (recommended)")
    parser.add_argument("--shared-secret", dest="shared_secret", default=None, help="Legacy shared secret from /api/agent/config")
    parser.add_argument("--server", dest="server", default=None, help="Server base URL (defaults to JARVIS_SERVER_URL)")
    parser.add_argument("--device-id", dest="device_id", default=None, help="Explicit device id (overrides env and hostname)")
    args = parser.parse_args()

    asyncio.run(
        run_agent(
            agent_token=args.token,
            server_base_url=args.server,
            device_id=args.device_id,
            shared_secret=args.shared_secret,
        )
    )
