import os
import asyncio
from pathlib import Path
import time
import re
import uuid
from datetime import datetime
import base64
import secrets
from datetime import timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import List, Optional, Any, Dict

from src.config import env

from src.core.llm_adapter import LLMAdapter
from src.core.jarvis_brain import JarvisBrain
from src.core.executor import ActionExecutor
from src.core.chat_orchestrator import ChatOrchestrator
from src.core.notification_hub import notification_hub
from src.utils.git_sync import git_sync, setup_ssh_trust

# Self-update is optional and may pull in extra dependencies. Keep API boot resilient.
try:
    from src.utils.self_update import parse_voice_command, self_update_file, self_add_feature
    SELF_UPDATE_AVAILABLE = True
except Exception:
    parse_voice_command = None
    self_update_file = None
    self_add_feature = None
    SELF_UPDATE_AVAILABLE = False
from src.utils.voice_auth import voice_auth
from src.utils.auth_tokens import AuthTokens
from src.utils.db import db as database
from src.utils.email_generator import email_generator
from src.utils.screen_access import screen_access
from src.utils.app_manager import app_manager
from src.utils.task_manager import task_manager
from src.utils.error_handler import error_handler
from src.utils.telegram_bot import telegram_bot
from src.utils.session_manager import session_manager, start_session_cleanup_task
from src.utils.mcp_file_ops import file_ops
from src.agent.device_hub import DeviceHub

# Background scheduler (optional)
try:
    from src.jobs.job_scheduler import initialize_scheduler, shutdown_scheduler
    SCHEDULER_AVAILABLE = True
except Exception:
    initialize_scheduler = None
    shutdown_scheduler = None
    SCHEDULER_AVAILABLE = False

# Import system_operations safely (may fail on headless systems)
try:
    from src.utils.system_operations import system_ops
    SYSTEM_OPS_AVAILABLE = True
except (ImportError, KeyError, Exception) as e:
    SYSTEM_OPS_AVAILABLE = False
    logger = __import__('logging').getLogger(__name__)
    logger.warning(f"System operations not available: {e}")

# =========================================================
# FastAPI Initialization
# =========================================================


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup
    print("[OK] Jarvis server startup (lifespan)")
    try:
        database._ensure_connected()
        print("[DB] Connection check complete")
    except Exception as e:
        print(f"[INFO] DB error during startup (will retry): {e}")

    try:
        start_session_cleanup_task()
        print("[OK] Session cleanup task started")
    except Exception as e:
        print(f"[INFO] Could not start session cleanup (already running): {e}")

    if ENABLE_SCHEDULER and SCHEDULER_AVAILABLE and initialize_scheduler:
        try:
            initialize_scheduler()
        except Exception as e:
            print(f"[INFO] Scheduler failed to start: {e}")

    print("[OK] Jarvis server started.")

    try:
        yield
    finally:
        # Shutdown
        if SCHEDULER_AVAILABLE and shutdown_scheduler:
            try:
                shutdown_scheduler()
            except Exception:
                pass

        # Best-effort cleanup for async HTTP sessions.
        try:
            await _shutdown_cleanup()
        except Exception:
            pass


app = FastAPI(title="Jarvis Cloud Assistant", lifespan=lifespan)
load_dotenv()

START_TS = time.time()

# Serve frontend build if present (single-service deploy)
FRONTEND_BUILD_DIR = Path(__file__).resolve().parent / "jarvis-frontend" / "build"

# Enable/disable background scheduler via env
ENABLE_SCHEDULER = env.get_bool("JARVIS_ENABLE_SCHEDULER", True)

# =========================================================
# Runtime Mode / Security
# =========================================================
# Cloud mode is intended for hosted deployments (e.g., Render). In this mode we:
# - Require an authenticated session for chat + internet endpoints (to prevent public abuse)
# - Disable local/PC control and local filesystem endpoints (these are unsafe + meaningless in cloud)
CLOUD_MODE = env.get_bool("JARVIS_CLOUD_MODE", False)
VOICE_ONLY_MODE = env.get_bool("JARVIS_VOICE_ONLY", True)
PC_AGENT_ENABLED = env.get_bool("JARVIS_ENABLE_PC_AGENT", True)
AGENT_SHARED_SECRET = env.get_str("JARVIS_AGENT_SHARED_SECRET", "")
EXPOSE_AGENT_SHARED_SECRET = env.get_bool("JARVIS_EXPOSE_AGENT_SHARED_SECRET", False)
DEFAULT_DEVICE_ID = env.get_str("JARVIS_DEFAULT_DEVICE_ID", "primary")
DEVICE_OWNER_USERNAME = env.get_str("JARVIS_DEVICE_OWNER_USERNAME", "")
LOCAL_DEFAULT_DEVICE_FALLBACK = env.get_bool("JARVIS_LOCAL_DEFAULT_DEVICE_FALLBACK", True)
ADMIN_USERNAME = (env.get_str("JARVIS_ADMIN_USERNAME", "admin") or "admin").strip().lower()
ADMIN_BOOTSTRAP_SECRET = env.get_str("JARVIS_ADMIN_BOOTSTRAP_SECRET", "")

# =========================================================
# Small in-memory caches for hot paths
# =========================================================
# These are intentionally tiny + short-lived to reduce DB/file churn on endpoints
# like /api/agent/config that are called frequently by the UI.
import time

_ROLE_CACHE_TTL_S = 30
_ROLE_CACHE: dict[str, tuple[float, str]] = {}

_DEVICE_LOOKUP_CACHE_TTL_S = 60
_OWNER_TO_DEVICE_CACHE: dict[str, tuple[float, str | None]] = {}
_DEVICE_TO_OWNER_CACHE: dict[str, tuple[float, str | None]] = {}

_AGENT_CONFIG_CACHE_TTL_S = 120
_AGENT_CONFIG_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}

# Map job_id -> (ts, username, device_id, source_text) so we can route agent results
# back to the user over /ws/notifications.
_JOB_OWNER_CACHE_TTL_S = 10 * 60
_JOB_OWNER_CACHE: dict[str, tuple[float, str, str | None, str | None]] = {}

_DEVICE_REGISTRY_INDEX_READY = False
_DEVICE_PERMISSIONS_INDEX_READY = False
_AGENT_CONFIG_INDEX_READY = False


def _remember_job_owner(job: dict) -> None:
    """Best-effort: remember which authenticated user initiated this job."""
    try:
        if not isinstance(job, dict):
            return
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            return
        username = str(job.get("username") or "").strip().lower()
        if not username:
            return
        device_id = str(job.get("device_id") or "").strip().lower() or None
        source_text = str(job.get("source_text") or "").strip() or None

        now = time.time()
        _JOB_OWNER_CACHE[job_id] = (now, username, device_id, source_text)

        # Opportunistic pruning (keep it tiny; avoid background tasks).
        if len(_JOB_OWNER_CACHE) > 500:
            cutoff = now - _JOB_OWNER_CACHE_TTL_S
            for k, v in list(_JOB_OWNER_CACHE.items()):
                if not v or v[0] < cutoff:
                    _JOB_OWNER_CACHE.pop(k, None)
    except Exception:
        return


def _pop_job_owner(job_id: str | None) -> tuple[str | None, str | None, str | None]:
    """Return (username, device_id, source_text) for a recent job_id."""
    try:
        jid = str(job_id or "").strip()
        if not jid:
            return None, None, None
        now = time.time()
        item = _JOB_OWNER_CACHE.pop(jid, None)
        if not item:
            return None, None, None
        ts, username, device_id, source_text = item
        if (now - float(ts)) > _JOB_OWNER_CACHE_TTL_S:
            return None, None, None
        return username or None, device_id or None, source_text or None
    except Exception:
        return None, None, None


def _truncate_notification_payload(obj: Any, *, max_str: int = 2000, max_list: int = 30, max_depth: int = 4, _depth: int = 0):
    """Limit payload size to avoid large websocket frames (screenshots, logs, etc.)."""
    try:
        if _depth >= max_depth:
            return "…"
        if obj is None:
            return None
        if isinstance(obj, (int, float, bool)):
            return obj
        if isinstance(obj, str):
            s = obj
            if len(s) > max_str:
                return s[:max_str] + "…"
            return s
        if isinstance(obj, dict):
            out = {}
            # Keep keys stable; truncate values.
            for k, v in list(obj.items())[:200]:
                ks = str(k)
                if len(ks) > 120:
                    ks = ks[:120] + "…"
                out[ks] = _truncate_notification_payload(v, max_str=max_str, max_list=max_list, max_depth=max_depth, _depth=_depth + 1)
            return out
        if isinstance(obj, list):
            return [
                _truncate_notification_payload(v, max_str=max_str, max_list=max_list, max_depth=max_depth, _depth=_depth + 1)
                for v in obj[:max_list]
            ]
        return _truncate_notification_payload(str(obj), max_str=max_str, max_list=max_list, max_depth=max_depth, _depth=_depth + 1)
    except Exception:
        try:
            return str(obj)[:max_str] + ("…" if len(str(obj)) > max_str else "")
        except Exception:
            return "…"


def _require_pc_agent_enabled() -> None:
    if not PC_AGENT_ENABLED:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "PC agent features are disabled in this runtime",
                "hint": "This is expected for the desktop app. For web/cloud remote control, enable JARVIS_ENABLE_PC_AGENT=true.",
            },
        )


class _DisabledDeviceHub:
    async def send_job(self, device_id: str, job: dict) -> None:
        raise PermissionError("PC agent disabled")

    async def register_token(self, device_id: str, websocket: WebSocket, capabilities: dict) -> None:
        raise PermissionError("PC agent disabled")

    async def register(self, device_id: str, secret: str, websocket: WebSocket, capabilities: dict) -> None:
        raise PermissionError("PC agent disabled")

    async def unregister(self, device_id: str) -> None:
        return None

    async def touch(self, device_id: str) -> None:
        return None

    async def update_capabilities(self, device_id: str, capabilities: dict) -> None:
        return None

    async def get_agent(self, device_id: str):
        return None

    async def list_agents(self) -> dict:
        return {}

    async def is_connected(self, device_id: str) -> bool:
        return False


# Local/dev convenience: if no shared secret is configured, generate one so the UI
# can display it and legacy agent auth can still work. (Token auth is preferred.)
# Note: in the desktop app we disable PC-agent features by default.
if PC_AGENT_ENABLED and (not CLOUD_MODE) and (not AGENT_SHARED_SECRET):
    try:
        AGENT_SHARED_SECRET = secrets.token_urlsafe(32)
    except Exception:
        AGENT_SHARED_SECRET = os.urandom(32).hex()

device_hub = DeviceHub(shared_secret=AGENT_SHARED_SECRET) if PC_AGENT_ENABLED else _DisabledDeviceHub()
# Local/dev convenience: agent token issuance requires JARVIS_JWT_SECRET.
# In cloud mode this must be explicitly configured; in local mode we can
# generate a per-run secret so PC agent pairing works out of the box.
if (not CLOUD_MODE) and (not (os.getenv("JARVIS_JWT_SECRET") or "").strip()):
    try:
        os.environ["JARVIS_JWT_SECRET"] = secrets.token_urlsafe(48)
    except Exception:
        os.environ["JARVIS_JWT_SECRET"] = os.urandom(48).hex()

auth_tokens = AuthTokens()

PUBLIC_SERVER_URL = (env.get_str("JARVIS_PUBLIC_SERVER_URL", "https://jarvis-cloud-assistant.onrender.com") or "https://jarvis-cloud-assistant.onrender.com").strip().rstrip("/")
AGENT_TOKEN_TTL_SECONDS = env.get_int("JARVIS_AGENT_TOKEN_TTL_SECONDS", 2592000)  # 30d

def _get_principal(session_id: str | None) -> dict:
    """Return principal dict: {username, role, auth_type}.

    - If JWT is configured, role comes from JWT payload.
    - Otherwise, role comes from voice_auth user store.
    """
    if not session_id:
        return {"username": None, "role": "anonymous", "auth_type": "none"}

    if auth_tokens.secret:
        is_valid, username, payload = auth_tokens.verify(session_id)
        if not is_valid or not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please login again.",
            )
        # Prefer authoritative role from the user store (MongoDB/file), so role changes apply immediately.
        # Cache briefly to avoid repeated file/DB reads on hot endpoints.
        role = None
        try:
            u = (username or "").strip().lower()
            now = time.time()
            cached = _ROLE_CACHE.get(u)
            if cached and (now - cached[0]) < _ROLE_CACHE_TTL_S:
                role = cached[1]
            else:
                # Only trust the user store if the user actually exists there.
                # voice_auth.get_role() defaults to "user" for unknown users, which would
                # incorrectly override a valid JWT role.
                user_doc = voice_auth.get_user(username)
                if user_doc:
                    role = (user_doc.get("role") or "").strip().lower() or None
                    if role:
                        _ROLE_CACHE[u] = (now, str(role).strip().lower())
        except Exception:
            role = None
        if not role:
            role = ((payload or {}).get("role") or "user").strip().lower()
        if role not in ("user", "admin"):
            role = "user"
        return {"username": username, "role": role, "auth_type": "jwt", "token": payload}

    is_valid, username = voice_auth.validate_session(session_id)
    if not is_valid or not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session. Please login again.",
        )
    return {"username": username, "role": voice_auth.get_role(username), "auth_type": "legacy"}


def _require_admin_session(session_id: str | None) -> dict:
    p = _get_principal(session_id)
    if p.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required.",
        )
    return p


def _require_authenticated_session(session_id: str | None) -> dict:
    p = _get_principal(session_id)
    if not p.get("username"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please login first.",
        )
    return p

def _require_voice_session(session_id: str | None):
    """Return username if session is valid; raise HTTPException if required/invalid.

    In hosted deployments we use stateless JWT tokens so sessions survive restarts.
    """
    if CLOUD_MODE and not auth_tokens.secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfigured: JARVIS_JWT_SECRET is required in cloud mode.",
        )

    if CLOUD_MODE and not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please login first.",
        )
    if session_id:
        # Prefer JWT when configured; fallback to legacy in-memory sessions for local/dev.
        if auth_tokens.secret:
            is_valid, username, _payload = auth_tokens.verify(session_id)
            if not is_valid or not username:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired session. Please login again.",
                )
            return username

        is_valid, username = voice_auth.validate_session(session_id)
        if not is_valid or not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please login again.",
            )
        return username
    return None


ADMIN_ONLY_ACTION_TYPES = {
    # Local/PC control
    "open_app", "close_app", "switch_app",
    "execute_command",
    "set_brightness", "adjust_brightness",
    "set_power_plan", "set_energy_saver",
    "set_volume", "adjust_volume",
    "set_mute", "toggle_mute",
    "capture_screen", "screen_navigation",
    # Filesystem
    "read", "list", "mkdir",
    "write", "edit", "delete", "move", "copy", "cleanup",
    # Self-modifying
    "self_update", "self_add",
    # Error healing (may execute commands / install packages)
    "check_errors", "fix_errors", "check_render_logs",
    # Universal envelope
    "device_action",
}


READ_ONLY_ACTION_TYPES = {
    # Non-destructive information gathering
    "read", "list",
}

def _cloud_feature_disabled(feature: str):
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"{feature} is disabled in cloud deployments.",
    )


def _user_explicitly_requested_screen_capture(text: str) -> bool:
    """Best-effort guard to prevent accidental screen capture actions.

    The LLM may sometimes propose capture_screen even when not asked.
    We only allow capture unless user text clearly indicates it.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    keywords = (
        "screenshot",
        "screen shot",
        "capture screen",
        "take a screenshot",
        "what's on my screen",
        "whats on my screen",
        "read my screen",
        "read screen",
        "ocr",
    )
    return any(k in t for k in keywords)


def _user_explicitly_requested_research_open(text: str) -> bool:
    """Return True when the user explicitly asked for research + opening a source.

    This is used to add an open_url action *after* web-backed answering.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    # "research" is the primary explicit keyword the user asked for.
    # Opening phrases cover "open this url in new tab" style requests.
    research_markers = (
        "research",
        "do research",
        "make research",
        "research it",
        "research this",
        "find sources",
        "with sources",
        "with links",
        "with citations",
    )
    open_markers = (
        "open in new tab",
        "open this url",
        "open the url",
        "open the link",
        "open source",
        "open the source",
        "open it in browser",
        "open in browser",
    )
    return any(m in t for m in research_markers) or any(m in t for m in open_markers)


