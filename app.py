import os
import asyncio
from pathlib import Path
import time
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import List, Optional

from src.core.llm_adapter import LLMAdapter
from src.core.jarvis_brain import JarvisBrain
from src.core.executor import ActionExecutor
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
app = FastAPI(title="Jarvis Cloud Assistant")
load_dotenv()

START_TS = time.time()

# Serve frontend build if present (single-service deploy)
FRONTEND_BUILD_DIR = Path(__file__).resolve().parent / "jarvis-frontend" / "build"

# Enable/disable background scheduler via env
ENABLE_SCHEDULER = os.getenv("JARVIS_ENABLE_SCHEDULER", "true").lower() in ("1", "true", "yes", "y")

# =========================================================
# Runtime Mode / Security
# =========================================================
# Cloud mode is intended for hosted deployments (e.g., Render). In this mode we:
# - Require an authenticated session for chat + internet endpoints (to prevent public abuse)
# - Disable local/PC control and local filesystem endpoints (these are unsafe + meaningless in cloud)
CLOUD_MODE = os.getenv("JARVIS_CLOUD_MODE", "false").lower() in ("1", "true", "yes", "y")
AGENT_SHARED_SECRET = os.getenv("JARVIS_AGENT_SHARED_SECRET", "")
DEFAULT_DEVICE_ID = os.getenv("JARVIS_DEFAULT_DEVICE_ID", "primary")
DEVICE_OWNER_USERNAME = os.getenv("JARVIS_DEVICE_OWNER_USERNAME", "")
ADMIN_USERNAME = (os.getenv("JARVIS_ADMIN_USERNAME", "admin") or "admin").strip().lower()
ADMIN_BOOTSTRAP_SECRET = os.getenv("JARVIS_ADMIN_BOOTSTRAP_SECRET", "")

device_hub = DeviceHub(shared_secret=AGENT_SHARED_SECRET)
auth_tokens = AuthTokens()

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
        # Fallback to the role embedded in the token if the store is unavailable.
        role = None
        try:
            role = voice_auth.get_role(username)
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
    "capture_screen", "screen_navigation",
    # Filesystem
    "read", "list", "mkdir",
    "write", "edit", "delete", "move", "copy", "cleanup",
    # Self-modifying
    "self_update", "self_add",
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

def _require_device_owner(username: str | None):
    if DEVICE_OWNER_USERNAME and (username or "").lower() != DEVICE_OWNER_USERNAME.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not permitted to control the connected device.",
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
    return False


def _public_user_profile(username: str | None) -> dict | None:
    """Return a safe subset of user data for the frontend.

    Never return voice hashes, password hashes, salts, or other secrets.
    """
    if not username:
        return None
    try:
        u = voice_auth.get_user(username) or {}
        return {
            "username": (u.get("username") or username).strip().lower(),
            "role": (u.get("role") or voice_auth.get_role(username) or "user").strip().lower(),
            "created_at": u.get("created_at"),
            "last_login": u.get("last_login"),
            "updated_at": u.get("updated_at"),
        }
    except Exception:
        return {"username": (username or "").strip().lower(), "role": "user"}


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
        "open_app", "close_app", "switch_app",
        "execute_command",
        "capture_screen", "screen_navigation",
        # Filesystem actions should run on the user's machine (agent), not on Render.
        "read", "list", "mkdir",
        "write", "edit", "delete", "move", "copy", "cleanup",
        "self_update", "self_add",
    }

async def _dispatch_actions_to_device(device_id: str, username: str, actions: list[dict], source_text: str):
    """Forward actions to a connected local agent."""
    job = {
        "job_id": f"job_{os.urandom(8).hex()}",
        "device_id": device_id,
        "username": username,
        "source_text": source_text,
        "actions": actions,
    }
    await device_hub.send_job(device_id, job)
    return job


