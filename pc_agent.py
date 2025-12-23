import asyncio
import json
import os
import platform
import signal
import shutil
from pathlib import Path
from datetime import datetime, UTC

import aiohttp

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

def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "y")


SERVER_BASE_URL = os.getenv("JARVIS_SERVER_URL", "https://jarvis-cloud-assistant.onrender.com").rstrip("/")
DEVICE_ID = os.getenv("JARVIS_DEVICE_ID", platform.node() or "primary")
SHARED_SECRET = os.getenv("JARVIS_AGENT_SHARED_SECRET", "")

ALLOW_EXECUTE_COMMAND = _env_bool("JARVIS_AGENT_ALLOW_EXECUTE_COMMAND", "false")
ALLOW_APP_CONTROL = _env_bool("JARVIS_AGENT_ALLOW_APP_CONTROL", "false")
ALLOW_SCREEN = _env_bool("JARVIS_AGENT_ALLOW_SCREEN", "false")
ALLOW_SELF_UPDATE = _env_bool("JARVIS_AGENT_ALLOW_SELF_UPDATE", "false")

# File ops are powerful. Keep sandboxed.
ALLOW_FILE_OPS = _env_bool("JARVIS_AGENT_ALLOW_FILE_OPS", "false")
PROJECT_ROOT = Path(os.getenv("JARVIS_AGENT_PROJECT_ROOT", Path(__file__).resolve().parent)).resolve()
ALLOWED_PATHS_RAW = os.getenv("JARVIS_AGENT_ALLOWED_PATHS", "data,docs,src,modules")
ALLOWED_ROOTS = [(PROJECT_ROOT / p.strip()).resolve() for p in ALLOWED_PATHS_RAW.split(",") if p.strip()]
BLOCKED_DIRS = {".git", "venv", "__pycache__", ".pytest_cache", "node_modules"}
BLOCKED_FILES = {".env", ".env.example", "id_rsa", "id_rsa.pub"}

PING_INTERVAL_S = int(os.getenv("JARVIS_AGENT_PING_INTERVAL", "20"))

# Optional strict allowlist of action types.
# If set, ONLY these action types will be executed.
_ALLOWLIST_RAW = os.getenv("JARVIS_AGENT_ACTION_ALLOWLIST", "").strip()
ACTION_ALLOWLIST = {a.strip() for a in _ALLOWLIST_RAW.split(",") if a.strip()} if _ALLOWLIST_RAW else None


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


async def _execute_action(action: dict) -> dict:
    t = (action or {}).get("type")

    if ACTION_ALLOWLIST is not None and t not in ACTION_ALLOWLIST:
        return {"status": "forbidden", "action_type": t, "message": "Action blocked by agent allowlist"}

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
        return mgr.execute_command(cmd, wait)

    if t in ("capture_screen", "screen_navigation"):
        if not ALLOW_SCREEN:
            return {"status": "forbidden", "action_type": t, "message": "Screen features disabled on agent"}
        sa = _get_screen_access()
        if not sa:
            return {"status": "error", "action_type": t, "message": "Screen features not available on this agent"}
        if t == "capture_screen":
            return {
                "status": "success",
                "action_type": "capture_screen",
                "screenshot": sa.take_screenshot_info(),
            }
        # screen_navigation currently implemented in ActionExecutor; we keep agent minimal.
        return {"status": "error", "action_type": "screen_navigation", "message": "screen_navigation not implemented in agent"}

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

    return {"status": "ignored", "action_type": t}


async def run_agent():
    if not SHARED_SECRET:
        raise SystemExit("Missing JARVIS_AGENT_SHARED_SECRET (must match Render env)")

    ws_url = _ws_url_from_base(SERVER_BASE_URL)
    print(f"[AGENT] Connecting to {ws_url} as device_id={DEVICE_ID}")

    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    async with aiohttp.ClientSession() as session:
        while not stop_event.is_set():
            try:
                async with session.ws_connect(ws_url, heartbeat=PING_INTERVAL_S) as ws:
                    await ws.send_str(json.dumps({"type": "auth", "device_id": DEVICE_ID, "secret": SHARED_SECRET}))
                    ack = await ws.receive(timeout=15)
                    if ack.type != aiohttp.WSMsgType.TEXT:
                        raise RuntimeError("No ack from server")
                    print(f"[AGENT] Connected: {ack.data}")

                    async def pinger():
                        while True:
                            await asyncio.sleep(PING_INTERVAL_S)
                            await ws.send_str(json.dumps({"type": "ping", "ts": _now_utc_iso()}))

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

                                await ws.send_str(json.dumps({
                                    "type": "result",
                                    "device_id": DEVICE_ID,
                                    "job_id": job_id,
                                    "results": results,
                                    "completed_at": _now_utc_iso(),
                                }))

                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                    finally:
                        ping_task.cancel()

            except Exception as e:
                print(f"[AGENT] Connection error: {e}")
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_agent())