def _pick_best_source_url(action_results: list[dict]) -> str | None:
    """Pick a best-effort 'source' URL from web tool results."""
    try:
        prefer = (
            "wikipedia.org",
            "docs.",
            "developer.",
            "github.com",
            "nodejs.org",
            "python.org",
            "openai.com",
            "microsoft.com",
            "mozilla.org",
        )
        urls: list[str] = []
        for r in action_results or []:
            if not isinstance(r, dict):
                continue
            if (r.get("status") or "").lower() != "success":
                continue
            action = (r.get("action") or r.get("action_type") or "").lower()
            if action == "fetch_url":
                u = str(r.get("url") or "").strip()
                if u:
                    urls.append(u)
                continue
            if action in {"web_search", "search"}:
                for item in (r.get("results") or [])[:5]:
                    if not isinstance(item, dict):
                        continue
                    u = str(item.get("url") or "").strip()
                    if u:
                        urls.append(u)

        if not urls:
            return None

        for p in prefer:
            for u in urls:
                if p in u.lower():
                    return u
        return urls[0]
    except Exception:
        return None



def _require_device_owner(username: str | None):
    if DEVICE_OWNER_USERNAME and (username or "").lower() != DEVICE_OWNER_USERNAME.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not permitted to control the connected device.",
        )


def _device_registry_collection():
    """Collection mapping device_id <-> owner_username.

    Security model:
    - A device_id can be owned by at most one user.
    - A user can own at most one device_id.
    - Non-admin users can only assign an unowned device_id to themselves.
    """
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["device_registry"]
    global _DEVICE_REGISTRY_INDEX_READY
    if not _DEVICE_REGISTRY_INDEX_READY:
        try:
            col.create_index("device_id", unique=True)
        except Exception:
            pass
        try:
            col.create_index("owner_username", unique=True, sparse=True)
        except Exception:
            pass
        _DEVICE_REGISTRY_INDEX_READY = True
    return col


def _device_permissions_collection():
    """Collection storing approved device permissions.

    Document shape (upserted by device_id):
    - device_id: str
    - owner_username: str | None
    - permissions: { allow_app_control: bool, ... }
    - updated_at, updated_by
    """
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["device_permissions"]
    global _DEVICE_PERMISSIONS_INDEX_READY
    if not _DEVICE_PERMISSIONS_INDEX_READY:
        try:
            col.create_index("device_id", unique=True)
        except Exception:
            pass
        try:
            col.create_index("owner_username")
        except Exception:
            pass
        _DEVICE_PERMISSIONS_INDEX_READY = True
    return col


def _agent_config_collection():
    """Collection storing agent bootstrap config (no secrets).

    Document shape (upserted by device_id):
    - device_id: str
    - owner_username: str | None
    - server_url: str
    - updated_at, updated_by
    """
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["agent_configs"]
    global _AGENT_CONFIG_INDEX_READY
    if not _AGENT_CONFIG_INDEX_READY:
        try:
            col.create_index("device_id", unique=True)
        except Exception:
            pass
        try:
            col.create_index("owner_username")
        except Exception:
            pass
        _AGENT_CONFIG_INDEX_READY = True
    return col


def _skills_collection():
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["skills"]
    try:
        col.create_index([("owner", 1), ("name", 1)], unique=True)
    except Exception:
        pass
    return col