# =========================================================
# Remote Agent (WebSocket)
# =========================================================
@app.websocket("/ws/agent")
async def agent_ws(ws: WebSocket):
    await ws.accept()
    device_id: str | None = None
    try:
        # Expect initial auth message
        raw = await ws.receive_text()
        msg = {}
        try:
            import json
            msg = json.loads(raw)
        except Exception:
            await ws.close(code=1008)
            return

        if msg.get("type") != "auth":
            await ws.close(code=1008)
            return

        device_id = (msg.get("device_id") or "").strip() or None
        secret = (msg.get("secret") or "").strip()
        try:
            await device_hub.register(device_id=device_id or "", secret=secret, websocket=ws)
        except PermissionError:
            await ws.close(code=1008)
            return

        await ws.send_json({"type": "ack", "device_id": device_id, "status": "connected"})

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

            if payload.get("type") == "result":
                # For now, just log results. You can persist to DB later.
                await device_hub.touch(device_id)
                logger = __import__('logging').getLogger(__name__)
                logger.info("[AGENT RESULT] %s", payload)
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
# Remote Device Control APIs (Cloud -> Agent)
# =========================================================
class DeviceDispatchRequest(BaseModel):
    session_id: str | None = None
    device_id: str | None = None
    actions: List[dict]
    source_text: str | None = ""


@app.post("/api/device/dispatch")
async def device_dispatch(req: DeviceDispatchRequest):
    """Forward actions to a connected PC agent.

    This is only meaningful in cloud mode.
    """
    p = _get_principal(req.session_id)
    username = p.get("username")
    if not _can_control_device(p):
        raise HTTPException(status_code=403, detail="Not permitted to control the connected device")
    _require_device_owner(username)
    did = req.device_id or DEFAULT_DEVICE_ID
    if not await device_hub.is_connected(did):
        raise HTTPException(status_code=409, detail="Device agent is not connected")

    job = await _dispatch_actions_to_device(did, username=username or "user", actions=req.actions, source_text=req.source_text or "")
    return {"status": "queued", "job": job}


@app.get("/api/device/status")
async def device_status(session_id: str):
    p = _get_principal(session_id)
    username = p.get("username")
    if not _can_control_device(p):
        raise HTTPException(status_code=403, detail="Not permitted to control the connected device")
    _require_device_owner(username)
    agents_by_id = await device_hub.list_agents()
    agents = list(agents_by_id.values())
    return {"status": "success", "agents": agents, "default_device_id": DEFAULT_DEVICE_ID}


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

# =========================================================
# CORS Configuration
# =========================================================
cors_origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "https://jarvis-frontend.onrender.com",
    "https://jarvis-cloud-assistant.onrender.com"
]

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

class MessageIn(BaseModel):
    user: str | None = "user"
    text: str
    mode: str | None = "chat"
    session_id: str | None = None  # Voice auth session

class VoiceAuthRequest(BaseModel):
    username: str
    voice_sample_hash: str | None = None
    password: str | None = None
    action: str  # "register" or "login"
    role: str | None = None  # optional: 'admin' or 'user'

