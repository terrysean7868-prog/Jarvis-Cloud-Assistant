import asyncio
import argparse
import json
import os
import sys
import platform
import re
import signal
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, UTC

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load environment from .env if present (avoids manual env setup).
try:
    from dotenv import load_dotenv
    for _p in (REPO_ROOT / ".env",):
        try:
            if _p.exists():
                load_dotenv(_p, override=False)
        except Exception:
            pass
except Exception:
    pass

# NOTE: This agent is intentionally defensive.
# We avoid importing optional/heavy modules at startup to prevent dependency issues
# and to reduce the chance of accidental data exposure.

# system_ops may not exist on some platforms; in this repo it should.
try:
    from src.utils.system_operations import system_ops
    SYSTEM_OPS_AVAILABLE = True
except Exception:
    system_ops = None
    SYSTEM_OPS_AVAILABLE = False

# Centralized env access (fallback to os.getenv if src package isn't available).
try:
    from src.config import env
except Exception:
    env = None

def _env_bool(name: str, default: str = "false") -> bool:
    if env is not None:
        return env.get_bool(name, default.lower() in ("1", "true", "yes", "y"))
    return os.getenv(name, default).lower() in ("1", "true", "yes", "y")


def _env_str(name: str, default: str = "") -> str:
    if env is not None:
        return env.get_str(name, default)
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    if env is not None:
        return env.get_int(name, default)
    try:
        return int((os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


SERVER_BASE_URL = _env_str("JARVIS_SERVER_URL", "https://jarvis-cloud-assistant.onrender.com").rstrip("/")
# Device IDs are treated case-insensitively by the server.
DEVICE_ID = (_env_str("JARVIS_DEVICE_ID", platform.node() or "primary") or "primary").strip().lower()
SHARED_SECRET = _env_str("JARVIS_AGENT_SHARED_SECRET", "")

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


def _current_capabilities() -> dict:
    return {
        "allow_execute_command": bool(ALLOW_EXECUTE_COMMAND),
        "allow_app_control": bool(ALLOW_APP_CONTROL),
        "allow_screen": bool(ALLOW_SCREEN),
        "allow_self_update": bool(ALLOW_SELF_UPDATE),
        "allow_file_ops": bool(ALLOW_FILE_OPS),
        "platform": platform.system().lower(),
        "hostname": platform.node(),
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


def _get_self_update():
    # Lazy import: self_update pulls in OpenAI + pydantic plugins.
    try:
        from src.utils.self_update import self_update_file, self_add_feature
        return self_update_file, self_add_feature
    except Exception:
        return None, None


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
        name = (action or {}).get("name") or (action or {}).get("action") or ""
        args = (action or {}).get("args")
        if not isinstance(args, dict):
            args = {}
        name = str(name or "").strip()
        if not name or name == "device_action":
            return {"status": "error", "action_type": "device_action", "message": "Invalid device action name"}
        # Convert into an existing action shape and reuse the normal dispatcher.
        nested = {"type": name}
        nested.update(args)
        return await _execute_action(nested)

    if t == "list_device_actions":
        # Read-only: returns a catalog of common supported universal actions.
        # Capability gating still applies when executing the actual action.
        return {
            "status": "success",
            "action_type": t,
            "actions": sorted([
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
                "open_path",
                "get_clipboard",
                "set_clipboard",
                "list_processes",
                "kill_process",
                "set_wifi",
                "set_bluetooth",
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
            ]),
        }

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
            return mgr.open_app(name, action.get("args") or [])
        if t == "close_app":
            return mgr.close_app(name)
        if t == "switch_app":
            return mgr.switch_to_app(name)

    if t == "execute_command":
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
                "value": target,
            }
        return {"status": "success", "action_type": "set_brightness", "value": target}

    if t in ("set_volume", "adjust_volume"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}

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
                "value": target,
            }
        return {"status": "success", "action_type": "set_volume", "value": target}

    if t in ("set_mute", "toggle_mute"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}

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
                "muted": bool(muted),
            }

        return {"status": "success", "action_type": "set_mute", "muted": bool(muted)}

    if t in ("set_power_plan", "set_energy_saver"):
        if not ALLOW_EXECUTE_COMMAND:
            return {"status": "forbidden", "action_type": t, "message": "System control disabled on agent (enable execute_command permission)."}
        plan = (action or {}).get("plan") or ("power saver" if bool((action or {}).get("enabled", True)) else "balanced")
        ok, msg, guid = _set_windows_power_plan(str(plan))
        if not ok:
            _open_windows_settings("ms-settings:powersleep")
            msg = (msg + " (Opened Power & sleep settings.)").strip()
        return {"status": "success" if ok else "error", "action_type": "set_power_plan", "plan": str(plan), "guid": guid, "message": msg}

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
                ok_all = True
                chunk = 220
                for i in range(0, len(text), chunk):
                    part = text[i:i + chunk]
                    ok_all = bool(sa.type_text(part, interval=interval)) and ok_all
                    await asyncio.sleep(0.08)
                return {"status": "success" if ok_all else "error", "action_type": t, "typed": len(text)}

            ok = bool(sa.type_text(text, interval=interval))
            return {"status": "success" if ok else "error", "action_type": t, "typed": len(text)}

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

    if t == "self_update":
        if not ALLOW_SELF_UPDATE:
            return {"status": "forbidden", "action_type": t, "message": "Self-update disabled on agent"}
        self_update_file, _self_add_feature = _get_self_update()
        if not self_update_file:
            return {"status": "error", "action_type": t, "message": "Self-update not available on this agent"}
        description = action.get("description", "")
        file_path = action.get("file_path", "")
        return self_update_file(description, file_path)

    if t == "self_add":
        if not ALLOW_SELF_UPDATE:
            return {"status": "forbidden", "action_type": t, "message": "Self-update disabled on agent"}
        _self_update_file, self_add_feature = _get_self_update()
        if not self_add_feature:
            return {"status": "error", "action_type": t, "message": "Self-add not available on this agent"}
        description = action.get("description", "")
        feature_type = action.get("feature_type", "module")
        return self_add_feature(description, feature_type)

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