def _issue_agent_token(device_id: str, owner_username: str | None) -> str:
    if not auth_tokens.secret:
        raise HTTPException(status_code=500, detail="Server misconfigured: JARVIS_JWT_SECRET is required")
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=max(300, AGENT_TOKEN_TTL_SECONDS))
    jti = secrets.token_urlsafe(16)
    payload = {
        "iss": auth_tokens.issuer,
        "sub": device_id,
        "typ": "agent",
        "device_id": device_id,
        "owner": (owner_username or "").strip().lower() or None,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    # Use jose.jwt to avoid coupling to AuthTokens.issue() which assumes sub=username.
    from jose import jwt
    return jwt.encode(payload, auth_tokens.secret, algorithm="HS256")


def _get_saved_device_permissions(device_id: str | None) -> dict | None:
    col = _device_permissions_collection()
    if col is None:
        return None
    did = _normalize_device_id(device_id)
    if not did:
        return None
    doc = col.find_one({"device_id": did}, {"_id": 0, "permissions": 1})
    perms = (doc or {}).get("permissions")
    return perms if isinstance(perms, dict) else None


def _save_device_permissions(device_id: str, owner_username: str | None, permissions: dict, updated_by: str | None):
    col = _device_permissions_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    did = _normalize_device_id(device_id)
    owner = (owner_username or "").strip().lower() or None
    updater = (updated_by or "").strip().lower() or None
    now = datetime.utcnow()
    col.update_one(
        {"device_id": did},
        {
            "$set": {
                "device_id": did,
                "owner_username": owner,
                "permissions": permissions or {},
                "updated_at": now,
                "updated_by": updater,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def _normalize_device_id(device_id: str | None) -> str:
    return (device_id or "").strip().lower()


def _get_owner_device_id(owner_username: str | None) -> str | None:
    """Return the device_id owned by the given user (or None)."""
    owner = (owner_username or "").strip().lower()
    if not owner:
        return None

    now = time.time()
    cached = _OWNER_TO_DEVICE_CACHE.get(owner)
    if cached and (now - cached[0]) < _DEVICE_LOOKUP_CACHE_TTL_S:
        return cached[1]

    col = _device_registry_collection()
    if col is None:
        _OWNER_TO_DEVICE_CACHE[owner] = (now, None)
        return None
    doc = col.find_one({"owner_username": owner}, {"_id": 0, "device_id": 1})
    did = (doc or {}).get("device_id")
    did = _normalize_device_id(did) or None
    _OWNER_TO_DEVICE_CACHE[owner] = (now, did)
    if did:
        _DEVICE_TO_OWNER_CACHE[did] = (now, owner)
    return did


def _get_device_owner(device_id: str | None) -> str | None:
    """Return the owner_username for a device_id (or None)."""
    did = _normalize_device_id(device_id)
    if not did:
        return None

    now = time.time()
    cached = _DEVICE_TO_OWNER_CACHE.get(did)
    if cached and (now - cached[0]) < _DEVICE_LOOKUP_CACHE_TTL_S:
        return cached[1]

    col = _device_registry_collection()
    if col is None:
        _DEVICE_TO_OWNER_CACHE[did] = (now, None)
        return None
    doc = col.find_one({"device_id": did}, {"_id": 0, "owner_username": 1})
    owner = (doc or {}).get("owner_username")
    owner = (owner or "").strip().lower() or None
    _DEVICE_TO_OWNER_CACHE[did] = (now, owner)
    if owner:
        _OWNER_TO_DEVICE_CACHE[owner] = (now, did)
    return owner


def _set_device_owner(device_id: str, owner_username: str | None, updated_by: str | None = None):
    """Assign/unassign ownership for a device_id.

    If owner_username is None/empty, the device is unassigned.
    """
    col = _device_registry_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    did = _normalize_device_id(device_id)
    owner = (owner_username or "").strip().lower() or None
    updater = (updated_by or "").strip().lower() or None

    now = datetime.utcnow()
    if owner:
        col.update_one(
            {"device_id": did},
            {
                "$set": {
                    "device_id": did,
                    "owner_username": owner,
                    "updated_at": now,
                    "updated_by": updater,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    else:
        # Unassign ownership, keep the record for audit.
        col.update_one(
            {"device_id": did},
            {
                "$set": {
                    "device_id": did,
                    "owner_username": None,
                    "updated_at": now,
                    "updated_by": updater,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )


def _can_control_device(principal: dict) -> bool:
    """Device control permission.

    Cloud mode forces registrations to role=user. To keep the product usable,
    the configured device owner is allowed to control the device.
    """
    username = (principal or {}).get("username")
    role = (principal or {}).get("role")
    if role == "admin":
        return True
    if DEVICE_OWNER_USERNAME and username and username.lower() == DEVICE_OWNER_USERNAME.lower():
        return True
    try:
        return bool(_get_owner_device_id(username))
    except Exception:
        return False


def _public_user_profile(username: str | None) -> dict | None:
    """Return a safe subset of user data for the frontend.

    Never return voice hashes, password hashes, salts, or other secrets.
    """
    if not username:
        return None
    try:
        u = voice_auth.get_user(username) or {}
        assistant_name = (u.get("assistant_name") or "Jarvis").strip()
        # Keep assistant name reasonably short and safe for UI/wake-word usage.
        assistant_name = " ".join(assistant_name.split())[:24]
        if not assistant_name:
            assistant_name = "Jarvis"

        # Auto-detect whether biometrics are enrolled (no env flags required on frontend).
        try:
            embeds = u.get("voice_bio_embeddings")
            voice_bio_enrolled = isinstance(embeds, list) and len(embeds) > 0
        except Exception:
            voice_bio_enrolled = False
        return {
            "username": (u.get("username") or username).strip().lower(),
            "role": (u.get("role") or voice_auth.get_role(username) or "user").strip().lower(),
            "assistant_name": assistant_name,
            "voice_biometrics_enrolled": bool(voice_bio_enrolled),
            "created_at": u.get("created_at"),
            "last_login": u.get("last_login"),
            "updated_at": u.get("updated_at"),
        }
    except Exception:
        return {"username": (username or "").strip().lower(), "role": "user", "assistant_name": "Jarvis"}


class UserAssistantNameRequest(BaseModel):
    session_id: str
    assistant_name: str


class UserPreferencesGetRequest(BaseModel):
    session_id: str


class UserPreferencesUpdateRequest(BaseModel):
    session_id: str
    preferences: Dict[str, Any]
    mode: str = "merge"  # merge|replace


class AdminUserPreferencesGetRequest(BaseModel):
    session_id: str
    user_id: str


class AdminUserPreferencesUpdateRequest(BaseModel):
    session_id: str
    user_id: str
    preferences: Dict[str, Any]
    mode: str = "merge"  # merge|replace


def _is_jsonable(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_jsonable(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_jsonable(v) for k, v in value.items())
    return False


def _user_prefs_collection():
    # Lazily ensure DB connection and indexes
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    col = database.db["user_preferences"]
    try:
        col.create_index("user_id", unique=True)
    except Exception:
        pass
    return col


def _normalize_user_id(user_id: str | None) -> str:
    return (user_id or "").strip().lower()


@app.post("/api/user/assistant-name")
async def user_set_assistant_name(req: UserAssistantNameRequest):
    """Set the per-user assistant name (used for wake-word + UI display).

    Requires an authenticated session. The name is stored in the user's auth profile.
    """
    p = _require_authenticated_session(req.session_id)
    raw = (req.assistant_name or "").strip()
    # Basic validation: letters/numbers/spaces, 2..24 chars.
    cleaned = " ".join(raw.split())
    if len(cleaned) < 2 or len(cleaned) > 24:
        raise HTTPException(status_code=400, detail="assistant_name must be 2..24 characters")
    import re
    if not re.fullmatch(r"[A-Za-z0-9 ]+", cleaned):
        raise HTTPException(status_code=400, detail="assistant_name may contain only letters, numbers, and spaces")

    # Persist in user store
    result = voice_auth.update_user(p.get("username"), assistant_name=cleaned)
    if result.get("status") != "success":
        raise HTTPException(status_code=400, detail=result.get("message") or "Failed to update")

    return {"status": "success", "assistant_name": cleaned, "user": _public_user_profile(p.get("username"))}


@app.post("/api/user/preferences/get")
async def user_get_preferences(req: UserPreferencesGetRequest):
    """Get the authenticated user's stored preferences/habits."""
    p = _require_authenticated_session(req.session_id)
    user_id = _normalize_user_id(p.get("username"))
    col = _user_prefs_collection()
    doc = col.find_one({"user_id": user_id}, {"_id": 0, "preferences": 1}) or {}
    return {"status": "success", "user_id": user_id, "preferences": doc.get("preferences", {})}


@app.post("/api/user/preferences/set")
async def user_set_preferences(req: UserPreferencesUpdateRequest):
    """Update the authenticated user's preferences/habits.

    mode:
    - merge: upserts only the provided top-level keys
    - replace: replaces the entire preferences object
    """
    p = _require_authenticated_session(req.session_id)
    user_id = _normalize_user_id(p.get("username"))
    mode = (req.mode or "merge").strip().lower()
    if mode not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode must be 'merge' or 'replace'")
    prefs = req.preferences or {}
    if not isinstance(prefs, dict):
        raise HTTPException(status_code=400, detail="preferences must be an object")
    if not _is_jsonable(prefs):
        raise HTTPException(status_code=400, detail="preferences must be JSON-serializable")

    col = _user_prefs_collection()
    now = datetime.utcnow()

    if mode == "replace":
        col.update_one(
            {"user_id": user_id},
            {"$set": {"preferences": prefs, "updated_at": now}, "$setOnInsert": {"user_id": user_id, "created_at": now}},
            upsert=True,
        )
    else:
        set_doc: Dict[str, Any] = {"updated_at": now}
        for k, v in prefs.items():
            set_doc[f"preferences.{k}"] = v
        col.update_one(
            {"user_id": user_id},
            {"$set": set_doc, "$setOnInsert": {"user_id": user_id, "created_at": now}},
            upsert=True,
        )

    doc = col.find_one({"user_id": user_id}, {"_id": 0, "preferences": 1}) or {}
    return {"status": "success", "user_id": user_id, "preferences": doc.get("preferences", {})}


@app.post("/api/admin/user/preferences/get")
async def admin_get_user_preferences(req: AdminUserPreferencesGetRequest):
    """Admin-only: read any user's preferences/habits."""
    _require_admin_session(req.session_id)
    target_user_id = _normalize_user_id(req.user_id)
    if not target_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    col = _user_prefs_collection()
    doc = col.find_one({"user_id": target_user_id}, {"_id": 0, "preferences": 1}) or {}
    return {"status": "success", "user_id": target_user_id, "preferences": doc.get("preferences", {})}


@app.post("/api/admin/user/preferences/set")
async def admin_set_user_preferences(req: AdminUserPreferencesUpdateRequest):
    """Admin-only: write any user's preferences/habits."""
    p = _require_admin_session(req.session_id)
    target_user_id = _normalize_user_id(req.user_id)
    if not target_user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    mode = (req.mode or "merge").strip().lower()
    if mode not in ("merge", "replace"):
        raise HTTPException(status_code=400, detail="mode must be 'merge' or 'replace'")
    prefs = req.preferences or {}
    if not isinstance(prefs, dict):
        raise HTTPException(status_code=400, detail="preferences must be an object")
    if not _is_jsonable(prefs):
        raise HTTPException(status_code=400, detail="preferences must be JSON-serializable")

    col = _user_prefs_collection()
    now = datetime.utcnow()

    if mode == "replace":
        col.update_one(
            {"user_id": target_user_id},
            {"$set": {"preferences": prefs, "updated_at": now, "updated_by": _normalize_user_id(p.get("username"))},
             "$setOnInsert": {"user_id": target_user_id, "created_at": now}},
            upsert=True,
        )
    else:
        set_doc = {"updated_at": now, "updated_by": _normalize_user_id(p.get("username"))}
        for k, v in prefs.items():
            set_doc[f"preferences.{k}"] = v
        col.update_one(
            {"user_id": target_user_id},
            {"$set": set_doc, "$setOnInsert": {"user_id": target_user_id, "created_at": now}},
            upsert=True,
        )

    doc = col.find_one({"user_id": target_user_id}, {"_id": 0, "preferences": 1}) or {}
    return {"status": "success", "user_id": target_user_id, "preferences": doc.get("preferences", {})}


def _permissions_for(principal: dict) -> dict:
    """Compute permissions the UI can use to enable/disable features."""
    username = (principal or {}).get("username")
    role = (principal or {}).get("role")
    is_authed = bool(username)
    is_admin = role == "admin"
    return {
        "authenticated": is_authed,
        "role": role,
        "basic_info": is_authed,
        # Cloud->agent / device control
        "device_control": _can_control_device(principal) if is_authed else False,
        # Dangerous server-side actions (files, system execute, git, etc.)
        "admin_actions": bool(is_admin),
    }

def _is_remote_device_action(a: dict) -> bool:
    """Actions that are meaningful on the user's PC but unsafe/meaningless on Render."""
    t = (a or {}).get("type")
    return t in {
        "device_action",
        "open_app", "close_app", "switch_app",
        "execute_command",
        "set_brightness", "adjust_brightness",
        "set_power_plan", "set_energy_saver",
        "set_volume", "adjust_volume",
        "set_mute", "toggle_mute",
        "capture_screen", "screen_navigation",
        # Filesystem actions should run on the user's machine (agent), not on Render.
        "read", "list", "mkdir",
        "write", "edit", "delete", "move", "copy", "cleanup",
        "self_update", "self_add",
    }

async def _dispatch_actions_to_device(device_id: str, username: str, actions: list[dict], source_text: str):
    """Forward actions to a connected local agent."""
    _require_pc_agent_enabled()
    job = {
        "job_id": f"job_{os.urandom(8).hex()}",
        "device_id": device_id,
        "username": username,
        "source_text": source_text,
        "actions": actions,
    }
    _remember_job_owner(job)
    await device_hub.send_job(device_id, job)
    return job


# =========================================================
# Remote Agent (WebSocket)
# =========================================================
@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    await ws.accept()

    if not PC_AGENT_ENABLED:
        # Desktop mode: do not accept agent connections.
        try:
            # Keep payload shape consistent with _auth_fail so clients can show
            # a specific error reason.
            await ws.send_json({"type": "error", "error": "auth_failed", "reason": "pc_agent_disabled"})
        except Exception:
            pass
        try:
            await ws.close(code=1008)
        except Exception:
            pass
        return

    device_id: str | None = None

    async def _auth_fail(reason: str):
        # Send a small error payload so the PC agent can show a helpful message.
        # Keep it generic enough to avoid leaking sensitive server configuration.
        try:
            await ws.send_json({"type": "error", "error": "auth_failed", "reason": reason})
        except Exception:
            pass
        try:
            await ws.close(code=1008)
        except Exception:
            pass

    try:
        # Expect initial auth message
        raw = await ws.receive_text()
        msg = {}
        try:
            import json
            msg = json.loads(raw)
        except Exception:
            await _auth_fail("invalid_json")
            return

        if msg.get("type") != "auth":
            await _auth_fail("expected_auth")
            return

        token = (msg.get("token") or "").strip()
        # Normalize to avoid case-sensitive mismatches (e.g. AVADH vs avadh).
        device_id = _normalize_device_id(msg.get("device_id")) or None
        secret = (msg.get("secret") or "").strip()
        capabilities = msg.get("capabilities") or {}

        # Preferred auth: JWT agent token issued by /api/agent/config.
        if token:
            if not auth_tokens.secret:
                await _auth_fail("server_missing_jwt_secret")
                return
            ok, _sub, payload = auth_tokens.verify(token)
            if not ok or not payload or payload.get("typ") != "agent":
                # Most common cause: user pasted a login/session JWT instead of the agent token.
                await _auth_fail("invalid_agent_token")
                return
            did = _normalize_device_id(payload.get("device_id") or payload.get("sub"))
            if not did:
                await _auth_fail("missing_device_id")
                return
            device_id = did
            try:
                await device_hub.register_token(device_id=device_id, websocket=ws, capabilities=capabilities if isinstance(capabilities, dict) else {})
            except PermissionError:
                await _auth_fail("device_not_authorized")
                return
        else:
            # Legacy auth: shared secret.
            try:
                await device_hub.register(device_id=device_id or "", secret=secret, websocket=ws, capabilities=capabilities if isinstance(capabilities, dict) else {})
            except PermissionError:
                await _auth_fail("invalid_shared_secret")
                return

        await ws.send_json({"type": "ack", "device_id": device_id, "status": "connected"})

        # If we have previously approved permissions for this device, apply them automatically.
        try:
            saved = _get_saved_device_permissions(device_id)
            if saved and isinstance(saved, dict):
                await _dispatch_actions_to_device(
                    device_id=device_id,
                    username="system",
                    actions=[{"type": "agent_set_permissions", "permissions": saved}],
                    source_text="auto_apply_permissions",
                )
        except Exception:
            pass

        # Main loop
        while True:
            incoming = await ws.receive_text()
            try:
                import json
                payload = json.loads(incoming)
            except Exception:
                continue

            if payload.get("type") == "ping":
                await device_hub.touch(device_id)
                await ws.send_json({"type": "pong"})
                continue

            if payload.get("type") == "capabilities":
                await device_hub.touch(device_id)
                caps = payload.get("capabilities") or {}
                if isinstance(caps, dict):
                    await device_hub.update_capabilities(device_id, caps)
                await ws.send_json({"type": "ok"})
                continue

            if payload.get("type") == "result":
                # For now, just log results. You can persist to DB later.
                await device_hub.touch(device_id)
                logger = __import__('logging').getLogger(__name__)
                logger.info("[AGENT RESULT] %s", payload)

                # Publish to the user who initiated this job (best-effort).
                try:
                    jid = (payload or {}).get("job_id")
                    user, did, source_text = _pop_job_owner(jid)
                    if user:
                        await notification_hub.publish(
                            user,
                            {
                                "type": "device_job_result",
                                "job_id": str(jid or ""),
                                "device_id": did or device_id,
                                "source_text": source_text,
                                "results": _truncate_notification_payload((payload or {}).get("results") or []),
                                "received_at": datetime.utcnow().isoformat(),
                            },
                        )
                except Exception:
                    pass
                continue

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger = __import__('logging').getLogger(__name__)
        logger.warning("Agent ws error: %s", e)
    finally:
        if device_id:
            await device_hub.unregister(device_id)


# =========================================================
# Agent Config / Bootstrap (Cloud -> PC Agent)
# =========================================================
class AgentConfigRequest(BaseModel):
    session_id: str
    device_id: str | None = None


@app.post("/api/agent/config")
async def agent_config(req: AgentConfigRequest, background_tasks: BackgroundTasks):
    """Return agent connection config stored in MongoDB.

    This avoids keeping secrets in local .env files. The server issues a JWT agent token
    that the PC agent can present over /ws/agent.
    """
    _require_pc_agent_enabled()
    p = _require_authenticated_session(req.session_id)
    username = (p.get("username") or "").strip().lower()
    role = (p.get("role") or "user").strip().lower()

    did = None
    if req.device_id:
        did = _validate_device_id_or_400(req.device_id)
    else:
        # Prefer user's assigned device
        did = _get_owner_device_id(username)
        if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
            did = DEFAULT_DEVICE_ID

    if not did:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "No device assigned to this user",
                "action": "configure_pc",
                "hint": "Configure a device via /api/user/device/configure (auto-pick) or /api/user/device/set (explicit device_id).",
            },
        )

    # Non-admin users can only request config for their own device.
    if role != "admin":
        owned = _get_owner_device_id(username)
        if owned and _normalize_device_id(owned) != _normalize_device_id(did):
            raise HTTPException(status_code=403, detail="Users cannot request agent config for another device")

    # Fast-path: return a recent cached config (avoids repeated DB lookups and token re-issuance).
    try:
        cache_key = (req.session_id, _normalize_device_id(did))
        cached = _AGENT_CONFIG_CACHE.get(cache_key)
        if cached and (time.time() - cached[0]) < _AGENT_CONFIG_CACHE_TTL_S:
            return cached[1]
    except Exception:
        pass

    # Record config in MongoDB (no secrets stored).
    # Do this in a background task so the UI gets the token immediately.
    def _persist_agent_cfg():
        try:
            col = _agent_config_collection()
            if col is None:
                return
            col.update_one(
                {"device_id": did},
                {
                    "$set": {
                        "device_id": did,
                        "owner_username": username if role != "admin" else (_get_device_owner(did) or None),
                        "server_url": PUBLIC_SERVER_URL,
                        "updated_at": datetime.utcnow(),
                        "updated_by": username,
                    },
                    "$setOnInsert": {"created_at": datetime.utcnow()},
                },
                upsert=True,
            )
        except Exception:
            return

    try:
        background_tasks.add_task(_persist_agent_cfg)
    except Exception:
        pass

    token = _issue_agent_token(device_id=did, owner_username=username)
    ws_url = ("wss://" + PUBLIC_SERVER_URL[len("https://"):] + "/ws/agent") if PUBLIC_SERVER_URL.startswith("https://") else ("ws://" + PUBLIC_SERVER_URL[len("http://"):] + "/ws/agent")
    payload = {
        "status": "success",
        "device_id": did,
        "server_url": PUBLIC_SERVER_URL,
        "ws_url": ws_url,
        "agent_token": token,
        "expires_in_seconds": max(300, AGENT_TOKEN_TTL_SECONDS),
    }

    # Only expose the shared secret for local/dev setups.
    # In cloud mode, avoid leaking a global secret unless explicitly enabled.
    if AGENT_SHARED_SECRET and (not CLOUD_MODE or EXPOSE_AGENT_SHARED_SECRET):
        # If explicitly enabled in cloud mode, restrict to admins.
        if (not CLOUD_MODE) or (role == "admin"):
            payload["agent_shared_secret"] = AGENT_SHARED_SECRET

    # Cache briefly to make refresh/login feel instant.
    try:
        _AGENT_CONFIG_CACHE[(req.session_id, _normalize_device_id(did))] = (time.time(), payload)
    except Exception:
        pass

    return payload


# =========================================================
# Speech-to-Text (Mobile fallback)
# =========================================================

GOOGLE_SPEECH_ENABLED = env.get_bool("GOOGLE_SPEECH_ENABLED", False)
GOOGLE_SPEECH_LANGUAGE_DEFAULT = env.get_str("GOOGLE_SPEECH_LANGUAGE_DEFAULT", "en-US")
GOOGLE_SPEECH_CREDENTIALS_JSON = env.get_str("GOOGLE_SPEECH_CREDENTIALS_JSON", "").strip()
GOOGLE_SPEECH_CREDENTIALS_B64 = env.get_str("GOOGLE_SPEECH_CREDENTIALS_B64", "").strip()


def _get_google_speech_client_and_creds():
    if not GOOGLE_SPEECH_ENABLED:
        raise HTTPException(status_code=501, detail="Google Speech-to-Text is disabled (set GOOGLE_SPEECH_ENABLED=true)")

    try:
        from google.cloud import speech
    except Exception:
        raise HTTPException(status_code=501, detail="google-cloud-speech is not installed")

    credentials = None

    raw_json = None
    if GOOGLE_SPEECH_CREDENTIALS_JSON:
        raw_json = GOOGLE_SPEECH_CREDENTIALS_JSON
    elif GOOGLE_SPEECH_CREDENTIALS_B64:
        try:
            raw_json = base64.b64decode(GOOGLE_SPEECH_CREDENTIALS_B64).decode("utf-8")
        except Exception:
            raise HTTPException(status_code=500, detail="Invalid GOOGLE_SPEECH_CREDENTIALS_B64")

    if raw_json:
        try:
            import json
            from google.oauth2 import service_account

            info = json.loads(raw_json)
            credentials = service_account.Credentials.from_service_account_info(info)
        except Exception:
            raise HTTPException(status_code=500, detail="Invalid Google service account JSON in env")

    try:
        client = speech.SpeechClient(credentials=credentials) if credentials else speech.SpeechClient()
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to initialize Google Speech client")

    return speech, client


class GoogleSTTRequest(BaseModel):
    session_id: str
    audio_b64: str
    sample_rate_hz: int = 16000
    language: str | None = None


@app.post("/api/stt/google")
async def stt_google(req: GoogleSTTRequest):
    _require_authenticated_session(req.session_id)

    if not req.audio_b64 or len(req.audio_b64) > 6_000_000:
        raise HTTPException(status_code=413, detail="Audio payload too large")

    try:
        audio_bytes = base64.b64decode(req.audio_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid audio_b64")

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio")

    if req.sample_rate_hz < 8000 or req.sample_rate_hz > 48000:
        raise HTTPException(status_code=400, detail="sample_rate_hz must be between 8000 and 48000")

    speech, client = _get_google_speech_client_and_creds()

    language_code = (req.language or GOOGLE_SPEECH_LANGUAGE_DEFAULT or "en-US").strip() or "en-US"
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=int(req.sample_rate_hz),
        language_code=language_code,
        enable_automatic_punctuation=True,
        model="latest_short",
    )
    audio = speech.RecognitionAudio(content=audio_bytes)

    try:
        response = client.recognize(config=config, audio=audio)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google STT failed: {e}")

    text = ""
    try:
        for result in response.results:
            if result.alternatives:
                text += (result.alternatives[0].transcript or "")
    except Exception:
        text = ""

    return {
        "status": "success",
        "text": (text or "").strip(),
        "language": language_code,
        "sample_rate_hz": int(req.sample_rate_hz),
    }


# =========================================================
# Remote Device Control APIs (Cloud -> Agent)
# =========================================================
class UserDeviceGetRequest(BaseModel):
    session_id: str


class UserDeviceSetRequest(BaseModel):
    session_id: str
    device_id: str


class UserDeviceConfigureRequest(BaseModel):
    session_id: str
    device_id: str | None = None


class AdminDeviceAssignRequest(BaseModel):
    session_id: str
    device_id: str
    owner_username: str | None = None


class AdminDeviceListRequest(BaseModel):
    session_id: str


def _validate_device_id_or_400(device_id: str) -> str:
    did = _normalize_device_id(device_id)
    if not did:
        raise HTTPException(status_code=400, detail="device_id is required")
    import re
    if not re.fullmatch(r"[a-z0-9_-]{3,32}", did):
        raise HTTPException(status_code=400, detail="device_id must be 3..32 chars: a-z, 0-9, '_' or '-'")
    return did


@app.post("/api/user/device/get")
async def user_get_device(req: UserDeviceGetRequest):
    p = _require_authenticated_session(req.session_id)
    user_id = (p.get("username") or "").strip().lower()
    did = _get_owner_device_id(user_id)
    # legacy fallback for single-device setups
    if not did and DEVICE_OWNER_USERNAME and user_id == DEVICE_OWNER_USERNAME.lower():
        did = DEFAULT_DEVICE_ID
    return {"status": "success", "user_id": user_id, "device_id": did}


@app.post("/api/user/device/set")
async def user_set_device(req: UserDeviceSetRequest):
    """Bind the authenticated user to a device_id.

    Users cannot claim a device owned by another user.
    """
    p = _require_authenticated_session(req.session_id)
    user_id = (p.get("username") or "").strip().lower()
    did = _validate_device_id_or_400(req.device_id)

    current_owner = _get_device_owner(did)
    if current_owner and current_owner != user_id:
        raise HTTPException(status_code=403, detail="This device_id is already assigned to another user")

    # Unassign any previous device from this user, then assign the new one.
    prev = _get_owner_device_id(user_id)
    if prev and prev != did:
        _set_device_owner(prev, None, updated_by=user_id)

    _set_device_owner(did, user_id, updated_by=user_id)
    return {"status": "success", "user_id": user_id, "device_id": did}


@app.post("/api/user/device/configure")
async def user_configure_device(req: UserDeviceConfigureRequest):
    """Single-call setup: bind the authenticated user to their connected PC agent.

    Behavior:
    - If device_id is provided, attempt to claim/use it (must not be owned by another user).
    - If the user already has a device, return it.
    - Otherwise, auto-pick from connected agents:
      - If exactly one unowned agent is connected, claim it.
      - If multiple candidates exist, ask the user to specify a device_id.
    """
    _require_pc_agent_enabled()
    p = _require_authenticated_session(req.session_id)
    user_id = (p.get("username") or "").strip().lower()

    if req.device_id:
        did = _validate_device_id_or_400(req.device_id)
        current_owner = _get_device_owner(did)
        if current_owner and current_owner != user_id:
            raise HTTPException(status_code=403, detail="No permission: this device_id is assigned to another user")
        prev = _get_owner_device_id(user_id)
        if prev and prev != did:
            _set_device_owner(prev, None, updated_by=user_id)
        _set_device_owner(did, user_id, updated_by=user_id)
        agent = await device_hub.get_agent(did)
        return {
            "status": "success",
            "user_id": user_id,
            "device_id": did,
            "connected": bool(agent),
            "capabilities": (agent or {}).get("capabilities", {}),
            "message": f"Configured your PC as device '{did}'.",
        }

    existing = _get_owner_device_id(user_id)
    if existing:
        agent = await device_hub.get_agent(existing)
        return {
            "status": "success",
            "user_id": user_id,
            "device_id": existing,
            "connected": bool(agent),
            "capabilities": (agent or {}).get("capabilities", {}),
            "message": f"Your PC is already configured as device '{existing}'.",
        }

    agents_by_id = await device_hub.list_agents()
    if not agents_by_id:
        raise HTTPException(status_code=409, detail="No PC agent is connected. Start pc_agent.py on your PC and try again.")

    # Prefer unowned devices; allow already-owned-by-user (none in this branch).
    candidates = []
    for did in agents_by_id.keys():
        owner = _get_device_owner(did)
        if not owner:
            candidates.append(did)

    if len(candidates) == 1:
        did = candidates[0]
        _set_device_owner(did, user_id, updated_by=user_id)
        agent = await device_hub.get_agent(did)
        return {
            "status": "success",
            "user_id": user_id,
            "device_id": did,
            "connected": True,
            "capabilities": (agent or {}).get("capabilities", {}),
            "message": f"Configured your PC as device '{did}'.",
        }

    if not candidates:
        raise HTTPException(status_code=403, detail="No permission: all connected PCs are already assigned to other users")

    raise HTTPException(
        status_code=409,
        detail={
            "message": "Multiple PCs are connected. Specify which one to configure.",
            "available_device_ids": sorted(candidates)[:20],
        },
    )


@app.post("/api/admin/device/assign")
async def admin_assign_device(req: AdminDeviceAssignRequest):
    """Admin-only: assign/unassign devices to users."""
    p = _require_admin_session(req.session_id)
    admin_user = (p.get("username") or "").strip().lower()
    did = _validate_device_id_or_400(req.device_id)
    owner = (req.owner_username or "").strip().lower() or None

    # If assigning to a user, ensure uniqueness on both sides.
    if owner:
        existing_owner = _get_device_owner(did)
        if existing_owner and existing_owner != owner:
            # force reassignment by first unassigning
            _set_device_owner(did, None, updated_by=admin_user)
        prev = _get_owner_device_id(owner)
        if prev and prev != did:
            _set_device_owner(prev, None, updated_by=admin_user)
        _set_device_owner(did, owner, updated_by=admin_user)
        return {"status": "success", "device_id": did, "owner_username": owner}

    # Unassign
    _set_device_owner(did, None, updated_by=admin_user)
    return {"status": "success", "device_id": did, "owner_username": None}


@app.post("/api/admin/device/list")
async def admin_list_devices(req: AdminDeviceListRequest):
    _require_admin_session(req.session_id)
    col = _device_registry_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    items = list(col.find({}, {"_id": 0}).sort("updated_at", -1).limit(200))
    return {"status": "success", "devices": items}


class DeviceDispatchRequest(BaseModel):
    session_id: str | None = None
    device_id: str | None = None
    owner_username: str | None = None
    actions: List[dict]
    source_text: str | None = ""


@app.post("/api/device/dispatch")
async def device_dispatch(req: DeviceDispatchRequest):
    """Forward actions to a connected PC agent.

    This is only meaningful in cloud mode.
    """
    _require_pc_agent_enabled()
    p = _require_authenticated_session(req.session_id)
    username = (p.get("username") or "").strip().lower()
    role = (p.get("role") or "user").strip().lower()

    did = None
    if role == "admin":
        if req.device_id:
            did = _validate_device_id_or_400(req.device_id)
        elif req.owner_username:
            did = _get_owner_device_id(req.owner_username)
        else:
            # Default admin behavior: target the admin's own assigned device when present.
            did = _get_owner_device_id(username) or DEFAULT_DEVICE_ID
    else:
        if req.device_id and _normalize_device_id(req.device_id) != _normalize_device_id(_get_owner_device_id(username) or ""):
            raise HTTPException(status_code=403, detail="Users cannot dispatch to another device")
        did = _get_owner_device_id(username)
        if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
            did = DEFAULT_DEVICE_ID
        if not did:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "No device assigned to this user",
                    "action": "configure_pc",
                    "hint": "Configure a device via /api/user/device/configure (auto-pick) or /api/user/device/set (explicit device_id).",
                },
            )

    if not await device_hub.is_connected(did):
        raise HTTPException(status_code=409, detail={
            "message": "Device agent is not connected",
            "device_id": did,
            "hint": "Start pc_agent.py on the target PC and ensure JARVIS_SERVER_URL and JARVIS_AGENT_SHARED_SECRET match the server.",
        })

    actions = req.actions or []
    if not isinstance(actions, list):
        raise HTTPException(status_code=400, detail={
            "message": "Invalid actions payload",
            "hint": "actions must be a JSON array of objects like {type: 'open_app', ...}",
        })

    # Validate action objects early so we fail fast with a helpful message.
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            raise HTTPException(status_code=400, detail={
                "message": "Invalid action",
                "index": i,
                "hint": "Each action must be an object with at least a 'type' field.",
            })
        t = (a.get("type") or "").strip()
        if not t:
            raise HTTPException(status_code=400, detail={
                "message": "Invalid action",
                "index": i,
                "hint": "Each action must include a non-empty 'type'.",
            })

    agent = await device_hub.get_agent(did)
    caps = (agent or {}).get("capabilities") or None
    if not caps:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Agent is connected but did not report capabilities",
                "action": "update_pc_agent",
                "hint": "Update pc_agent.py and restart the agent so it reports capabilities on connect.",
                "device_id": did,
            },
        )

    saved_perms = _get_saved_device_permissions(did) or {}

    def _capability_requirement(action_type: str):
        t = (action_type or "").strip()
        if t == "device_action":
            # Capability is derived from the nested action name.
            return None
        if t in ("open_app", "close_app", "switch_app"):
            return ("allow_app_control", "JARVIS_AGENT_ALLOW_APP_CONTROL")
        if t in (
            "execute_command",
            "set_brightness", "adjust_brightness",
            "set_power_plan", "set_energy_saver",
            "set_volume", "adjust_volume",
            "set_mute", "toggle_mute",
            # Universal device actions (names)
            "lock_screen",
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
            "set_airplane_mode",
        ):
            return ("allow_execute_command", "JARVIS_AGENT_ALLOW_EXECUTE_COMMAND")
        if t in ("capture_screen", "screen_navigation", "type_text", "press_key", "hotkey"):
            return ("allow_screen", "JARVIS_AGENT_ALLOW_SCREEN")
        # Universal device actions (names) that operate by keystrokes/UI automation.
        if t in (
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
        ):
            return ("allow_screen", "JARVIS_AGENT_ALLOW_SCREEN")
        if t in (
            "find_files",
        ):
            return ("allow_file_ops", "JARVIS_AGENT_ALLOW_FILE_OPS")
        if t in ("read", "write", "edit", "delete", "move", "copy", "list", "mkdir", "cleanup"):
            return ("allow_file_ops", "JARVIS_AGENT_ALLOW_FILE_OPS")
        if t in ("self_update", "self_add"):
            return ("allow_self_update", "JARVIS_AGENT_ALLOW_SELF_UPDATE")
        return None

    for a in actions:
        at = (a or {}).get("type") or ""
        req_cap = _capability_requirement(at)
        if at == "device_action":
            nm = (a or {}).get("name") or (a or {}).get("action") or ""
            req_cap = _capability_requirement(str(nm))
        if not req_cap:
            continue
        key, env_name = req_cap
        if not bool(caps.get(key)):
            # If already approved previously, auto-apply on the running agent.
            if bool(saved_perms.get(key)):
                try:
                    await _dispatch_actions_to_device(
                        did,
                        username=username or "user",
                        actions=[{"type": "agent_set_permissions", "permissions": {key: True}}],
                        source_text="auto_grant_preapproved",
                    )
                    caps[key] = True
                    continue
                except Exception:
                    pass
            raise HTTPException(status_code=403, detail={
                "message": f"No permission: '{at}' is disabled on your PC agent.",
                "action_type": at,
                "required_capability": key,
                "env_var": env_name,
                "suggestion": f"Enable {env_name}=true on the PC agent.",
            })

    job = await _dispatch_actions_to_device(did, username=username or "user", actions=actions, source_text=req.source_text or "")
    return {"status": "queued", "job": job, "request_id": f"disp_{uuid.uuid4().hex[:12]}"}


# =========================================================
# Skills API
# =========================================================
class SkillUpsertRequest(BaseModel):
    session_id: str
    name: str
    description: str | None = None
    type: str | None = "n8n"
    path: str | None = None
    enabled: bool | None = True
    version: str | None = "1.0"
    tags: List[str] | None = None
    inputs: Dict[str, Any] | None = None
    outputs: Dict[str, Any] | None = None
    trigger_phrases: List[str] | None = None
    n8n_workflow_id: str | None = None


class SkillUpdateRequest(BaseModel):
    session_id: str
    name: str
    updates: Dict[str, Any]


def _slugify_skill(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_\- ]+", "", name or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    return s[:60] if s else "skill"


async def _maybe_create_n8n_webhook_workflow(name: str, path: str) -> str | None:
    """Best-effort N8N workflow creation when credentials are configured."""
    api_base = (env.get_str("N8N_API_URL", "") or "").strip().rstrip("/")
    api_key = (env.get_str("N8N_API_KEY", "") or "").strip()
    auto_create = env.get_bool("JARVIS_N8N_AUTO_CREATE_SKILL_WEBHOOK", False)
    if not api_base or not api_key or not auto_create:
        return None

    try:
        import aiohttp
        headers = {"Content-Type": "application/json", "X-N8N-API-KEY": api_key}
        workflow = {
            "name": f"Jarvis Skill: {name}",
            "nodes": [
                {
                    "parameters": {
                        "httpMethod": "POST",
                        "path": path,
                        "responseMode": "onReceived"
                    },
                    "id": "webhook",
                    "name": "Webhook",
                    "type": "n8n-nodes-base.webhook",
                    "typeVersion": 1,
                    "position": [240, 300]
                }
            ],
            "connections": {}
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.post(f"{api_base}/workflows", headers=headers, json=workflow) as resp:
                data = await resp.json()
                if resp.status >= 200 and resp.status < 300:
                    return str(data.get("id") or "") or None
    except Exception:
        return None
    return None


@app.post("/api/skills/list")
async def skills_list(req: dict):
    """List skills for the authenticated user."""
    session_id = (req or {}).get("session_id")
    p = _require_authenticated_session(session_id)
    col = _skills_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    items = list(col.find({"owner": _normalize_user_id(p.get("username"))}, {"_id": 0}).sort("name", 1))
    return {"status": "success", "skills": items}


@app.post("/api/skills/add")
async def skills_add(req: SkillUpsertRequest):
    p = _require_authenticated_session(req.session_id)
    role = (p.get("role") or "user").strip().lower()
    write_role = (env.get_str("JARVIS_SKILLS_WRITE_ROLE", "user") or "user").strip().lower()
    if write_role == "admin" and role != "admin":
        raise HTTPException(status_code=403, detail="Admin required to add skills")

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Skill name required")

    path = (req.path or "").strip()
    if not path:
        path = f"skills/{_slugify_skill(name)}"

    owner = _normalize_user_id(p.get("username"))
    skill = {
        "owner": owner,
        "name": name,
        "description": (req.description or "").strip() or None,
        "type": (req.type or "n8n").strip().lower(),
        "path": path or None,
        "enabled": bool(req.enabled) if req.enabled is not None else True,
        "version": (req.version or "1.0").strip(),
        "tags": req.tags or [],
        "inputs": req.inputs or {"query": "string"},
        "outputs": req.outputs or {"result": "string"},
        "trigger_phrases": req.trigger_phrases or [f"run skill {name}"],
        "n8n_workflow_id": (req.n8n_workflow_id or "").strip() or None,
        "updated_at": datetime.utcnow(),
    }

    # Optional auto-create N8N webhook workflow
    if skill.get("type") == "n8n" and not skill.get("n8n_workflow_id"):
        wf_id = await _maybe_create_n8n_webhook_workflow(name=name, path=path)
        if wf_id:
            skill["n8n_workflow_id"] = wf_id

    col = _skills_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    col.update_one(
        {"owner": owner, "name": name},
        {"$set": skill, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return {"status": "success", "skill": skill}


@app.post("/api/skills/update")
async def skills_update(req: SkillUpdateRequest):
    p = _require_authenticated_session(req.session_id)
    role = (p.get("role") or "user").strip().lower()
    write_role = (env.get_str("JARVIS_SKILLS_WRITE_ROLE", "user") or "user").strip().lower()
    if write_role == "admin" and role != "admin":
        raise HTTPException(status_code=403, detail="Admin required to update skills")

    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Skill name required")
    if not isinstance(req.updates, dict) or not req.updates:
        raise HTTPException(status_code=400, detail="updates must be an object")

    owner = _normalize_user_id(p.get("username"))
    allowed = {"description", "type", "path", "enabled", "version", "tags", "inputs", "outputs", "trigger_phrases", "n8n_workflow_id"}
    updates = {k: v for k, v in req.updates.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid update fields")
    updates["updated_at"] = datetime.utcnow()

    col = _skills_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    col.update_one({"owner": owner, "name": name}, {"$set": updates})
    return {"status": "success"}


class DevicePermissionsGrantRequest(BaseModel):
    session_id: str
    device_id: str | None = None
    owner_username: str | None = None
    permissions: Dict[str, bool]


@app.get("/api/device/permissions")
async def device_permissions_get(session_id: str, device_id: str | None = None, owner_username: str | None = None):
    """Get saved device permissions for a device.

    Used by the frontend to determine whether the user has already granted permissions
    (so the UI can prompt/attempt agent start appropriately).
    """
    _require_pc_agent_enabled()
    p = _require_authenticated_session(session_id)
    username = (p.get("username") or "").strip().lower()
    role = (p.get("role") or "user").strip().lower()

    did = None
    if role == "admin":
        if device_id:
            did = _validate_device_id_or_400(device_id)
        elif owner_username:
            did = _get_owner_device_id(owner_username)
        else:
            did = _get_owner_device_id(username) or DEFAULT_DEVICE_ID
    else:
        if device_id and _normalize_device_id(device_id) != _normalize_device_id(_get_owner_device_id(username) or ""):
            raise HTTPException(status_code=403, detail="Users cannot view permissions for another device")
        did = _get_owner_device_id(username)
        if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
            did = DEFAULT_DEVICE_ID
        if not did:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "No device assigned to this user",
                    "action": "configure_pc",
                    "hint": "Configure a device via /api/user/device/configure (auto-pick) or /api/user/device/set (explicit device_id).",
                },
            )

    saved = _get_saved_device_permissions(did) or {}
    connected = False
    try:
        connected = bool(await device_hub.is_connected(did))
    except Exception:
        connected = False

    return {"status": "success", "device_id": did, "permissions": saved, "connected": connected}


@app.post("/api/device/permissions/grant")
async def device_permissions_grant(req: DevicePermissionsGrantRequest):
    """Grant/deny runtime permissions on the connected PC agent.

    This is used by the frontend permission popup. It only affects the running agent
    process (no env change) and requires that the device is connected.
    """
    _require_pc_agent_enabled()
    p = _require_authenticated_session(req.session_id)
    username = (p.get("username") or "").strip().lower()
    role = (p.get("role") or "user").strip().lower()

    did = None
    if role == "admin":
        if req.device_id:
            did = _validate_device_id_or_400(req.device_id)
        elif req.owner_username:
            did = _get_owner_device_id(req.owner_username)
        else:
            did = _get_owner_device_id(username) or DEFAULT_DEVICE_ID
    else:
        if req.device_id and _normalize_device_id(req.device_id) != _normalize_device_id(_get_owner_device_id(username) or ""):
            raise HTTPException(status_code=403, detail="Users cannot modify permissions for another device")
        did = _get_owner_device_id(username)
        if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
            did = DEFAULT_DEVICE_ID
        if not did:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "No device assigned to this user",
                    "action": "configure_pc",
                    "hint": "Configure a device via /api/user/device/configure (auto-pick) or /api/user/device/set (explicit device_id).",
                },
            )

    perms = req.permissions or {}
    if not isinstance(perms, dict) or not perms:
        raise HTTPException(status_code=400, detail="permissions is required")

    allowed_keys = {
        "allow_app_control",
        "allow_execute_command",
        "allow_file_ops",
        "allow_screen",
        "allow_self_update",
    }
    normalized: Dict[str, bool] = {}
    for k, v in perms.items():
        if k not in allowed_keys:
            raise HTTPException(status_code=400, detail=f"Unsupported permission key: {k}")
        normalized[k] = bool(v)

    # Persist approvals so they apply automatically next time, even if the agent is offline.
    try:
        owner = _get_device_owner(did)
        _save_device_permissions(did, owner_username=owner, permissions=normalized, updated_by=username)
    except Exception:
        pass

    # If the agent is offline, we cannot apply runtime permissions now, but the saved permissions
    # will be auto-applied when the agent connects.
    if not await device_hub.is_connected(did):
        return {"status": "saved", "offline": True, "device_id": did, "permissions": normalized}

    job = await _dispatch_actions_to_device(
        did,
        username=username or "user",
        actions=[{"type": "agent_set_permissions", "permissions": normalized}],
        source_text="permission_grant",
    )
    return {"status": "queued", "job": job, "device_id": did, "permissions": normalized}


@app.get("/api/device/status")
async def device_status(session_id: str):
    _require_pc_agent_enabled()
    p = _require_authenticated_session(session_id)
    username = (p.get("username") or "").strip().lower()
    role = (p.get("role") or "user").strip().lower()
    agents_by_id = await device_hub.list_agents()
    if role == "admin":
        return {"status": "success", "agents": list(agents_by_id.values()), "default_device_id": DEFAULT_DEVICE_ID}

    did = _get_owner_device_id(username)
    if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
        did = DEFAULT_DEVICE_ID

    # Local/dev convenience: allow issuing a token for the default device even before
    # the user has explicitly configured a device binding.
    if not did and (not CLOUD_MODE) and LOCAL_DEFAULT_DEVICE_FALLBACK and DEFAULT_DEVICE_ID:
        did = DEFAULT_DEVICE_ID
    if not did:
        return {"status": "success", "agents": [], "default_device_id": DEFAULT_DEVICE_ID}
    agent = agents_by_id.get(did)
    return {"status": "success", "agents": ([agent] if agent else []), "default_device_id": did}


class AdminUserUpdateRequest(BaseModel):
    session_id: str
    username: str
    new_username: str | None = None
    role: str | None = None
    bootstrap_secret: str | None = None


@app.post("/api/admin/users/update")
async def admin_update_user(req: AdminUserUpdateRequest):
    """User management.

    Authorization:
    - Normal path: requires an admin session.
    - Bootstrap path: if JARVIS_ADMIN_BOOTSTRAP_SECRET is set and the request provides it,
      allow promoting an account to admin without an existing admin session.
      This is intended for first-time setup.
    """

    requested_role = (req.role or "").strip().lower() if req.role is not None else None
    bootstrap_ok = False
    if ADMIN_BOOTSTRAP_SECRET and req.bootstrap_secret and req.bootstrap_secret == ADMIN_BOOTSTRAP_SECRET:
        bootstrap_ok = True

    if not bootstrap_ok:
        _require_admin_session(req.session_id)

    target_username = (req.username or "").strip().lower()
    if not target_username:
        raise HTTPException(status_code=400, detail="username is required")

    new_username = (req.new_username or "").strip().lower() if req.new_username else None
    # If using bootstrap, only allow promotion to admin (no other edits).
    if bootstrap_ok:
        if requested_role != "admin":
            raise HTTPException(status_code=403, detail="Bootstrap can only be used to set role=admin")
        if req.new_username:
            raise HTTPException(status_code=403, detail="Bootstrap cannot rename users")

        # Optional: restrict bootstrap to the caller's own account.
        p = _get_principal(req.session_id)
        if p.get("username") and p.get("username") != (req.username or "").strip().lower():
            raise HTTPException(status_code=403, detail="Bootstrap can only promote the currently logged-in user")

    return voice_auth.update_user(target_username, new_username=new_username, new_role=requested_role)


class AdminBootstrapRequest(BaseModel):
    session_id: str
    bootstrap_secret: str


@app.post("/api/admin/bootstrap")
async def admin_bootstrap(req: AdminBootstrapRequest):
    """Promote the currently logged-in user to admin (first-time setup).

    Requires JARVIS_ADMIN_BOOTSTRAP_SECRET to be set on the server.
    """
    if not ADMIN_BOOTSTRAP_SECRET:
        raise HTTPException(status_code=400, detail="Bootstrap is not enabled on this server")
    if req.bootstrap_secret != ADMIN_BOOTSTRAP_SECRET:
        raise HTTPException(status_code=403, detail="Invalid bootstrap secret")
    p = _get_principal(req.session_id)
    if not p.get("username"):
        raise HTTPException(status_code=401, detail="Login required")
    return voice_auth.update_user(p["username"], new_role="admin")


class AdminLearningAddRequest(BaseModel):
    session_id: str
    prompt: str
    completion: str
    tags: List[str] | None = None


@app.post("/api/admin/learning/add")
async def admin_learning_add(req: AdminLearningAddRequest):
    _require_admin_session(req.session_id)
    database.save_learning_example(
        user_id=_get_principal(req.session_id).get("username") or "default",
        prompt=req.prompt,
        completion=req.completion,
        meta={"source": "admin_api"},
        tags=req.tags or [],
    )
    return {"status": "success"}


class LearningAddRequest(BaseModel):
    session_id: str
    prompt: str
    completion: str
    tags: List[str] | None = None


@app.post("/api/learning/add")
async def learning_add(req: LearningAddRequest):
    """Save a learning example for the current authenticated user.

    This is a RAG-lite memory store (not model weight fine-tuning).
    It is scoped to the caller's username.
    """
    p = _require_authenticated_session(req.session_id)
    database.save_learning_example(
        user_id=(p.get("username") or "default"),
        prompt=req.prompt,
        completion=req.completion,
        meta={"source": "user_api"},
        tags=req.tags or [],
    )
    return {"status": "success"}


class AdminLearningSearchRequest(BaseModel):
    session_id: str
    query: str
    limit: int | None = 5


@app.post("/api/admin/learning/search")
async def admin_learning_search(req: AdminLearningSearchRequest):
    _require_admin_session(req.session_id)
    username = _get_principal(req.session_id).get("username") or "default"
    limit = max(1, min(int(req.limit or 5), 20))
    results = database.search_learning_examples(req.query, user_id=username, limit=limit)
    # Return only safe fields
    safe = [
        {
            "_id": r.get("_id"),
            "timestamp": r.get("timestamp"),
            "prompt": r.get("prompt"),
            "completion": r.get("completion"),
            "tags": r.get("tags", []),
            "usage_count": r.get("usage_count", 0),
            "last_used": r.get("last_used"),
        }
        for r in (results or [])
    ]
    return {"status": "success", "results": safe}


@app.get("/api/admin/learning/stats")
async def admin_learning_stats(session_id: str):
    _require_admin_session(session_id)
    username = _get_principal(session_id).get("username") or "default"
    return {"status": "success", "user": username, "stats": database.get_learning_stats(user_id=username)}


class AdminLearningClearRequest(BaseModel):
    session_id: str


@app.post("/api/admin/learning/clear")
async def admin_learning_clear(req: AdminLearningClearRequest):
    _require_admin_session(req.session_id)
    username = _get_principal(req.session_id).get("username") or "default"
    return {"status": "success", "user": username, **database.delete_learning_examples(user_id=username)}


class AdminWebTrainingFetchRequest(BaseModel):
    session_id: str
    topics: List[str] | None = None
    num_results: int | None = 2


class AdminWikiTrainingFetchRequest(BaseModel):
    session_id: str
    topics: List[str] | None = None
    max_pages: int | None = 2
    lang: str | None = "en"


@app.post("/api/admin/web-training/fetch")
async def admin_web_training_fetch(req: AdminWebTrainingFetchRequest):
    """Fetch and store web training summaries now (admin-only)."""
    _require_admin_session(req.session_id)
    topics = req.topics or [
        "artificial intelligence trends",
        "Python programming tips",
        "web development best practices",
        "cloud computing news",
        "cybersecurity updates",
    ]
    num_results = max(1, min(int(req.num_results or 2), 5))

    try:
        from src.internet.internet import InternetAccess

        internet = InternetAccess()
        await internet.initialize()

        saved = 0
        for topic in topics[:20]:
            try:
                results = await internet.search_and_summarize(topic, num_results=num_results)
                for r in results:
                    try:
                        database.save_web_training_item(
                            topic=topic,
                            title=r.get("title"),
                            snippet=r.get("snippet"),
                            summary=(r.get("content_summary") or r.get("summary")),
                            url=r.get("url"),
                            source="admin_fetch",
                        )
                        saved += 1
                    except Exception:
                        pass
            except Exception:
                continue

        await internet.close()
        return {"status": "success", "saved": saved, "topics": topics}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/admin/wiki-training/fetch")
async def admin_wiki_training_fetch(req: AdminWikiTrainingFetchRequest):
    """Fetch and store compact Wikipedia summaries (admin-only).

    Notes:
    - Uses the official Wikipedia API.
    - Stores only short extracts + URLs via `save_web_training_item`.
    - Bounded by `max_pages` to avoid large crawls.
    """
    _require_admin_session(req.session_id)

    topics = req.topics or [
        "human psychology",
        "cognitive bias",
        "cognitive dissonance",
        "confirmation bias",
        "social psychology",
        "behavioral economics",
    ]
    max_pages = max(1, min(int(req.max_pages or 2), 5))
    lang = (req.lang or "en").strip().lower() or "en"

    try:
        # Ensure DB is available (best-effort)
        try:
            database._ensure_connected()
        except Exception:
            pass
        if getattr(database, "db", None) is None:
            return {"status": "error", "message": "Database unavailable"}

        from src.internet.wikipedia_client import wikipedia_topic_summaries

        saved = 0
        for topic in topics[:30]:
            t = (topic or "").strip()
            if not t:
                continue
            try:
                summaries = await wikipedia_topic_summaries(t, lang=lang, max_pages=max_pages)
                for s in summaries:
                    try:
                        # Use a short snippet; DB layer also truncates.
                        snippet = (s.description or "").strip()
                        if snippet and s.extract:
                            snippet = (snippet + ": " + s.extract).strip()
                        else:
                            snippet = s.extract

                        database.save_web_training_item(
                            topic=t,
                            title=s.title,
                            snippet=(snippet or "")[:500],
                            summary=(s.extract or ""),
                            url=s.url,
                            source="wikipedia_api",
                        )
                        saved += 1
                    except Exception:
                        pass
            except Exception:
                continue

        return {"status": "success", "saved": saved, "topics": topics, "max_pages": max_pages, "lang": lang}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# CORS Configuration
# =========================================================
cors_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://jarvis-frontend.onrender.com",
    "https://jarvis-cloud-assistant.onrender.com"
]

# Allow extra origins via env (comma-separated), e.g. for custom domains.
try:
    extra = env.get_str("JARVIS_CORS_ORIGINS", "")
    if extra:
        for o in [x.strip() for x in extra.split(",")]:
            if o and o not in cors_origins:
                cors_origins.append(o)
except Exception:
    pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# Core Initialization
# =========================================================
llm = LLMAdapter()
brain = JarvisBrain(llm=llm)
executor = ActionExecutor(brain=brain)


async def _shutdown_cleanup():
    """Best-effort cleanup for async HTTP sessions.

    Prevents aiohttp 'Unclosed client session/connector' warnings on shutdown/reload.
    """
    # Close LLMAdapter's aiohttp session
    try:
        llm = getattr(brain, "llm", None)
        if llm and hasattr(llm, "close"):
            await llm.close()
    except Exception:
        pass

    # Close global web scraper session (DuckDuckGo/HTTP client)
    try:
        from src.internet.web_scraper import close_scraper

        await close_scraper()
    except Exception:
        pass


def _build_web_context_from_action_results(action_results: list[dict], max_chars: int = 380) -> str:
    """Build a compact context string (for the LLM) from web_search/fetch_url results.

    Keep it short because the LLM adapter currently truncates context.
    """
    blocks: list[str] = []

    for r in action_results or []:
        if not isinstance(r, dict):
            continue
        status = (r.get("status") or "").lower()
        action = (r.get("action") or r.get("action_type") or "").lower()
        if status != "success":
            continue

        if action in {"web_search", "search"}:
            query = (r.get("query") or "").strip()
            results = r.get("results") or []
            lines: list[str] = []
            if query:
                lines.append(f"Web results for: {query}")
            for idx, item in enumerate(results[:3], start=1):
                if not isinstance(item, dict):
                    continue
                title = (item.get("title") or "").strip()
                url = (item.get("url") or "").strip()
                snippet = (item.get("snippet") or "").strip()
                parts = []
                if title:
                    parts.append(title)
                if snippet:
                    parts.append(snippet)
                if url:
                    parts.append(url)
                if parts:
                    lines.append(f"{idx}) " + " | ".join(parts))
            if len(lines) > 1:
                blocks.append("\n".join(lines))
            continue

        if action == "fetch_url":
            url = (r.get("url") or "").strip()
            title = (r.get("title") or "").strip()
            summary = (r.get("summary") or "").strip()
            line = "Fetched page:"
            if title:
                line += f" {title}."
            if summary:
                line += f" Summary: {summary}"
            if url:
                line += f" Source: {url}"
            if line != "Fetched page:" and (title or summary or url):
                blocks.append(line)
            continue

    ctx = "\n\n".join([b for b in blocks if b]).strip()
    if not ctx:
        return ""
    # Keep the head so we retain the top-ranked URLs/titles.
    ctx = ctx[:max_chars]
    return ctx


def _web_lookup_found(action_results: list[dict]) -> bool:
    """Return True if web tool results include any usable hits."""
    for r in action_results or []:
        if not isinstance(r, dict):
            continue
        if (r.get("status") or "").lower() != "success":
            continue
        action = (r.get("action") or r.get("action_type") or "").lower()
        if action in {"web_search", "search"}:
            results = r.get("results") or []
            if isinstance(results, list) and len(results) > 0:
                return True
            try:
                if int(r.get("results_count") or 0) > 0:
                    return True
            except Exception:
                pass
        if action == "fetch_url":
            if (r.get("title") or "") or (r.get("summary") or ""):
                return True
    return False


def _fallback_answer_from_web_results(user_text: str, tool_results: list[dict], *, found: bool) -> str:
    """Non-LLM fallback text when web lookup succeeded but LLM continuation fails.

    Keeps the same UX contract: explicit found/not-found prefix and 1-2 source URLs when found.
    """
    try:
        from src.core.offline_analysis import synthesize_from_web

        return synthesize_from_web(user_text, tool_results, found=found)
    except Exception:
        # Extremely defensive: preserve the contract even if the offline synthesizer fails.
        if not found:
            return (
                "Not found this: No usable web results were returned. "
                "What exactly should I look for (asset/topic + timeframe or specific question)?"
            )
        return "I found this: Web results were returned, but synthesis failed.\n\nSource URLs:\n1. (no source URL available)"


async def _answer_user_using_web_context(user_text: str, web_context: str, mode: str = "chat") -> str | None:
    """Ask the LLM for a final answer using provided web context.

    Returns assistant text or None on failure.
    """
    try:
        if not web_context:
            return None
        # Ask for an answer (not a list of results) and forbid more web actions.
        # Be explicit: the answer must be specific to THIS question.
        prompt = (
            "You are answering the user's specific question using the provided web context.\n"
            "Rules:\n"
            "- Answer the question directly and specifically.\n"
            "- Do NOT output a list of search results or raw snippets.\n"
            "- If the context is insufficient or unclear, say what is missing instead of guessing.\n"
            "- Return JSON with actions: [] (no further actions).\n\n"
            f"User question: {user_text.strip()}"
        )
        out = await brain.llm.generate_response(prompt, context=web_context, mode=mode)
        if isinstance(out, dict):
            txt = (out.get("text") or "").strip()
            return txt or None
        return None
    except Exception:
        return None


def _persist_web_context_items(topic: str, action_results: list[dict]):
    """Best-effort: store short web snippets/summaries to MongoDB.

    We store only short snippets/summaries + URLs (no full-page mirroring).
    """
    try:
        database._ensure_connected()
        if database.db is None:
            return
    except Exception:
        return

    try:
        for r in action_results or []:
            if not isinstance(r, dict):
                continue
            if (r.get("status") or "").lower() != "success":
                continue

            action = (r.get("action") or r.get("action_type") or "").lower()
            if action in {"web_search", "search"}:
                query = (r.get("query") or topic or "").strip()
                results = r.get("results") or []
                for item in results[:3]:
                    if not isinstance(item, dict):
                        continue
                    title = (item.get("title") or "").strip()
                    url = (item.get("url") or "").strip()
                    snippet = (item.get("snippet") or "").strip()
                    if url:
                        database.save_web_training_item(topic=query, title=title, snippet=snippet, summary=None, url=url, source="web_search")
                continue

            if action == "fetch_url":
                url = (r.get("url") or "").strip()
                title = (r.get("title") or "").strip()
                summary = (r.get("summary") or "").strip()
                if url:
                    database.save_web_training_item(topic=(topic or "").strip(), title=title, snippet=None, summary=summary, url=url, source="fetch_url")
                continue
    except Exception:
        # never break chat flow on DB issues
        return


async def _continue_user_using_web_context(user_text: str, web_context: str, mode: str = "chat", *, found: bool) -> dict | None:
    """Ask the LLM to continue using provided web context and MAY return actions.

    Critical behavior:
    - It must NOT request another web_search/fetch_url loop (we already have web context).
    - It may propose PC/app actions based on what it learned.
    """
    try:
        import re
        if not web_context:
            return None

        status_line = "I found this:" if found else "Not found this:"
        prompt = (
            "You are completing the user's request using the provided web context.\n"
            "Rules:\n"
            "- Use the web context to learn app features/steps if needed.\n"
            "- Do NOT output web_search, fetch_url, or search actions again.\n"
            "- You MAY output normal actions (open_app, switch_app, open_url, execute_command, type_text, hotkey, press_key, etc.) if they help complete the task.\n"
            "- Your reply MUST start with an explicit lookup status line so the user understands the result.\n"
            f"  - If info exists in context: start with '{status_line} <short answer>'\n"
            f"  - If info does not exist in context: start with '{status_line} <what was missing>'\n"
            "- If you say 'I found this:', include a very short extracted summary (2-6 lines) and include 1-2 Source URLs from the context.\n"
            "- If you say 'Not found this:', ask exactly ONE clarifying question to learn what the user actually needs next, and return actions: [].\n"
            "- If the context is insufficient, ask at most 1 clarifying question and return actions: [].\n\n"
            f"User request: {user_text.strip()}"
        )

        out = await brain.llm.generate_response(prompt, context=web_context, mode=mode)
        if not isinstance(out, dict):
            return None

        # If the LLM adapter returned a fallback (e.g., rate limit), let the caller use
        # a deterministic fallback based on tool results.
        if (out.get("source") or "").startswith("fallback"):
            return None

        # Enforce the explicit UX contract even if the model is slightly off.
        txt = (out.get("text") or "").strip()
        status_prefix = "I found this:" if found else "Not found this:"
        if not txt.lower().startswith(status_prefix.lower()):
            txt = f"{status_prefix} {txt}".strip()

        if found:
            # Require at least one URL; if missing, pull 1-2 URLs from web_context.
            has_url = bool(re.search(r"https?://\S+", txt))
            if not has_url:
                urls = re.findall(r"https?://\S+", web_context or "")
                urls = [u.rstrip(").,;") for u in urls]
                urls = [u for i, u in enumerate(urls) if u and u not in urls[:i]]
                if urls:
                    txt = (txt + "\n\nSource URLs:\n" + "\n".join([f"{i+1}. {u}" for i, u in enumerate(urls[:2])])).strip()

            # If the user asked an informational question (not to open anything), avoid emitting side-effect actions.
            ut = (user_text or "").strip().lower()
            if ut and ("open " not in ut) and ("launch " not in ut) and ("go to " not in ut) and ("visit " not in ut):
                if ut.endswith("?") or re.search(r"\b(what is|what are|latest|current|as of)\b", ut):
                    out["actions"] = []
        else:
            # Must ask exactly ONE clarifying question and must not emit actions.
            out["actions"] = []
            qcount = txt.count("?")
            if qcount == 0:
                txt = (txt + " What exactly should I look for (product name/version or the exact feature)?").strip()
            elif qcount > 1:
                # Keep only the first question.
                first_q = txt.find("?")
                txt = (txt[: first_q + 1]).strip()

        out["text"] = txt

        # Hard filter: prevent re-triggering web loop.
        actions = out.get("actions") or []
        if not isinstance(actions, list):
            actions = []
        blocked = []
        kept = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            t = (a.get("type") or "").strip().lower()
            if t in {"web_search", "fetch_url", "search"}:
                blocked.append(a)
                continue
            kept.append(a)
        out["actions"] = kept
        if blocked and not kept:
            # Keep the assistant honest and avoid silent failures.
            out["text"] = ((out.get("text") or "").strip() + "\n\n(I used the web context, but won't run another web search loop here.)").strip()
        return out
    except Exception:
        return None


# =========================================================
# Chat Orchestrator (keeps routes thin)
# =========================================================
chat_orchestrator = ChatOrchestrator(
    brain=brain,
    executor=executor,
    cloud_mode=CLOUD_MODE,
    admin_only_action_types=ADMIN_ONLY_ACTION_TYPES,
    user_explicitly_requested_screen_capture=_user_explicitly_requested_screen_capture,
    can_control_device=_can_control_device,
    is_remote_device_action=_is_remote_device_action,
    build_web_context_from_action_results=_build_web_context_from_action_results,
    persist_web_context_items=_persist_web_context_items,
    web_lookup_found=_web_lookup_found,
    continue_user_using_web_context=_continue_user_using_web_context,
    fallback_answer_from_web_results=(
        lambda user_text, tool_results, found: _fallback_answer_from_web_results(
            user_text, tool_results, found=found
        )
    ),
    user_explicitly_requested_research_open=_user_explicitly_requested_research_open,
    pick_best_source_url=_pick_best_source_url,
    notify=notification_hub.publish,
)


# =========================================================
# Realtime Notifications (WebSocket)
# =========================================================
@app.websocket("/ws/notifications")
async def notifications_ws(ws: WebSocket):
    """Push server events (e.g., research completion) to the authenticated user.

    Client should connect with `?session_id=...` (voice-auth session).
    """
    await ws.accept()
    try:
        session_id = (ws.query_params.get("session_id") or "").strip()
        if not session_id:
            await ws.send_json({"type": "error", "error": "missing_session_id"})
            await ws.close(code=1008)
            return

        username = _require_voice_session(session_id)
        if not username:
            await ws.send_json({"type": "error", "error": "auth_failed"})
            await ws.close(code=1008)
            return

        q = await notification_hub.register(username)
        await ws.send_json({"type": "ack", "user": username})

        try:
            while True:
                # Wait for a server event; send periodic keepalives.
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30)
                    await ws.send_json(payload)
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "ping"})
        finally:
            await notification_hub.unregister(username, q)

    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "error": "server_error", "message": str(e)})
        except Exception:
            pass
        try:
            await ws.close(code=1011)
        except Exception:
            pass