# =========================================================
# Voice Authentication Endpoints
# =========================================================
@app.post("/api/voice-auth")
async def voice_auth_endpoint(auth_req: VoiceAuthRequest):
    """Handle voice-based authentication"""
    try:
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
                role=role
            )
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
                    ok, sid_or_err = voice_auth.authenticate_by_voice(uname, auth_req.voice_sample_hash, auth_req.password)
                    if ok:
                        result["session_id"] = sid_or_err
            return result
        
        elif auth_req.action == "login":
            if not auth_req.voice_sample_hash:
                return {"status": "error", "message": "Voice sample required for login"}
            is_valid, session_or_error = voice_auth.authenticate_by_voice(
                auth_req.username,
                auth_req.voice_sample_hash,
                auth_req.password
            )
            if is_valid:
                uname = auth_req.username.strip().lower()
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
                    "session_id": session_id
                }
            return {
                "status": "error",
                "message": session_or_error or "Authentication failed"
            }
        
        return {"status": "error", "message": "Invalid action"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# Simple health endpoint used by local startup checks
@app.get("/health")
async def health_check(check_db: int = 0):
    """Health endpoint for monitoring.

    - Always returns 200 when the API process is alive.
    - `check_db=1` performs a best-effort DB ping with a short timeout.
    """
    db_uri = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
    db_configured = bool(db_uri)
    db_connected = bool(getattr(database, "client", None) and getattr(database, "db", None))
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
async def logout(session_id: str):
    """Logout and invalidate session"""
    if auth_tokens.secret:
        success = auth_tokens.revoke(session_id)
    else:
        success = voice_auth.logout(session_id)
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
    principal = _get_principal(msg.session_id) if msg.session_id else {"username": None, "role": "anonymous"}
    username = None
    role = principal.get("role", "anonymous")
    if msg.session_id:
        username = _require_voice_session(msg.session_id)
    if username:
        msg.user = username
    
    response = await brain.handle_message(msg.text, mode=msg.mode)
    actions = response.get("actions", [])

    # Enforce permissions on actions
    if actions:
        if CLOUD_MODE:
            # In cloud mode, only forward device actions for the configured device owner (or admin).
            if not _can_control_device(principal):
                blocked = [a for a in actions if _is_remote_device_action(a)]
                actions = [a for a in actions if not _is_remote_device_action(a)]
                response["actions"] = actions
                if blocked:
                    response["text"] = (response.get("text") or "") + "\n\n(Device actions are not permitted for this account.)"
        else:
            # Local mode: admin-only actions require admin role.
            if role != "admin":
                blocked = [a for a in actions if (a or {}).get("type") in ADMIN_ONLY_ACTION_TYPES]
                actions = [a for a in actions if (a or {}).get("type") not in ADMIN_ONLY_ACTION_TYPES]
                response["actions"] = actions
                if blocked:
                    response["text"] = (response.get("text") or "") + "\n\n(Some actions require admin privileges and were skipped.)"

    # In cloud mode, forward any PC/device actions to the connected local agent.
    if CLOUD_MODE and actions:
        device_actions = [a for a in actions if _is_remote_device_action(a)]
        safe_actions = [a for a in actions if not _is_remote_device_action(a)]

        if device_actions:
            if not _can_control_device(principal):
                response["text"] = (response.get("text") or "") + "\n\n(Device actions are not permitted for this account.)"
            else:
                _require_device_owner(msg.user)
                did = DEFAULT_DEVICE_ID
                if await device_hub.is_connected(did):
                    background_tasks.add_task(
                        _dispatch_actions_to_device,
                        did,
                        msg.user or "user",
                        device_actions,
                        msg.text,
                    )
                    response["text"] = (response.get("text") or "") + "\n\n(Queued actions for your connected PC.)"
                else:
                    response["text"] = (response.get("text") or "") + "\n\n(Your PC agent is offline — start pc_agent.py on your Windows PC.)"

        response["actions"] = safe_actions
        return response

    # Local mode: execute actions directly on this machine.
    if actions:
        background_tasks.add_task(executor.process_actions, actions, msg.user)
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
async def get_news_endpoint(topic: str = "latest", num_results: int = 5):
    """Get latest news on a topic"""
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
        screenshot_info = screen_access.take_screenshot_info()
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

@app.post("/api/create-task")
async def create_task_endpoint(request: CreateTaskRequest):
    """Create a new task"""
    task_id = task_manager.create_task(request.description, request.steps, request.priority)
    return {"status": "success", "task_id": task_id}

@app.post("/api/stop-task")
async def stop_task_endpoint():
    """Stop current task"""
    return task_manager.stop_current_task()

@app.get("/api/current-task")
async def get_current_task():
    """Get current task"""
    task = task_manager.get_current_task()
    return {"task": task} if task else {"task": None}

@app.get("/api/tasks")
async def get_all_tasks():
    """Get all tasks"""
    return {"tasks": task_manager.get_all_tasks()}

@app.get("/api/wakeup-context")
async def get_wakeup_context():
    """Get wakeup context mapping"""
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

@app.on_event("startup")
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


@app.on_event("shutdown")
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