async def run_agent(agent_token: str | None = None, server_base_url: str | None = None):
    # Apply any previously approved permissions before connecting.
    _load_saved_permissions()

    base = (server_base_url or SERVER_BASE_URL).rstrip("/")
    ws_url = _ws_url_from_base(base)

    if not agent_token and not SHARED_SECRET:
        raise SystemExit("Missing agent auth. Provide --token (recommended) or set JARVIS_AGENT_SHARED_SECRET.")

    print(f"[AGENT] Connecting to {ws_url} as device_id={DEVICE_ID}", flush=True)

    stop_event = asyncio.Event()
    globals()["_STOP_EVENT"] = stop_event

    def _stop(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

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
                        auth_msg["device_id"] = DEVICE_ID
                    else:
                        auth_msg["device_id"] = DEVICE_ID
                        auth_msg["secret"] = SHARED_SECRET

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

                    effective_device_id = DEVICE_ID
                    try:
                        if isinstance(ack_obj, dict) and ack_obj.get("device_id"):
                            effective_device_id = str(ack_obj.get("device_id")).strip().lower() or effective_device_id
                    except Exception:
                        pass

                    async def pinger():
                        while True:
                            await asyncio.sleep(PING_INTERVAL_S)
                            await ws.send_str(json.dumps({"type": "ping", "device_id": effective_device_id, "ts": _now_utc_iso()}))

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
                                for a in actions:
                                    try:
                                        results.append(await _execute_action(a))
                                    except Exception as e:
                                        results.append({"status": "error", "message": str(e), "action": a})

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

                                await ws.send_str(json.dumps({
                                    "type": "result",
                                    "device_id": effective_device_id,
                                    "job_id": job_id,
                                    "results": results,
                                    "completed_at": _now_utc_iso(),
                                }))

                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        ping_task.cancel()

            except Exception as e:
                print(f"[AGENT] Connection error: {e}", flush=True)
                await asyncio.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis PC Agent")
    parser.add_argument("--token", dest="token", default=None, help="Agent token from /api/agent/config (recommended)")
    parser.add_argument("--server", dest="server", default=None, help="Server base URL (defaults to JARVIS_SERVER_URL)")
    args = parser.parse_args()

    asyncio.run(run_agent(agent_token=args.token, server_base_url=args.server))