class MessageIn(BaseModel):
    user: str | None = "user"
    text: str
    mode: str | None = "chat"
    session_id: str | None = None  # Voice auth session
    device_id: str | None = None  # optional: admin-only override for cloud->agent dispatch

class VoiceAuthRequest(BaseModel):
    username: str
    voice_sample_hash: str | None = None
    voice_sample_text: str | None = None
    audio_b64: str | None = None
    sample_rate_hz: int | None = None
    password: str | None = None
    action: str  # "register" or "login"
    role: str | None = None  # optional: 'admin' or 'user'


class VoiceBiometricsRequest(BaseModel):
    session_id: str
    audio_b64: str
    sample_rate_hz: int = 16000


class SecureVoiceTranscribeRequest(BaseModel):
    session_id: str
    audio_b64: str
    sample_rate_hz: int = 16000
    language: str | None = None

# =========================================================
# Voice Authentication Endpoints
# =========================================================
@app.post("/api/voice-auth")
async def voice_auth_endpoint(auth_req: VoiceAuthRequest):
    """Handle voice-based authentication"""
    try:
        from src.utils.voice_biometrics import (
            VOICE_BIOMETRICS_ENABLED,
            VOICE_BIOMETRICS_THRESHOLD,
            _decode_pcm16_b64,
            compute_embedding_from_pcm16,
            should_accept,
            to_jsonable_embedding,
        )

        if auth_req.action == "register":
            if not auth_req.voice_sample_hash:
                return {"status": "error", "message": "Voice sample required for registration"}

            # Prevent privilege escalation on hosted deployments.
            uname = (auth_req.username or "").strip().lower()
            requested_role = (auth_req.role or "user").strip().lower()

            # Cloud mode: always force role=user.
            # Local mode: only allow role=admin when registering the configured admin username.
            if CLOUD_MODE:
                role = "user"
            elif requested_role == "admin" and uname == ADMIN_USERNAME:
                role = "admin"
            else:
                role = "user"

            result = voice_auth.register_user(
                uname,
                auth_req.voice_sample_hash,
                auth_req.password,
                role=role,
                voice_sample_text=auth_req.voice_sample_text,
            )

            # Optional: enroll voice biometrics from provided PCM sample.
            if VOICE_BIOMETRICS_ENABLED and auth_req.audio_b64:
                try:
                    audio_bytes = _decode_pcm16_b64(auth_req.audio_b64)
                    emb = compute_embedding_from_pcm16(audio_bytes, int(auth_req.sample_rate_hz or 16000))
                    if emb is not None:
                        voice_auth.add_voice_biometrics_embedding(uname, to_jsonable_embedding(emb))
                        result["biometrics"] = {"enrolled": True}
                    else:
                        result["biometrics"] = {"enrolled": False, "message": "Could not extract a stable voice embedding"}
                except Exception as e:
                    result["biometrics"] = {"enrolled": False, "message": f"Biometrics enrollment failed: {e}"}
            # On successful registration, also create a session for UX.
            if result.get("status") in ("success", "queued"):
                if auth_tokens.secret:
                    try:
                        result["session_id"] = auth_tokens.issue(username=uname, role=role)
                    except Exception:
                        # fall back below
                        pass

                if not result.get("session_id"):
                    # Local/dev fallback: create legacy session via voice_auth.
                    ok, sid_or_err = voice_auth.authenticate_by_voice(
                        uname,
                        auth_req.voice_sample_hash,
                        auth_req.password,
                        voice_sample_text=auth_req.voice_sample_text,
                    )
                    if ok:
                        result["session_id"] = sid_or_err

            # Attach role/permissions/user payload (frontend expects it)
            try:
                u = voice_auth.get_user(uname) or {}
                effective_role = (u.get("role") or role or "user").strip().lower()
                principal = {"username": uname, "role": effective_role, "auth_type": "voice"}
                result["username"] = uname
                result["role"] = effective_role
                result["user"] = _public_user_profile(uname)
                result["permissions"] = _permissions_for(principal)
            except Exception:
                pass
            return result
        
        elif auth_req.action == "login":
            if not auth_req.voice_sample_hash:
                return {"status": "error", "message": "Voice sample required for login"}

            is_valid, session_or_error = voice_auth.authenticate_by_voice(
                auth_req.username,
                auth_req.voice_sample_hash,
                auth_req.password,
                voice_sample_text=auth_req.voice_sample_text,
            )
            if is_valid:
                uname = auth_req.username.strip().lower()

                # If biometrics is enabled and the account has stored embeddings, require a match.
                if VOICE_BIOMETRICS_ENABLED:
                    stored_vecs = voice_auth.get_voice_biometrics_vectors(uname)
                    if stored_vecs:
                        if not auth_req.audio_b64:
                            return {
                                "status": "error",
                                "message": "Voice biometrics is enabled. Please allow mic access and try again.",
                                "code": "biometrics_audio_required",
                            }
                        try:
                            audio_bytes = _decode_pcm16_b64(auth_req.audio_b64)
                            emb = compute_embedding_from_pcm16(audio_bytes, int(auth_req.sample_rate_hz or 16000))
                            if emb is None:
                                return {
                                    "status": "error",
                                    "message": "Could not extract voice biometrics from this sample. Try again.",
                                    "code": "biometrics_extract_failed",
                                }
                            ok, score = should_accept(emb, stored_vecs, threshold=VOICE_BIOMETRICS_THRESHOLD)
                            if not ok:
                                return {
                                    "status": "error",
                                    "message": "Voice biometrics did not match this account.",
                                    "code": "biometrics_mismatch",
                                    "score": score,
                                }
                        except Exception:
                            return {
                                "status": "error",
                                "message": "Voice biometrics verification failed.",
                                "code": "biometrics_verify_failed",
                            }

                u = voice_auth.get_user(uname) or {}
                role = u.get("role", "user")
                session_id = None
                if auth_tokens.secret:
                    try:
                        session_id = auth_tokens.issue(username=uname, role=role)
                    except Exception:
                        session_id = None
                if not session_id:
                    session_id = session_or_error
                return {
                    "status": "success",
                    "message": "Authentication successful",
                    "session_id": session_id,
                    "username": uname,
                    "role": role,
                    "user": _public_user_profile(uname),
                    "permissions": _permissions_for({"username": uname, "role": role, "auth_type": "voice"}),
                }
            return {
                "status": "error",
                "message": session_or_error or "Authentication failed"
            }
        
        return {"status": "error", "message": "Invalid action"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/voice-biometrics/enroll")
async def voice_biometrics_enroll(req: VoiceBiometricsRequest):
    """Enroll an embedding for the authenticated user."""
    principal = _require_authenticated_session(req.session_id)
    username = (principal.get("username") or "").strip().lower()

    from src.utils.voice_biometrics import (
        VOICE_BIOMETRICS_ENABLED,
        _decode_pcm16_b64,
        compute_embedding_from_pcm16,
        to_jsonable_embedding,
    )
    if not VOICE_BIOMETRICS_ENABLED:
        raise HTTPException(status_code=501, detail="VOICE_BIOMETRICS_ENABLED is false")

    if not req.audio_b64 or len(req.audio_b64) > 6_000_000:
        raise HTTPException(status_code=413, detail="Audio payload too large")

    audio_bytes = _decode_pcm16_b64(req.audio_b64)
    emb = compute_embedding_from_pcm16(audio_bytes, int(req.sample_rate_hz or 16000))
    if emb is None:
        raise HTTPException(status_code=400, detail="Could not extract a stable voice embedding")

    out = voice_auth.add_voice_biometrics_embedding(username, to_jsonable_embedding(emb))
    return {"status": "success", "username": username, **out}


@app.post("/api/voice/secure-transcribe")
async def voice_secure_transcribe(req: SecureVoiceTranscribeRequest):
    """Verify speaker identity and then transcribe the audio.

    This is used by the frontend when voice biometrics is enabled, so voice
    commands are only accepted from the logged-in user's voice.
    """
    principal = _require_authenticated_session(req.session_id)
    username = (principal.get("username") or "").strip().lower()

    from src.utils.voice_biometrics import (
        VOICE_BIOMETRICS_ENABLED,
        VOICE_BIOMETRICS_THRESHOLD,
        _decode_pcm16_b64,
        compute_embedding_from_pcm16,
        should_accept,
    )

    if not VOICE_BIOMETRICS_ENABLED:
        raise HTTPException(status_code=501, detail="VOICE_BIOMETRICS_ENABLED is false")

    if not req.audio_b64 or len(req.audio_b64) > 6_000_000:
        raise HTTPException(status_code=413, detail="Audio payload too large")

    audio_bytes = _decode_pcm16_b64(req.audio_b64)

    stored_vecs = voice_auth.get_voice_biometrics_vectors(username)
    if not stored_vecs:
        raise HTTPException(status_code=409, detail="No voice biometrics enrolled for this account")
    emb = compute_embedding_from_pcm16(audio_bytes, int(req.sample_rate_hz or 16000))
    if emb is None:
        raise HTTPException(status_code=400, detail="Could not extract voice biometrics")
    ok, score = should_accept(emb, stored_vecs, threshold=VOICE_BIOMETRICS_THRESHOLD)
    if not ok:
        raise HTTPException(status_code=403, detail={"message": "Voice biometrics mismatch", "score": score})

    # Transcribe using the existing Google STT helper.
    # (We keep this intentionally strict so the command path doesn't silently fall back.)
    speech, client = _get_google_speech_client_and_creds()
    if req.sample_rate_hz < 8000 or req.sample_rate_hz > 48000:
        raise HTTPException(status_code=400, detail="sample_rate_hz must be between 8000 and 48000")

    language_code = (req.language or GOOGLE_SPEECH_LANGUAGE_DEFAULT or "en-US").strip() or "en-US"
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=int(req.sample_rate_hz),
        language_code=language_code,
        enable_automatic_punctuation=True,
        model="latest_short",
    )
    audio = speech.RecognitionAudio(content=audio_bytes)
    try:
        response = client.recognize(config=config, audio=audio)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Google STT failed: {e}")

    text = ""
    try:
        for result in response.results:
            if result.alternatives:
                text += (result.alternatives[0].transcript or "")
    except Exception:
        text = ""

    return {
        "status": "success",
        "text": (text or "").strip(),
        "language": language_code,
        "sample_rate_hz": int(req.sample_rate_hz),
    }


# Simple health endpoint used by local startup checks
@app.get("/health")
async def health_check(check_db: int = 0):
    """Health endpoint for monitoring.

    - Always returns 200 when the API process is alive.
    - `check_db=1` performs a best-effort DB ping with a short timeout.
    """
    db_uri = env.get("MONGODB_URI") or env.get("MONGO_URI")
    db_configured = bool(db_uri)
    # PyMongo Database objects do not support truthiness checks.
    db_connected = (getattr(database, "client", None) is not None) and (getattr(database, "db", None) is not None)
    db_ping_ok = None
    db_ping_error = None

    if check_db and db_configured:
        try:
            # Do not call database._connect() here because it can create indexes (slow).
            from pymongo import MongoClient

            probe_uri = getattr(database, "uri", None) or db_uri
            client = MongoClient(
                probe_uri,
                serverSelectionTimeoutMS=1000,
                connectTimeoutMS=1000,
                socketTimeoutMS=1000,
            )
            client.admin.command("ping")
            db_ping_ok = True
            try:
                client.close()
            except Exception:
                pass
        except Exception as e:
            db_ping_ok = False
            db_ping_error = str(e)[:200]

    payload = {
        "status": "ok",
        "time_utc": datetime.utcnow().isoformat() + "Z",
        "uptime_s": int(time.time() - START_TS),
        "cloud_mode": bool(CLOUD_MODE),
        "db": {
            "configured": db_configured,
            "connected": db_connected,
            "ping_ok": db_ping_ok,
            "ping_error": db_ping_error,
        },
        "scheduler": {
            "enabled": bool(ENABLE_SCHEDULER),
            "available": bool(SCHEDULER_AVAILABLE),
        },
    }

    return JSONResponse(payload, status_code=200)


@app.post("/api/validate-session")
async def validate_session_endpoint(session_id: dict):
    """Validate authentication session"""
    session = session_id.get("session_id") if isinstance(session_id, dict) else session_id
    try:
        p = _get_principal(session)
        return {
            "valid": True,
            "username": p.get("username"),
            "role": p.get("role"),
            "user": _public_user_profile(p.get("username")),
            "permissions": _permissions_for(p),
        }
    except Exception:
        return {"valid": False, "username": None, "role": None, "user": None, "permissions": None}

@app.post("/api/logout")
async def logout(session_id: str | dict):
    """Logout and invalidate session.

    Also requests the connected PC agent (if any) to stop, so logout doesn't depend on
    browser protocol handlers.
    """
    sid = session_id
    if isinstance(session_id, dict):
        sid = session_id.get("session_id")

    if not isinstance(sid, str) or not sid.strip():
        raise HTTPException(status_code=400, detail="session_id required")
    sid = sid.strip()

    # Best-effort: stop the agent for this user/device (if connected).
    try:
        p = _get_principal(sid)
        username = (p.get("username") or "").strip().lower()
        did = _get_owner_device_id(username)
        if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
            did = DEFAULT_DEVICE_ID
        if did and await device_hub.is_connected(did):
            await _dispatch_actions_to_device(
                did,
                username=username or "user",
                actions=[{"type": "agent_stop"}],
                source_text="logout",
            )
    except Exception:
        pass

    if auth_tokens.secret:
        success = auth_tokens.revoke(sid)
    else:
        success = voice_auth.logout(sid)
    return {"status": "success" if success else "error"}

# =========================================================
# Telegram Bot Endpoints
# =========================================================
class TelegramAuthRequest(BaseModel):
    user_id: str
    username: str
    action: str  # "register" or "login"
    voice_sample_hash: str | None = None
    password: str | None = None
    role: str | None = None

@app.post("/api/telegram/register-start")
async def telegram_register_start(req: dict):
    """Start Telegram registration process"""
    user_id = req.get("user_id")
    username = req.get("username")
    
    if not user_id or not username:
        return {"status": "error", "message": "user_id and username required"}
    
    result = telegram_bot.start_registration(user_id, username)
    return result

@app.post("/api/telegram/process-voice")
async def telegram_process_voice(req: dict):
    """Process voice sample from Telegram"""
    user_id = req.get("user_id")
    voice_file_id = req.get("voice_file_id")
    # In production, download voice file from Telegram and get bytes
    voice_bytes = req.get("voice_bytes", b"")
    
    if not user_id or not voice_file_id:
        return {"status": "error", "message": "user_id and voice_file_id required"}
    
    result = telegram_bot.process_voice_sample(user_id, voice_file_id, voice_bytes)
    return result

@app.post("/api/telegram/complete-registration")
async def telegram_complete_registration(auth_req: TelegramAuthRequest):
    """Complete Telegram registration"""
    if not auth_req.voice_sample_hash or not auth_req.password:
        return {"status": "error", "message": "voice_sample_hash and password required"}
    
    result = telegram_bot.complete_registration(
        auth_req.user_id,
        auth_req.voice_sample_hash,
        auth_req.password,
        auth_req.username,
        auth_req.role or "user"
    )
    return result

@app.post("/api/telegram/login")
async def telegram_login(auth_req: TelegramAuthRequest):
    """Handle Telegram user login"""
    if not auth_req.voice_sample_hash:
        return {"status": "error", "message": "voice_sample_hash required"}
    
    result = telegram_bot.telegram_login(
        auth_req.user_id,
        auth_req.username,
        auth_req.voice_sample_hash
    )
    return result

@app.post("/api/telegram/validate-session")
async def telegram_validate_session(req: dict):
    """Validate Telegram user session"""
    user_id = req.get("user_id")
    if not user_id:
        return {"status": "error", "message": "user_id required"}
    
    is_valid, username = telegram_bot.validate_telegram_session(user_id)
    return {
        "valid": is_valid,
        "username": username,
        "user_info": telegram_bot.get_user_info(user_id)
    }

@app.post("/api/telegram/logout")
async def telegram_logout(req: dict):
    """Logout Telegram user"""
    user_id = req.get("user_id")
    if not user_id:
        return {"status": "error", "message": "user_id required"}
    
    success = telegram_bot.logout_telegram_user(user_id)
    return {
        "status": "success" if success else "error",
        "message": "Logged out successfully" if success else "User not found"
    }

@app.post("/api/telegram/chat")
async def telegram_chat(req: dict, background_tasks: BackgroundTasks):
    """Handle chat message from Telegram user"""
    if VOICE_ONLY_MODE:
        return {
            "status": "error",
            "message": "Voice-only mode is enabled on this assistant.",
        }
    user_id = req.get("user_id")
    text = req.get("text")
    
    if not user_id or not text:
        return {"status": "error", "message": "user_id and text required"}
    
    # Validate session
    is_valid, username = telegram_bot.validate_telegram_session(user_id)
    if not is_valid:
        return {
            "status": "auth_required",
            "message": "Please login first",
            "action": "redirect_to_login"
        }
    
    # Process message through brain
    response = await brain.handle_message(text, mode="chat")
    actions = response.get("actions", [])

    # Avoid accidental screen capture which can degrade UX.
    if actions and not _user_explicitly_requested_screen_capture(text):
        actions = [
            a
            for a in actions
            if (a or {}).get("type") not in ("capture_screen",)
        ]
        response["actions"] = actions

    # Enforce role-based permissions (Telegram sessions store role in user_info)
    role = ((telegram_bot.get_user_info(user_id) or {}).get("role") or "user").strip().lower()
    if role not in ("user", "admin"):
        role = "user"

    if actions:
        if role != "admin":
            blocked = [a for a in actions if (a or {}).get("type") in ADMIN_ONLY_ACTION_TYPES]
            actions = [a for a in actions if (a or {}).get("type") not in ADMIN_ONLY_ACTION_TYPES]
            response["actions"] = actions
            if blocked:
                response["text"] = (response.get("text") or "") + "\n\n(Some actions require admin privileges and were skipped.)"

        # Execute web lookups inline so Telegram responses include results.
        immediate_types = {"web_search", "fetch_url", "search"}
        immediate_actions = [a for a in actions if (a or {}).get("type") in immediate_types]
        deferred_actions = [a for a in actions if (a or {}).get("type") not in immediate_types]
        if immediate_actions:
            continued_actions = None
            try:
                tool_results = await executor.process_actions(immediate_actions, (username or "user"))
                if env.get_bool("JARVIS_RETURN_ACTION_RESULTS", False):
                    response["action_results"] = tool_results
                mode = (response.get("mode") or "chat")
                web_ctx = _build_web_context_from_action_results(tool_results)
                _persist_web_context_items(topic=text, action_results=tool_results)
                found = _web_lookup_found(tool_results)
                if env.get_str("JARVIS_WEB_RESULTS_MODE", "answer").lower() in ("append", "both"):
                    # Legacy/debug mode: append raw results.
                    response["text"] = (response.get("text") or "")
                else:
                    continued = await _continue_user_using_web_context(text, web_ctx, mode=mode, found=found)
                    if continued:
                        response["text"] = (continued.get("text") or response.get("text") or "")
                        # Allow dynamic actions after web lookup
                        continued_actions = continued.get("actions") or []
            except Exception as e:
                response["text"] = (response.get("text") or "") + f"\n\n(Web lookup failed: {e})"
            actions = deferred_actions
            if isinstance(continued_actions, list) and continued_actions:
                actions = continued_actions
            response["actions"] = actions

        if actions:
            background_tasks.add_task(executor.process_actions, actions, username)
    
    return {
        "status": "success",
        "user_id": user_id,
        "username": username,
        "response": response.get("text", ""),
        "actions": actions
    }

# =========================================================
# Main Chat Endpoint (With Auth Check)
# =========================================================
@app.post("/api/chat")
async def chat_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    # Cloud mode must require auth for chat to prevent public abuse/cost.
    # Voice-only mode below already enforces this, but keep it explicit when chat mode is enabled.
    if CLOUD_MODE and not msg.session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please login first.",
        )
    if VOICE_ONLY_MODE:
        incoming_mode = (msg.mode or "").strip().lower()
        if incoming_mode != "voice":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Voice-only mode is enabled on this assistant.",
            )
        if not msg.session_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Voice authentication is required in voice-only mode.",
            )
    request_id = f"chat_{uuid.uuid4().hex[:12]}"
    principal = _get_principal(msg.session_id) if msg.session_id else {"username": None, "role": "anonymous"}
    if CLOUD_MODE:
        # Upgrade anonymous principal to a required authenticated principal.
        principal = _require_authenticated_session(msg.session_id)
    username = None
    role = principal.get("role", "anonymous")
    if msg.session_id:
        username = _require_voice_session(msg.session_id)
    if username:
        msg.user = username
    
    # Bind learning/training memory to the authenticated principal when available.
    response, actions = await chat_orchestrator.run_chat(
        text=msg.text,
        mode=(msg.mode or "chat"),
        principal=principal,
        role=role,
        acting_user=(msg.user or username or "user"),
        background_tasks=background_tasks,
        user_id=((username or msg.user) if (username or msg.user) else None),
    )

    # Persist voice command telemetry (MongoDB)
    try:
        if (msg.mode or "").strip().lower() == "voice" and (username or msg.user):
            database._ensure_connected()
            if database.db is not None:
                database.save_voice_command(
                    command_text=msg.text,
                    command_type="voice",
                    status="received",
                    result={
                        "user": (username or msg.user),
                        "action_count": len(actions) if isinstance(actions, list) else 0,
                        "mode": msg.mode,
                    },
                )
    except Exception:
        # never break chat flow on telemetry
        pass

    # In cloud mode, forward any PC/device actions to the connected local agent.
    if CLOUD_MODE:
        # In cloud mode we do NOT execute device actions from here.
        # The frontend dispatches device actions via /api/device/dispatch so it can show
        # permission/start-agent UX. However, we *do* execute safe server-side actions
        # (e.g., task creation, email drafting) so they aren't silently ignored by the UI.

        safe_server_action_types = {"create_task", "stop_task", "generate_email", "n8n_webhook"}
        actions = actions or []
        server_actions = [a for a in actions if isinstance(a, dict) and (a.get("type") in safe_server_action_types)]
        remaining_actions = [a for a in actions if not (isinstance(a, dict) and (a.get("type") in safe_server_action_types))]

        if server_actions:
            try:
                server_results = await executor.process_actions(server_actions, (username or "user"))
                # Surface created task_id(s) for better cancellation UX.
                try:
                    task_ids = []
                    for r in server_results or []:
                        if isinstance(r, dict) and r.get("task_id"):
                            task_ids.append(str(r.get("task_id")))
                    if task_ids:
                        response["task_id"] = task_ids[-1]
                        response["task_ids"] = task_ids
                except Exception:
                    pass

                if env.get_bool("JARVIS_RETURN_ACTION_RESULTS", False):
                    response["action_results"] = server_results
            except Exception as e:
                response["text"] = (response.get("text") or "") + f"\n\n(Server action execution failed: {e})"

        # Strip server-only maintenance actions that the frontend can't execute.
        # (These are admin-only in local mode and should never be emitted in cloud mode.)
        server_only_blocked = {"check_errors", "fix_errors", "check_render_logs"}
        blocked = [a for a in remaining_actions if isinstance(a, dict) and (a.get("type") in server_only_blocked)]
        if blocked:
            response["text"] = (response.get("text") or "") + "\n\n(Some maintenance actions are disabled in cloud mode.)"
            remaining_actions = [a for a in remaining_actions if not (isinstance(a, dict) and (a.get("type") in server_only_blocked))]

        response["actions"] = remaining_actions
        response["request_id"] = request_id
        return response

    # Local mode
    try:
        response["request_id"] = request_id
    except Exception:
        pass
    return response

# Alias for backward compatibility
@app.post("/api/message")
async def message_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    """Alias for /api/chat endpoint for backward compatibility"""
    return await chat_endpoint(msg, background_tasks)

# =========================================================
# Internet Access API (Web Search & Data Retrieval)
# =========================================================
class SearchRequest(BaseModel):
    query: str
    num_results: int | None = 5
    session_id: str | None = None

@app.post("/api/internet/search")
async def search_web(req: SearchRequest):
    """Search the web for information"""
    _require_voice_session(req.session_id)
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        results = await internet.search(req.query, num_results=req.num_results or 5)
        
        await internet.close()
        
        return {
            "status": "success",
            "query": req.query,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

class FetchRequest(BaseModel):
    url: str
    include_content: bool | None = True
    session_id: str | None = None

@app.post("/api/internet/fetch")
async def fetch_webpage(req: FetchRequest):
    """Fetch and parse a webpage"""
    _require_voice_session(req.session_id)
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        result = await internet.fetch_webpage(req.url, include_content=req.include_content or True)
        
        await internet.close()
        
        return {
            "status": "success",
            "url": req.url,
            "content": result
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

@app.post("/api/internet/search-summarize")
async def search_and_summarize(req: SearchRequest):
    """Search web and get summaries of top results"""
    _require_voice_session(req.session_id)
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        results = await internet.search_and_summarize(req.query, num_results=req.num_results or 3)
        
        await internet.close()
        
        return {
            "status": "success",
            "query": req.query,
            "results": results,
            "count": len(results)
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

@app.get("/api/internet/news")
async def get_news_endpoint(topic: str = "latest", num_results: int = 5, session_id: str | None = None):
    """Get latest news on a topic"""
    _require_voice_session(session_id)
    try:
        from src.internet.internet import InternetAccess
        
        internet = InternetAccess()
        await internet.initialize()
        
        news = await internet.get_news(topic, num_results)
        
        await internet.close()
        
        return {
            "status": "success",
            "topic": topic,
            "news": news,
            "count": len(news)
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

# =========================================================
# Git Sync API
# =========================================================
@app.post("/api/git-sync")
async def trigger_git_sync(req: dict | None = None):
    _require_admin_session((req or {}).get("session_id"))
    try:
        git_sync(repo_path=".")
        return {"status": "success", "message": "[OK] Code pushed to main branch."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# GitHub Configuration API
# =========================================================
class GitHubConfig(BaseModel):
    repo_url: str | None = None
    username: str | None = None
    password: str | None = None
    ssh_key: str | None = None
    session_id: str | None = None

@app.post("/api/github-config")
async def set_github_config(config: GitHubConfig):
    """Set GitHub credentials for version control."""
    import os
    from pathlib import Path
    
    try:
        _require_admin_session(config.session_id)
        env_file = Path(".env")
        env_vars = {}
        
        # Read existing .env if it exists
        if env_file.exists():
            with open(env_file, "r") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        key, value = line.strip().split("=", 1)
                        env_vars[key] = value
        
        # Update with new values
        if config.repo_url:
            env_vars["GITHUB_REPO"] = config.repo_url
        if config.username:
            env_vars["GITHUB_USERNAME"] = config.username
        if config.password:
            env_vars["GITHUB_PASSWORD"] = config.password
        if config.ssh_key:
            env_vars["SSH_KEY"] = config.ssh_key
        
        # Write back to .env
        with open(env_file, "w") as f:
            for key, value in env_vars.items():
                f.write(f"{key}={value}\n")
        
        # Also set in environment
        for key, value in env_vars.items():
            os.environ[key] = value
        
        return {"status": "success", "message": "GitHub configuration updated"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# Self-Update API (Voice-triggered)
# =========================================================
class SelfUpdateRequest(BaseModel):
    command: str
    file_path: str | None = None
    description: str | None = None
    session_id: str | None = None  # session id of the caller (required for admin actions)

@app.post("/api/self-update")
async def handle_self_update(request: SelfUpdateRequest):
    """Handle self-update commands from voice input."""
    try:
        if CLOUD_MODE:
            return {"status": "error", "message": "Self-update is disabled in cloud mode"}

        # Validate session and admin privileges before allowing self-update
        if not request.session_id:
            return {"status":"error","message":"Admin session required"}
        # Prefer JWT role when configured
        if auth_tokens.secret:
            is_valid, username, payload = auth_tokens.verify(request.session_id)
            if not is_valid or not username:
                return {"status":"error","message":"Invalid or expired session"}
            if (payload or {}).get("role") != "admin":
                return {"status":"error","message":"Admin privileges required"}
        else:
            is_valid, username = voice_auth.validate_session(request.session_id)
            if not is_valid or not username:
                return {"status":"error","message":"Invalid or expired session"}
            if not voice_auth.is_admin(username):
                return {"status":"error","message":"Admin privileges required"}

        # Parse voice command
        parsed = parse_voice_command(request.command)
        
        if not parsed:
            # Try direct update if file_path and description provided
            if request.file_path and request.description:
                result = self_update_file(request.description, request.file_path)
                return result
            return {"status": "error", "message": "Could not parse command"}
        
        action = parsed.get("action")
        
        if action == "update" or action == "edit":
            file_path = parsed.get("target", request.file_path or "")
            description = parsed.get("description", request.description or request.command)
            result = self_update_file(description, file_path)
            return result
        
        elif action == "add":
            feature_type = parsed.get("feature_type", "module")
            description = parsed.get("description", request.description or request.command)
            result = self_add_feature(description, feature_type)
            return result
        
        return {"status": "error", "message": f"Unknown action: {action}"}
        
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# Email Generation API
# =========================================================
class EmailRequest(BaseModel):
    recipient: str
    subject: str | None = None
    body_prompt: str
    tone: str = "professional"
    command: str | None = None  # Voice command to parse

@app.post("/api/generate-email")
async def generate_email_endpoint(email_req: EmailRequest):
    """Generate email from voice command or parameters"""
    try:
        if email_req.command:
            result = email_generator.generate_from_command(email_req.command)
        else:
            result = email_generator.generate_email(
                email_req.recipient,
                email_req.subject,
                email_req.body_prompt,
                email_req.tone
            )
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/email-drafts")
async def get_email_drafts():
    """Get all email drafts"""
    return {"drafts": email_generator.get_drafts()}

# =========================================================
# Screen Access API
# =========================================================
@app.post("/api/capture-screen")
async def capture_screen_endpoint(region: dict | None = None):
    """Capture screen or region"""
    if CLOUD_MODE:
        _cloud_feature_disabled("Screen capture")
    sid = (region or {}).get("session_id") if isinstance(region, dict) else None
    _require_admin_session(sid)
    try:
        reg = None
        if region:
            reg = (region.get("x"), region.get("y"), region.get("width"), region.get("height"))
        include_base64 = bool((region or {}).get("include_base64", False)) if isinstance(region, dict) else False
        screenshot_info = screen_access.take_screenshot_info(region=reg, include_base64=include_base64)
        return screenshot_info
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/read-screen")
async def read_screen_endpoint(region: dict | None = None):
    """Read text from screen using OCR"""
    if CLOUD_MODE:
        _cloud_feature_disabled("Screen OCR")
    sid = (region or {}).get("session_id") if isinstance(region, dict) else None
    _require_admin_session(sid)
    try:
        reg = None
        if region:
            reg = (region.get("x"), region.get("y"), region.get("width"), region.get("height"))
        text = screen_access.read_screen_text(reg)
        return {"text": text, "status": "success"}
    except Exception as e:
        return {"error": str(e), "status": "error"}

# =========================================================
# Application Management API
# =========================================================
class OpenAppRequest(BaseModel):
    app_name: str
    args: List[str] | None = None
    session_id: str | None = None

@app.post("/api/open-app")
async def open_app_endpoint(request: OpenAppRequest):
    """Open an application"""
    if CLOUD_MODE:
        _cloud_feature_disabled("Opening local applications")
    _require_admin_session(request.session_id)
    return app_manager.open_app(request.app_name, request.args)

class AppNameRequest(BaseModel):
    app_name: str
    session_id: str | None = None

@app.post("/api/close-app")
async def close_app_endpoint(request: AppNameRequest):
    """Close an application"""
    if CLOUD_MODE:
        _cloud_feature_disabled("Closing local applications")
    _require_admin_session(request.session_id)
    return app_manager.close_app(request.app_name)

@app.post("/api/switch-app")
async def switch_app_endpoint(request: AppNameRequest):
    """Switch to an application"""
    if CLOUD_MODE:
        _cloud_feature_disabled("Switching local applications")
    _require_admin_session(request.session_id)
    return app_manager.switch_to_app(request.app_name)

@app.get("/api/running-apps")
async def get_running_apps(session_id: str):
    """Get list of running applications"""
    if CLOUD_MODE:
        _cloud_feature_disabled("Listing local applications")
    _require_authenticated_session(session_id)
    return {"apps": app_manager.list_running_apps()}

class ExecuteCommandRequest(BaseModel):
    command: str
    wait: bool = True
    session_id: str | None = None

@app.post("/api/execute-command")
async def execute_command_endpoint(request: ExecuteCommandRequest):
    """Execute a system command"""
    if CLOUD_MODE:
        _cloud_feature_disabled("Executing commands")
    _require_admin_session(request.session_id)
    return app_manager.execute_command(request.command, request.wait)

# =========================================================
# Task Management API
# =========================================================
class CreateTaskRequest(BaseModel):
    description: str
    steps: List[dict]
    priority: int = 5
    session_id: str | None = None

@app.post("/api/create-task")
async def create_task_endpoint(request: CreateTaskRequest):
    """Create a new task"""
    # Cloud: require auth and bind task to user.
    meta = {}
    if CLOUD_MODE:
        principal = _require_authenticated_session(request.session_id)
        meta = {"user_id": principal.get("username")}
    else:
        # Local: tasks can be powerful; require admin if a session_id was provided.
        # (If no auth system is in use locally, preserve legacy behavior.)
        if request.session_id:
            _require_admin_session(request.session_id)

    task_id = task_manager.create_task(request.description, request.steps, request.priority, meta=meta)
    return {"status": "success", "task_id": task_id}

class StopTaskRequest(BaseModel):
    task_id: str | None = None
    session_id: str | None = None
    reason: str | None = None

@app.post("/api/stop-task")
async def stop_task_endpoint(request: StopTaskRequest | None = None):
    """Stop current task or request cancellation for a specific task.

    Backward compatible:
    - If called with no body (or no task_id), stops the current task.
    - If task_id is provided, marks that task as cancel_requested (cooperative cancellation).
    """
    # Cancel a specific task (used for async research jobs)
    if request and request.task_id:
        principal = _require_authenticated_session(request.session_id)
        username = (principal.get("username") or "").strip().lower()
        is_admin = (principal.get("role") == "admin")

        task = None
        try:
            task = task_manager.get_task(request.task_id)
        except Exception:
            task = None

        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

        owner = None
        try:
            meta = task.get("meta")
            if isinstance(meta, dict):
                owner = (meta.get("user_id") or "").strip().lower() or None
        except Exception:
            owner = None

        # Only allow task owner to cancel, unless admin.
        if owner and owner != username and not is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to cancel this task")

        try:
            return task_manager.request_cancel(request.task_id, reason=request.reason)
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    # Default legacy behavior: stop current task
    # In cloud mode, require a task_id to avoid global stop of unrelated work.
    if CLOUD_MODE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task_id is required in cloud mode",
        )
    return task_manager.stop_current_task()

@app.get("/api/current-task")
async def get_current_task(session_id: str | None = None):
    """Get current task"""
    if CLOUD_MODE:
        _require_authenticated_session(session_id)
    task = task_manager.get_current_task()
    return {"task": task} if task else {"task": None}

@app.get("/api/tasks")
async def get_all_tasks(session_id: str | None = None):
    """Get all tasks"""
    tasks = task_manager.get_all_tasks()

    if not CLOUD_MODE:
        return {"tasks": tasks}

    principal = _require_authenticated_session(session_id)
    username = (principal.get("username") or "").strip().lower()
    is_admin = (principal.get("role") == "admin")
    if is_admin:
        return {"tasks": tasks}

    filtered = []
    for t in tasks:
        try:
            meta = t.get("meta") if isinstance(t, dict) else None
            owner = (meta.get("user_id") or "").strip().lower() if isinstance(meta, dict) else ""
            if owner and owner == username:
                filtered.append(t)
        except Exception:
            continue
    return {"tasks": filtered}

@app.get("/api/wakeup-context")
async def get_wakeup_context(session_id: str | None = None):
    """Get wakeup context mapping"""
    if CLOUD_MODE:
        _require_authenticated_session(session_id)
    return {"context": task_manager.get_wakeup_context()}

# =========================================================
# Error Handling API
# =========================================================
@app.post("/api/check-errors")
async def check_errors_endpoint():
    """Check for errors and auto-fix"""
    return error_handler.monitor_and_fix()

@app.get("/api/render-logs")
async def get_render_logs():
    """Get Render logs"""
    return error_handler.check_render_logs()

@app.post("/api/fix-error")
async def fix_error_endpoint(error: dict):
    """Fix a specific error"""
    return error_handler.auto_fix_error(error)

# =========================================================
# File Operations API (Via MCP or Local)
# =========================================================
class FileRequest(BaseModel):
    path: str
    session_id: str | None = None

class FileWriteRequest(BaseModel):
    path: str
    content: str
    session_id: str | None = None

class FileCopyRequest(BaseModel):
    source: str
    destination: str
    session_id: str | None = None

@app.post("/api/files/read")
async def read_file_endpoint(req: FileRequest):
    """Read file content"""
    if CLOUD_MODE:
        _cloud_feature_disabled("File operations")
    _require_admin_session(req.session_id)
    try:
        result = file_ops.read_file(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/write")
async def write_file_endpoint(req: FileWriteRequest):
    """Write content to file"""
    if CLOUD_MODE:
        _cloud_feature_disabled("File operations")
    _require_admin_session(req.session_id)
    try:
        result = file_ops.write_file(req.path, req.content)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/list")
async def list_files_endpoint(req: FileRequest):
    """List files in directory"""
    if CLOUD_MODE:
        _cloud_feature_disabled("File operations")
    _require_admin_session(req.session_id)
    try:
        result = file_ops.list_files(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/delete")
async def delete_file_endpoint(req: FileRequest):
    """Delete a file"""
    if CLOUD_MODE:
        _cloud_feature_disabled("File operations")
    _require_admin_session(req.session_id)
    try:
        result = file_ops.delete_file(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/mkdir")
async def create_directory_endpoint(req: FileRequest):
    """Create a directory"""
    if CLOUD_MODE:
        _cloud_feature_disabled("File operations")
    _require_admin_session(req.session_id)
    try:
        result = file_ops.create_directory(req.path)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/copy")
async def copy_file_endpoint(req: FileCopyRequest):
    """Copy a file"""
    if CLOUD_MODE:
        _cloud_feature_disabled("File operations")
    _require_admin_session(req.session_id)
    try:
        result = file_ops.copy_file(req.source, req.destination)
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/files/cleanup")
async def cleanup_project_endpoint(req: dict | None = None):
    """Clean up project cache files"""
    if CLOUD_MODE:
        _cloud_feature_disabled("File operations")
    _require_admin_session((req or {}).get("session_id"))
    try:
        result = file_ops.cleanup_project()
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

# =========================================================
# System Operations API (Digital Assistant PC Control)
# =========================================================
def _system_ops_unavailable():
    """Helper function to return error when system_ops is unavailable"""
    return {"status": "error", "message": "System operations not available on this platform"}

@app.get("/api/system/info")
async def get_system_info(session_id: str):
    """Get current system information"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_authenticated_session(session_id)
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.get_system_info()

@app.get("/api/system/processes")
async def list_processes_endpoint(session_id: str, filter: Optional[str] = None):
    """List running processes"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_authenticated_session(session_id)
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.list_processes(filter)

@app.post("/api/system/process-kill")
async def kill_process_endpoint(req: dict):
    """Kill a process by name"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    process_name = req.get("process_name")
    if not process_name:
        return {"status": "error", "message": "process_name required"}
    return system_ops.kill_process(process_name)

@app.post("/api/system/launch-app")
async def launch_application_endpoint(req: dict):
    """Launch an application"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    app_path = req.get("app_path")
    args = req.get("args", [])
    if not app_path:
        return {"status": "error", "message": "app_path required"}
    return system_ops.launch_application(app_path, args)

@app.post("/api/system/execute")
async def execute_command_endpoint(req: dict):
    """Execute a shell command"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    command = req.get("command")
    timeout = req.get("timeout", 30)
    if not command:
        return {"status": "error", "message": "command required"}
    return system_ops.execute_command(command, timeout)

@app.get("/api/system/screen")
async def get_screen_info(session_id: str):
    """Get screen/display information"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_authenticated_session(session_id)
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.get_screen_info()

@app.post("/api/system/screenshot")
async def take_screenshot(req: dict = None):
    """Take a screenshot"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    save_path = req.get("save_path") if req else None
    return system_ops.take_screenshot(save_path)

@app.post("/api/system/mouse-move")
async def move_mouse_endpoint(req: dict):
    """Move mouse to position"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    x = req.get("x")
    y = req.get("y")
    if x is None or y is None:
        return {"status": "error", "message": "x and y required"}
    return system_ops.move_mouse(int(x), int(y))

@app.post("/api/system/mouse-click")
async def click_mouse_endpoint(req: dict):
    """Click mouse at position"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    x = req.get("x")
    y = req.get("y")
    button = req.get("button", "left")
    if x is None or y is None:
        return {"status": "error", "message": "x and y required"}
    return system_ops.click_mouse(int(x), int(y), button)

@app.post("/api/system/type-text")
async def type_text_endpoint(req: dict):
    """Type text using keyboard"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    text = req.get("text")
    interval = req.get("interval", 0.1)
    if not text:
        return {"status": "error", "message": "text required"}
    return system_ops.type_text(text, interval)

@app.post("/api/system/press-key")
async def press_key_endpoint(req: dict):
    """Press a keyboard key"""
    if CLOUD_MODE:
        _cloud_feature_disabled("System operations")
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    key = req.get("key")
    if not key:
        return {"status": "error", "message": "key required"}
    return system_ops.press_key(key)

@app.post("/api/system/open-file")
async def open_file_endpoint(req: dict):
    """Open a file with default application"""
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    file_path = req.get("file_path")
    if not file_path:
        return {"status": "error", "message": "file_path required"}
    return system_ops.open_file(file_path)

@app.get("/api/system/windows")
async def get_open_windows(session_id: str):
    """Get list of open windows"""
    _require_authenticated_session(session_id)
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    return system_ops.get_open_windows()

@app.post("/api/system/window-focus")
async def focus_window_endpoint(req: dict):
    """Focus a window by title"""
    _require_admin_session((req or {}).get("session_id"))
    if not SYSTEM_OPS_AVAILABLE:
        return _system_ops_unavailable()
    window_title = req.get("window_title")
    if not window_title:
        return {"status": "error", "message": "window_title required"}
    return system_ops.focus_window(window_title)

# =========================================================
# Session Management Endpoints
# =========================================================
@app.post("/api/session/extend")
async def extend_session_endpoint(req: dict):
    """Extend current session on page reload"""
    session_id = req.get("session_id")
    if not session_id:
        return {"status": "error", "message": "session_id required"}
    
    is_valid, username = session_manager.validate_session(session_id, update_activity=True)
    if not is_valid:
        return {
            "status": "session_expired",
            "message": "Session expired. Please login again.",
            "action": "redirect_to_login"
        }
    
    # Extend session
    extended = session_manager.extend_session(session_id)
    
    return {
        "status": "success" if extended else "error",
        "message": "Session extended" if extended else "Failed to extend session",
        "username": username,
        "session_info": session_manager.get_session_info(session_id)
    }

@app.post("/api/session/check")
async def check_session_endpoint(req: dict):
    """Check if session is still valid"""
    session_id = req.get("session_id")
    if not session_id:
        return {"valid": False, "message": "No session_id provided"}
    
    is_valid, username = session_manager.validate_session(session_id, update_activity=False)
    
    return {
        "valid": is_valid,
        "username": username,
        "session_info": session_manager.get_session_info(session_id) if is_valid else None
    }

@app.post("/api/session/logout")
async def logout_session_endpoint(req: dict):
    """Logout from current session"""
    session_id = req.get("session_id")
    if not session_id:
        return {"status": "error", "message": "session_id required"}
    
    success = session_manager.invalidate_session(session_id)
    
    return {
        "status": "success" if success else "error",
        "message": "Logged out successfully" if success else "Session not found"
    }

@app.get("/api/session/stats")
async def get_session_stats():
    """Get session statistics"""
    return session_manager.get_session_stats()

# =========================================================
# Startup Event
# =========================================================

async def startup_event():
    print("[OK] Jarvis server startup event running")
    try:
        database._ensure_connected()
        print("[DB] Connection check complete")
    except Exception as e:
        print(f"[INFO] DB error during startup (will retry): {e}")
    
    # Start session cleanup task
    try:
        start_session_cleanup_task()
        print("[OK] Session cleanup task started")
    except Exception as e:
        print(f"[INFO] Could not start session cleanup (already running): {e}")

    # Start background job scheduler (web training, cleanup, etc.)
    if ENABLE_SCHEDULER and SCHEDULER_AVAILABLE and initialize_scheduler:
        try:
            initialize_scheduler()
        except Exception as e:
            print(f"[INFO] Scheduler failed to start: {e}")
    
    print("[OK] Jarvis server started and git-sync initialized.")


async def shutdown_event():
    if SCHEDULER_AVAILABLE and shutdown_scheduler:
        try:
            shutdown_scheduler()
        except Exception:
            pass


# =========================================================
# Root endpoint - API info
# =========================================================
@app.get("/")
async def root():
    # If frontend build exists, serve UI.
    if FRONTEND_BUILD_DIR.exists():
        index = FRONTEND_BUILD_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))

    # Otherwise, show API info.
    return JSONResponse({
        "message": "Jarvis Cloud Assistant API",
        "version": "1.0",
        "docs": "/docs"
    })


# Mount static frontend at the very end so API routes win.
if FRONTEND_BUILD_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_BUILD_DIR), html=True), name="frontend")
