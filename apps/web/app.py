import os
import sys
import asyncio
import logging
from pathlib import Path
import time
import re
import uuid
from datetime import datetime
import base64
import secrets
from datetime import timedelta, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi import status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import List, Optional, Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import env
from src.config import runtime_defaults as rd
from src.config.settings import settings as jarvis_settings

from src.core.llm_adapter import LLMAdapter
from src.core.jarvis_brain import JarvisBrain
from src.core.executor import ActionExecutor
from src.core.chat_orchestrator import ChatOrchestrator
from src.core.module_update_cycle import ModuleUpdateCycleService
from src.core.notification_hub import notification_hub
from src.autonomy.runtime import AutonomyRuntime
from src.utils.git_sync import git_sync, setup_ssh_trust

# Self-update is optional and may pull in extra dependencies. Keep API boot resilient.
try:
    from src.utils.self_update import (
        parse_voice_command,
        self_update_file,
        self_add_feature,
        rollback_file,
        get_update_history,
    )
    SELF_UPDATE_AVAILABLE = True
except Exception:
    parse_voice_command = None
    self_update_file = None
    self_add_feature = None
    rollback_file = None
    get_update_history = None
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
from src.api.internet_routes import build_internet_router
from src.api.session_routes import build_session_router
from src.api.system_control_routes import build_system_control_router
from src.api.telegram_routes import build_telegram_router

logger = logging.getLogger(__name__)

# Optional shared broker for multi-instance deployments (Redis pub/sub).
try:
    from src.broker.redis_broker import maybe_create_broker
except Exception:
    maybe_create_broker = None

# Background scheduler (optional)
try:
    from src.jobs.job_scheduler import initialize_scheduler, shutdown_scheduler, get_progressive_llm_update_report
    SCHEDULER_AVAILABLE = True
except Exception:
    initialize_scheduler = None
    shutdown_scheduler = None
    get_progressive_llm_update_report = None
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

    # Multi-instance support: when a broker is configured, subscribe to
    # cross-instance notifications and agent job routing.
    try:
        if _BROKER is not None:
            try:
                notification_hub.start_broker_listener()
            except Exception:
                pass
            try:
                if hasattr(device_hub, "start_broker_listener"):
                    device_hub.start_broker_listener()
            except Exception:
                pass
            print("[OK] Broker enabled (multi-instance)")
    except Exception:
        pass
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

    try:
        await autonomy_runtime.start()
        print("[OK] Autonomous runtime started")
    except Exception as e:
        print(f"[INFO] Autonomous runtime start failed: {e}")

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

        # Broker shutdown (best-effort)
        try:
            await notification_hub.shutdown()
        except Exception:
            pass
        try:
            if hasattr(device_hub, "shutdown"):
                await device_hub.shutdown()
        except Exception:
            pass
        try:
            if _BROKER is not None:
                await _BROKER.close()
        except Exception:
            pass

        try:
            await autonomy_runtime.stop()
        except Exception:
            pass


app = FastAPI(title="Jarvis Cloud Assistant", lifespan=lifespan)
load_dotenv()

# Instantiate broker early so hubs can attach it (listeners start in lifespan).
_BROKER = None
try:
    if maybe_create_broker is not None:
        _BROKER = maybe_create_broker()
        if _BROKER is not None:
            try:
                notification_hub.attach_broker(_BROKER)
            except Exception:
                pass
except Exception:
    _BROKER = None

START_TS = time.time()

# ---------------------------------------------------------
# Multi-instance / multi-worker note
# ---------------------------------------------------------
# Without a shared broker (Redis), the following features are per-process:
# - websocket notifications hub
# - device agent routing (in-memory registry)
#
# For production deployments with multiple workers/instances, configure:
# - JARVIS_REDIS_URL
#
# If you cannot run Redis, enforce a single worker (e.g., gunicorn -w 1)
# to preserve correctness.
if bool(jarvis_settings.cloud_mode) and (_BROKER is None):
    try:
        import logging

        logging.getLogger(__name__).warning(
            "CLOUD_MODE enabled but no broker configured (JARVIS_REDIS_URL missing). "
            "Websocket notifications and PC-agent routing will only work within a single process. "
            "Run a single worker or configure Redis."
        )
    except Exception:
        pass

# Serve frontend build if present (single-service deploy)
FRONTEND_BUILD_DIR = REPO_ROOT / "frontend" / "build"

# Enable/disable background scheduler via env
ENABLE_SCHEDULER = env.get_bool("JARVIS_ENABLE_SCHEDULER", True)

# =========================================================
# Runtime Mode / Security
# =========================================================
# Cloud mode is intended for hosted deployments (e.g., Render). In this mode we:
# - Require an authenticated session for chat + internet endpoints (to prevent public abuse)
# - Disable local/PC control and local filesystem endpoints (these are unsafe + meaningless in cloud)
CLOUD_MODE = bool(jarvis_settings.cloud_mode)
VOICE_ONLY_MODE = env.get_bool("JARVIS_VOICE_ONLY", False)
PC_AGENT_ENABLED = env.get_bool("JARVIS_ENABLE_PC_AGENT", True)
AGENT_SHARED_SECRET = env.get_str("JARVIS_AGENT_SHARED_SECRET", "")
EXPOSE_AGENT_SHARED_SECRET = env.get_bool("JARVIS_EXPOSE_AGENT_SHARED_SECRET", False)
DEFAULT_DEVICE_ID = env.get_str("JARVIS_DEFAULT_DEVICE_ID", "primary")
DEVICE_OWNER_USERNAME = env.get_str("JARVIS_DEVICE_OWNER_USERNAME", "")
LOCAL_DEFAULT_DEVICE_FALLBACK = env.get_bool("JARVIS_LOCAL_DEFAULT_DEVICE_FALLBACK", True)
ADMIN_USERNAME = (env.get_str("JARVIS_ADMIN_USERNAME", "admin") or "admin").strip().lower()
ADMIN_BOOTSTRAP_SECRET = env.get_str("JARVIS_ADMIN_BOOTSTRAP_SECRET", "")
VOICE_BIOMETRICS_STRICT_LOGIN = False

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

_JOB_RESULT_CACHE_TTL_S = 5 * 60
_JOB_RESULT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JOB_RESULT_WAITERS: dict[str, asyncio.Event] = {}

_DELEGATED_TASK_INDEX_READY = False

_DEVICE_REGISTRY_INDEX_READY = False
_USER_DEVICE_LINK_INDEX_READY = False
_DEVICE_PERMISSIONS_INDEX_READY = False
_AGENT_CONFIG_INDEX_READY = False
_REQUIREMENTS_AUDIT_INDEX_READY = False


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


def _delegated_tasks_collection():
    """Collection storing cloud delegated tasks for queue/resume lifecycle."""
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["delegated_tasks"]
    global _DELEGATED_TASK_INDEX_READY
    if not _DELEGATED_TASK_INDEX_READY:
        try:
            col.create_index([("status", 1), ("device_id", 1), ("updated_at", -1)])
        except Exception:
            pass
        try:
            col.create_index([("username", 1), ("feature", 1), ("status", 1), ("updated_at", -1)])
        except Exception:
            pass
        try:
            col.create_index("task_id", unique=True)
        except Exception:
            pass
        try:
            col.create_index("last_job_id")
        except Exception:
            pass
        _DELEGATED_TASK_INDEX_READY = True
    return col


def _normalize_flow_status(value: str | None, *, default: str = "awaiting_agent") -> str:
    s = str(value or "").strip().lower()
    allowed = {
        "available",
        "executing",
        "delegated",
        "queued_for_agent",
        "awaiting_agent",
        "pending_permission",
        "restricted",
        "failed",
        "completed",
    }
    return s if s in allowed else default


def _remember_job_result(payload: dict[str, Any]) -> None:
    try:
        job_id = str((payload or {}).get("job_id") or "").strip()
        if not job_id:
            return
        now = time.time()
        _JOB_RESULT_CACHE[job_id] = (now, payload)

        waiter = _JOB_RESULT_WAITERS.get(job_id)
        if waiter:
            waiter.set()

        if len(_JOB_RESULT_CACHE) > 1000:
            cutoff = now - _JOB_RESULT_CACHE_TTL_S
            for k, v in list(_JOB_RESULT_CACHE.items()):
                if (not v) or (v[0] < cutoff):
                    _JOB_RESULT_CACHE.pop(k, None)
    except Exception:
        return


async def _await_job_result(job_id: str, timeout_s: float = 2.5) -> dict[str, Any] | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None

    try:
        cached = _JOB_RESULT_CACHE.get(jid)
        if cached and (time.time() - float(cached[0])) <= _JOB_RESULT_CACHE_TTL_S:
            return cached[1]
    except Exception:
        pass

    waiter = _JOB_RESULT_WAITERS.get(jid)
    if waiter is None:
        waiter = asyncio.Event()
        _JOB_RESULT_WAITERS[jid] = waiter

    try:
        await asyncio.wait_for(waiter.wait(), timeout=max(0.1, float(timeout_s or 0.1)))
    except Exception:
        pass
    finally:
        _JOB_RESULT_WAITERS.pop(jid, None)

    try:
        cached = _JOB_RESULT_CACHE.get(jid)
        if cached:
            return cached[1]
    except Exception:
        pass
    return None


def _queue_delegated_task(
    *,
    username: str,
    role: str,
    device_id: str | None,
    feature: str,
    source_text: str,
    actions: list[dict[str, Any]],
    status_value: str,
    reason: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    normalized_status = _normalize_flow_status(status_value)
    task = {
        "task_id": f"dlg_{uuid.uuid4().hex[:16]}",
        "username": (username or "").strip().lower() or None,
        "role": (role or "user").strip().lower(),
        "device_id": _normalize_device_id(device_id) or None,
        "feature": str(feature or "device_action").strip() or "device_action",
        "source_text": str(source_text or "").strip(),
        "actions": [a for a in (actions or []) if isinstance(a, dict)],
        "status": normalized_status,
        "reason": str(reason or "").strip() or None,
        "attempts": 0,
        "last_job_id": None,
        "created_at": now,
        "updated_at": now,
        "created_at_iso": now.isoformat(),
        "updated_at_iso": now.isoformat(),
    }

    try:
        col = _delegated_tasks_collection()
        if col is not None:
            # De-duplicate very recent equivalent pending items from repeated polling.
            existing = col.find_one(
                {
                    "username": task["username"],
                    "feature": task["feature"],
                    "device_id": task["device_id"],
                    "status": {"$in": ["awaiting_agent", "queued_for_agent", "delegated", "executing"]},
                },
                {"_id": 0},
                sort=[("updated_at", -1)],
            )
            if isinstance(existing, dict):
                return _to_json_safe(existing)
            col.insert_one(task)
            task.pop("_id", None)
    except Exception:
        pass

    return _to_json_safe(task)


def _mark_delegated_task(*, task_id: str | None = None, job_id: str | None = None, status_value: str, extra: dict[str, Any] | None = None) -> None:
    try:
        col = _delegated_tasks_collection()
        if col is None:
            return
        q = None
        if task_id:
            q = {"task_id": str(task_id).strip()}
        elif job_id:
            q = {"last_job_id": str(job_id).strip()}
        if not q:
            return

        now = datetime.now(timezone.utc)
        update = {
            "status": _normalize_flow_status(status_value),
            "updated_at": now,
            "updated_at_iso": now.isoformat(),
        }
        if isinstance(extra, dict):
            update.update(extra)
        col.update_one(q, {"$set": update}, upsert=False)
    except Exception:
        return


def _delegated_status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "available": 0,
        "executing": 0,
        "delegated": 0,
        "queued_for_agent": 0,
        "awaiting_agent": 0,
        "pending_permission": 0,
        "requires_configuration": 0,
        "restricted": 0,
        "failed": 0,
        "completed": 0,
    }
    for row in rows or []:
        s = _normalize_flow_status((row or {}).get("status"), default="awaiting_agent")
        if s in counts:
            counts[s] += 1
    return counts


def _list_delegated_tasks_for_principal(
    principal: dict[str, Any],
    *,
    limit: int = 100,
    statuses: str | None = None,
) -> list[dict[str, Any]]:
    col = _delegated_tasks_collection()
    if col is None:
        return []

    username = str((principal or {}).get("username") or "").strip().lower()
    role = str((principal or {}).get("role") or "user").strip().lower()
    is_admin = role == "admin"

    q: dict[str, Any] = {}
    if statuses:
        vals = [str(s).strip().lower() for s in str(statuses).split(",") if str(s).strip()]
        vals = [_normalize_flow_status(v, default=v) for v in vals]
        if vals:
            q["status"] = {"$in": vals}
    if (not is_admin) and username:
        q["username"] = username

    lim = max(1, min(int(limit or 100), 400))
    rows = list(col.find(q, {"_id": 0}).sort("updated_at", -1).limit(lim))
    return [_to_json_safe(r) for r in rows]


async def _resume_queued_delegations_for_device(device_id: str) -> None:
    did = _normalize_device_id(device_id)
    if not did:
        return

    col = _delegated_tasks_collection()
    if col is None:
        return

    owner = _get_device_owner(did)
    query = {
        "status": {"$in": ["awaiting_agent", "queued_for_agent"]},
        "$or": [
            {"device_id": did},
            {"device_id": None, "username": owner},
        ],
    }

    rows = []
    try:
        rows = list(col.find(query, {"_id": 0}).sort("updated_at", 1).limit(30))
    except Exception:
        rows = []

    for row in rows:
        try:
            actions = row.get("actions") or []
            if not isinstance(actions, list) or not actions:
                _mark_delegated_task(task_id=row.get("task_id"), status_value="failed", extra={"error": "missing_actions"})
                continue

            username = str(row.get("username") or "user").strip().lower() or "user"
            source_text = str(row.get("source_text") or row.get("feature") or "delegated_task").strip()
            job = await _dispatch_actions_to_device(did, username=username, actions=actions, source_text=source_text)
            _mark_delegated_task(
                task_id=row.get("task_id"),
                status_value="delegated",
                extra={
                    "device_id": did,
                    "last_job_id": job.get("job_id"),
                    "attempts": int(row.get("attempts") or 0) + 1,
                    "dispatched_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            _mark_delegated_task(task_id=row.get("task_id"), status_value="queued_for_agent", extra={"last_error": str(e)[:300]})


async def _resume_pending_permission_delegations_for_device(device_id: str) -> None:
    did = _normalize_device_id(device_id)
    if not did:
        return

    col = _delegated_tasks_collection()
    if col is None:
        return

    owner = _get_device_owner(did)
    query = {
        "status": "pending_permission",
        "$or": [
            {"device_id": did},
            {"device_id": None, "username": owner},
        ],
    }

    rows = []
    try:
        rows = list(col.find(query, {"_id": 0}).sort("updated_at", 1).limit(30))
    except Exception:
        rows = []

    for row in rows:
        try:
            actions = row.get("actions") or []
            if not isinstance(actions, list) or not actions:
                _mark_delegated_task(task_id=row.get("task_id"), status_value="failed", extra={"error": "missing_actions"})
                continue

            username = str(row.get("username") or "user").strip().lower() or "user"
            source_text = str(row.get("source_text") or row.get("feature") or "delegated_task").strip()
            job = await _dispatch_actions_to_device(did, username=username, actions=actions, source_text=source_text)
            _mark_delegated_task(
                task_id=row.get("task_id"),
                status_value="delegated",
                extra={
                    "device_id": did,
                    "last_job_id": job.get("job_id"),
                    "attempts": int(row.get("attempts") or 0) + 1,
                    "dispatched_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            _mark_delegated_task(task_id=row.get("task_id"), status_value="pending_permission", extra={"last_error": str(e)[:300]})


async def _delegate_or_queue_cloud_action(
    *,
    session_id: str | None,
    feature: str,
    actions: list[dict[str, Any]],
    source_text: str,
    require_admin: bool = False,
    await_timeout_s: float = 2.5,
) -> dict[str, Any]:
    _require_pc_agent_enabled()
    principal = _require_admin_session(session_id) if require_admin else _require_authenticated_session(session_id)
    username = str((principal or {}).get("username") or "").strip().lower()
    role = str((principal or {}).get("role") or "user").strip().lower()

    did = _get_owner_device_id(username)
    if (not did) and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
        did = DEFAULT_DEVICE_ID
    if (not did) and role == "admin":
        did = DEFAULT_DEVICE_ID

    if not did:
        task = _queue_delegated_task(
            username=username,
            role=role,
            device_id=None,
            feature=feature,
            source_text=source_text,
            actions=actions,
            status_value="awaiting_agent",
            reason="missing_device_assignment",
        )
        return {
            "status": "awaiting_agent",
            "mode": "cloud",
            "feature": feature,
            "task": task,
            "message": "No device is assigned yet. Task is waiting for agent assignment.",
        }

    if not await device_hub.is_connected(did):
        task = _queue_delegated_task(
            username=username,
            role=role,
            device_id=did,
            feature=feature,
            source_text=source_text,
            actions=actions,
            status_value="queued_for_agent",
            reason="agent_offline",
        )
        return {
            "status": "queued_for_agent",
            "mode": "cloud",
            "feature": feature,
            "device_id": did,
            "task": task,
            "message": "PC agent is offline. Task queued for manual resume after reconnect.",
        }

    job = await _dispatch_actions_to_device(did, username=username or "user", actions=actions, source_text=source_text)
    job_id = str((job or {}).get("job_id") or "").strip()

    payload = await _await_job_result(job_id, timeout_s=await_timeout_s)
    if not payload:
        return {
            "status": "delegated",
            "mode": "cloud",
            "feature": feature,
            "device_id": did,
            "job": job,
            "message": "Delegated to PC agent and awaiting completion.",
        }

    results = (payload or {}).get("results") or []
    first = results[0] if isinstance(results, list) and results else {}
    raw_first_status = str((first or {}).get("status") or "").strip().lower()
    if raw_first_status in {"success", "ok"}:
        first_status = "completed"
    elif raw_first_status in {"error", "forbidden"}:
        first_status = "failed"
    else:
        first_status = _normalize_flow_status(raw_first_status, default="completed")

    return {
        "status": first_status,
        "mode": "cloud",
        "feature": feature,
        "device_id": did,
        "job": job,
        "agent_result": payload,
    }


def _delegated_first_result(delegated: dict[str, Any]) -> dict[str, Any] | None:
    payload = (delegated.get("agent_result") or {}).get("results") or []
    first = payload[0] if isinstance(payload, list) and payload else None
    if isinstance(first, dict):
        return _to_json_safe(dict(first))
    return None


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

device_hub = (
    DeviceHub(shared_secret=AGENT_SHARED_SECRET, broker=_BROKER, instance_id=jarvis_settings.instance_id)
    if PC_AGENT_ENABLED
    else _DisabledDeviceHub()
)
# Local/dev convenience: agent token issuance requires JARVIS_JWT_SECRET.
# In cloud mode this must be explicitly configured; in local mode we can
# generate a per-run secret so PC agent pairing works out of the box.
if (not CLOUD_MODE) and (not (os.getenv("JARVIS_JWT_SECRET") or "").strip()):
    try:
        os.environ["JARVIS_JWT_SECRET"] = secrets.token_urlsafe(48)
    except Exception:
        os.environ["JARVIS_JWT_SECRET"] = os.urandom(48).hex()

auth_tokens = AuthTokens()

PUBLIC_SERVER_URL = (env.get_str("JARVIS_PUBLIC_SERVER_URL", "") or "").strip().rstrip("/")
AGENT_TOKEN_TTL_SECONDS = env.get_int("JARVIS_AGENT_TOKEN_TTL_SECONDS", 2592000)  # 30d


def _effective_server_url(request: Request | None) -> str:
    """Best-effort server base URL for agent bootstrap payloads.

    Priority:
    1) Explicit JARVIS_PUBLIC_SERVER_URL (always wins)
    2) In local/non-cloud mode, derive from the current request host/scheme
    3) In cloud mode, derive from forwarded headers/request as fallback
    """
    if PUBLIC_SERVER_URL:
        return PUBLIC_SERVER_URL

    try:
        if request is not None:
            xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            xf_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
            host = xf_host or (request.headers.get("host") or "").strip()
            scheme = xf_proto or (request.url.scheme if request.url else "http")
            if host:
                return f"{scheme}://{host}".rstrip("/")
            if request.base_url:
                return str(request.base_url).rstrip("/")
    except Exception:
        pass

    if CLOUD_MODE:
        return "https://jarvis-cloud-assistant.onrender.com"
    return "http://127.0.0.1:18001"


def _is_local_request(request: Request | None) -> bool:
    try:
        if request is None:
            return False
        host = (request.headers.get("host") or "").split(",")[0].strip()
        host = host.split(":")[0].strip().lower()
        return host in {"localhost", "127.0.0.1", "::1"}
    except Exception:
        return False

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

def _cloud_feature_disabled(feature: str, *, required_by: str = "admin", delegated: bool = True):
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "message": f"{feature} is restricted in cloud deployments.",
            "mode": "cloud",
            "status": "restricted",
            "details_level": "sanitized",
            "restriction": {
                "feature": feature,
                "required_by": required_by,
                "delegated": bool(delegated),
                "delegated_to": "pc_agent" if delegated else None,
                "guidance": [
                    "Use a connected PC agent for local/device execution.",
                    "If this is an admin-only operation, request admin approval.",
                ],
            },
        },
    )


def _build_cloud_safe_system_info() -> dict:
    """Return a cloud-safe, schema-stable system info payload.

    This intentionally mirrors key fields used by the local dashboard while
    replacing local-only internals with sanitized placeholders.
    """
    db_connected = (getattr(database, "client", None) is not None) and (getattr(database, "db", None) is not None)
    return {
        "status": "success",
        "mode": "cloud",
        "details_level": "sanitized",
        "version": "1.0",
        "voice_enabled": bool(VOICE_ONLY_MODE),
        "pc_agent_enabled": bool(PC_AGENT_ENABLED),
        "database_connected": bool(db_connected),
        "redis_connected": bool(_BROKER is not None),
        "llm_configured": bool(getattr(rd, "PRIMARY_ENDPOINT", "") or getattr(rd, "PRIMARY_API_KEY", "") or getattr(rd, "BACKUP_API_KEY", "")),
        "uptime_seconds": int(time.time() - START_TS),
        "task_queue_enabled": bool(ENABLE_SCHEDULER and SCHEDULER_AVAILABLE),
        "connected_agents": None,
        "local_system_details": "restricted",
        # Preserve dashboard-compatible keys.
        "cpu_percent": None,
        "memory_percent": None,
        "disk_percent": None,
        "boot_time": None,
        "cpu_count": None,
        "process_count": None,
        "restriction": {
            "feature": "System operations",
            "required_by": "admin",
            "delegated": True,
            "delegated_to": "pc_agent",
            "guidance": [
                "Connect a PC agent to access local runtime metrics.",
                "Cloud mode returns sanitized system information by design.",
            ],
        },
    }


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
                query = str(r.get("query") or "").strip()
                for item in (r.get("results") or [])[:5]:
                    if not isinstance(item, dict):
                        continue
                    if not _web_result_is_relevant(query, item):
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


def _web_query_terms(query: str) -> set[str]:
    raw = (query or "").strip().lower()
    toks = re.findall(r"[a-z0-9]+", raw)
    if not toks:
        return set()
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "is", "are",
        "what", "how", "when", "where", "who", "why", "this", "that", "these", "those", "please",
        "search", "find", "look", "up", "online", "internet", "research", "analysis", "summary",
    }
    return {t for t in toks if t and t not in stop and len(t) >= 3}


def _web_result_is_relevant(query: str, item: dict) -> bool:
    terms = _web_query_terms(query)
    if not terms:
        return True

    title = str(item.get("title") or "").lower()
    snippet = str(item.get("snippet") or "").lower()
    url = str(item.get("url") or "").lower()
    blob = f"{title} {snippet} {url}"

    hit_count = sum(1 for t in terms if t in blob)
    if hit_count >= 2:
        return True

    # For short precise queries, a single strong hit can be acceptable.
    if len(terms) <= 3 and hit_count >= 1:
        return True

    return False



def _require_device_owner(username: str | None):
    if DEVICE_OWNER_USERNAME and (username or "").lower() != DEVICE_OWNER_USERNAME.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not permitted to control the connected device.",
        )


def _device_registry_collection():
    """Collection storing device metadata keyed by device_id.

    Ownership mapping is handled by user_device_links to allow many users to
    share one device_id when needed.
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
        _DEVICE_REGISTRY_INDEX_READY = True
    return col


def _user_device_links_collection():
    """Collection mapping owner_username -> device_id.

    Model:
    - One user can be mapped to at most one device_id (unique owner_username).
    - One device_id can be mapped to many users (non-unique device_id).
    """
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["user_device_links"]
    global _USER_DEVICE_LINK_INDEX_READY
    if not _USER_DEVICE_LINK_INDEX_READY:
        try:
            col.create_index("owner_username", unique=True)
        except Exception:
            pass
        try:
            col.create_index("device_id")
        except Exception:
            pass
        _USER_DEVICE_LINK_INDEX_READY = True
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


def _requirements_audit_collection():
    """Collection for permission/access requirement audit events."""
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["requirements_audit"]
    global _REQUIREMENTS_AUDIT_INDEX_READY
    if not _REQUIREMENTS_AUDIT_INDEX_READY:
        try:
            col.create_index("ts")
        except Exception:
            pass
        try:
            col.create_index([("user_id", 1), ("device_id", 1), ("ts", -1)])
        except Exception:
            pass
        try:
            col.create_index([("status", 1), ("requirement_type", 1), ("ts", -1)])
        except Exception:
            pass
        _REQUIREMENTS_AUDIT_INDEX_READY = True
    return col


def _log_requirement_event(
    *,
    user_id: str | None,
    device_id: str | None,
    requested_action: str,
    requirement_type: str,
    target: str,
    permission_or_scope: str | None = None,
    status: str = "pending",
    actor_role: str | None = None,
    source: str | None = None,
    details: dict | None = None,
):
    """Best-effort requirement audit event logger."""
    try:
        col = _requirements_audit_collection()
        if col is None:
            return
        now = datetime.now(timezone.utc)
        payload = {
            "ts": now.isoformat(),
            "user_id": (user_id or "").strip().lower() or None,
            "device_id": _normalize_device_id(device_id) or None,
            "requested_action": (requested_action or "").strip() or "unknown",
            "requirement_type": (requirement_type or "").strip() or "unknown",
            "target": (target or "").strip() or "unknown",
            "permission_or_scope": (permission_or_scope or "").strip() or None,
            "status": (status or "pending").strip().lower(),
            "actor_role": (actor_role or "").strip().lower() or None,
            "source": (source or "").strip() or None,
            "details": details if isinstance(details, dict) else {},
            "created_at": now,
        }
        col.insert_one(payload)
    except Exception:
        return


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
    """Return the device_id assigned to the given user (or None)."""
    owner = (owner_username or "").strip().lower()
    if not owner:
        return None

    now = time.time()
    cached = _OWNER_TO_DEVICE_CACHE.get(owner)
    if cached and (now - cached[0]) < _DEVICE_LOOKUP_CACHE_TTL_S:
        return cached[1]

    col = _user_device_links_collection()
    if col is None:
        # Legacy fallback for deployments that only have device_registry ownership data.
        legacy = _device_registry_collection()
        if legacy is None:
            _OWNER_TO_DEVICE_CACHE[owner] = (now, None)
            return None
        doc = legacy.find_one({"owner_username": owner}, {"_id": 0, "device_id": 1})
    else:
        doc = col.find_one({"owner_username": owner}, {"_id": 0, "device_id": 1})
    did = (doc or {}).get("device_id")
    did = _normalize_device_id(did) or None
    _OWNER_TO_DEVICE_CACHE[owner] = (now, did)
    if did:
        _DEVICE_TO_OWNER_CACHE[did] = (now, owner)
    return did


def _get_device_owner(device_id: str | None) -> str | None:
    """Return one owner_username for a device_id (or None).

    In shared-device mode, this returns the most recently updated owner.
    """
    did = _normalize_device_id(device_id)
    if not did:
        return None

    now = time.time()
    cached = _DEVICE_TO_OWNER_CACHE.get(did)
    if cached and (now - cached[0]) < _DEVICE_LOOKUP_CACHE_TTL_S:
        return cached[1]

    col = _user_device_links_collection()
    if col is None:
        legacy = _device_registry_collection()
        if legacy is None:
            _DEVICE_TO_OWNER_CACHE[did] = (now, None)
            return None
        doc = legacy.find_one({"device_id": did}, {"_id": 0, "owner_username": 1})
    else:
        doc = col.find_one(
            {"device_id": did},
            {"_id": 0, "owner_username": 1},
            sort=[("updated_at", -1), ("created_at", -1)],
        )
    owner = (doc or {}).get("owner_username")
    owner = (owner or "").strip().lower() or None
    _DEVICE_TO_OWNER_CACHE[did] = (now, owner)
    if owner:
        _OWNER_TO_DEVICE_CACHE[owner] = (now, did)
    return owner


def _set_device_owner(device_id: str, owner_username: str | None, updated_by: str | None = None):
    """Assign/unassign user mapping for a device_id.

    If owner_username is provided, map that user -> device.
    If owner_username is None, clear all user links pointing to device_id.
    """
    col = _user_device_links_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    did = _normalize_device_id(device_id)
    owner = (owner_username or "").strip().lower() or None
    updater = (updated_by or "").strip().lower() or None

    now = datetime.utcnow()
    if owner:
        col.update_one(
            {"owner_username": owner},
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
        _OWNER_TO_DEVICE_CACHE[owner] = (time.time(), did)
        _DEVICE_TO_OWNER_CACHE[did] = (time.time(), owner)
    else:
        col.delete_many({"device_id": did})
        _DEVICE_TO_OWNER_CACHE.pop(did, None)


def _clear_owner_device(owner_username: str | None):
    owner = (owner_username or "").strip().lower()
    if not owner:
        return
    col = _user_device_links_collection()
    if col is None:
        return
    col.delete_one({"owner_username": owner})
    _OWNER_TO_DEVICE_CACHE.pop(owner, None)


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


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            out[str(k)] = _to_json_safe(v)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return str(value)


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


async def _run_device_cycle_evaluation(goal: str, user_id: str, device_id: str, execution_results: list[dict]) -> None:
    """
    Run the Goal → Plan → Execute → Evaluate → Improve cycle for device actions.
    
    This runs in the background after the device executes actions, enabling:
    - Automatic evaluation of goal achievement
    - Improvement recommendations
    - Learning feedback persistence for future refinements
    """
    try:
        if not goal or not user_id or not execution_results:
            return
        
        # Reconstruct a minimal planned_response from execution results.
        # We don't have the original actions list, but we can infer it from the results.
        planned_response = {
            "actions": [{"type": r.get("action_type")} for r in execution_results if isinstance(r, dict)]
        }
        
        # Invoke the full cycle: Evaluate phase + Improve phase
        cycle_result = llm.process_goal_plan_execute_cycle(
            goal=goal,
            user_id=user_id,
            planned_response=planned_response,
            execution_results=execution_results,
        )
        
        logger = __import__('logging').getLogger(__name__)
        logger.info(
            "[CYCLE RESULT] goal=%s, user=%s, device=%s, "
            "achieved=%s, success_rate=%s, retry_strategy=%s",
            goal[:50],
            user_id,
            device_id,
            cycle_result.get("evaluation", {}).get("goal_achieved"),
            cycle_result.get("evaluation", {}).get("success_rate"),
            cycle_result.get("improvement_feedback", {}).get("retry_strategy"),
        )
        
        # TODO: Implement auto-retry if retry_strategy suggests it
        # retry_strategy = cycle_result.get("improvement_feedback", {}).get("retry_strategy")
        # if retry_strategy in {"RETRY_FAILED_ONLY", "RETRY_WITH_DELAYS"}:
        #     improved_plan = cycle_result.get("improvement_feedback", {}).get("improved_plan")
        #     # Requeue with improved plan + adjusted delays
        
    except Exception as e:
        logger = __import__('logging').getLogger(__name__)
        logger.error("[CYCLE EVAL ERROR] %s", e)


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

        # Manual mode: do not auto-resume queued/pending delegated tasks on reconnect.

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

                try:
                    _remember_job_result(payload if isinstance(payload, dict) else {})
                    jid = str((payload or {}).get("job_id") or "").strip()
                    if jid:
                        results = (payload or {}).get("results") or []
                        first = results[0] if isinstance(results, list) and results else {}
                        raw_status = str((first or {}).get("status") or "completed").strip().lower()
                        if raw_status in {"success", "ok"}:
                            flow_status = "completed"
                        elif raw_status in {"error", "forbidden"}:
                            flow_status = "failed"
                        else:
                            flow_status = _normalize_flow_status(raw_status, default="completed")
                        _mark_delegated_task(
                            job_id=jid,
                            status_value=flow_status,
                            extra={
                                "completed_at": datetime.now(timezone.utc).isoformat(),
                                "result_preview": _truncate_notification_payload(results),
                            },
                        )
                except Exception:
                    pass

                # Publish to the user who initiated this job (best-effort).
                try:
                    jid = (payload or {}).get("job_id")
                    user, did, source_text = _pop_job_owner(jid)
                    execution_results = (payload or {}).get("results") or []
                    if user:
                        await notification_hub.publish(
                            user,
                            {
                                "type": "device_job_result",
                                "job_id": str(jid or ""),
                                "device_id": did or device_id,
                                "source_text": source_text,
                                "results": _truncate_notification_payload(execution_results),
                                "received_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        
                        # CONTINUOUS IMPROVEMENT: Run cycle evaluation & improvement in background.
                        # This enables automatic refinement for each device action across all scenarios.
                        asyncio.create_task(
                            _run_device_cycle_evaluation(
                                goal=source_text,
                                user_id=user,
                                device_id=did or device_id,
                                execution_results=execution_results,
                            )
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
async def agent_config(req: AgentConfigRequest, background_tasks: BackgroundTasks, request: Request):
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

    server_url = _effective_server_url(request)

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
                        "server_url": server_url,
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
    ws_url = ("wss://" + server_url[len("https://"):] + "/ws/agent") if server_url.startswith("https://") else ("ws://" + server_url[len("http://"):] + "/ws/agent")
    payload = {
        "status": "success",
        "device_id": did,
        "server_url": server_url,
        "ws_url": ws_url,
        "agent_token": token,
        "expires_in_seconds": max(300, AGENT_TOKEN_TTL_SECONDS),
    }

    # Only expose the shared secret for local/dev setups.
    # In cloud mode, avoid leaking a global secret unless explicitly enabled.
    allow_secret = bool(AGENT_SHARED_SECRET) and (not CLOUD_MODE or EXPOSE_AGENT_SHARED_SECRET or _is_local_request(request))
    if allow_secret:
        # If explicitly enabled in cloud mode, restrict to admins (unless it's a local request).
        if (not CLOUD_MODE) or _is_local_request(request) or (role == "admin"):
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

    Shared-device mode: multiple users may point to the same device_id.
    """
    p = _require_authenticated_session(req.session_id)
    user_id = (p.get("username") or "").strip().lower()
    did = _validate_device_id_or_400(req.device_id)

    # Update this user's mapping; device_id may be shared across users.
    prev = _get_owner_device_id(user_id)
    if prev and prev != did:
        _clear_owner_device(user_id)

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
        prev = _get_owner_device_id(user_id)
        if prev and prev != did:
            _clear_owner_device(user_id)
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

    # Shared-device mode: any connected device can be mapped to this user.
    candidates = [str(d or "").strip().lower() for d in agents_by_id.keys() if str(d or "").strip()]

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
        raise HTTPException(status_code=409, detail="No PC agent is connected. Start pc_agent.py on your PC and try again.")

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

    # Shared-device mode: many users can map to the same device_id.
    if owner:
        prev = _get_owner_device_id(owner)
        if prev and prev != did:
            _clear_owner_device(owner)
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
    requested_action = ", ".join([
        str((a or {}).get("type") or "").strip()
        for a in (req.actions or []) if isinstance(a, dict)
    ]) or "device_action"

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
            _log_requirement_event(
                user_id=username,
                device_id=req.device_id,
                requested_action=requested_action,
                requirement_type="admin_policy",
                target="Device Registry Policy",
                status="denied",
                actor_role=role,
                source="device_dispatch",
                details={"message": "Users cannot dispatch to another device"},
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "Users cannot dispatch to another device",
                    "requirement": {
                        "requirement_type": "admin_policy",
                        "target": "Device Registry Policy",
                        "target_application": "Jarvis Management Console",
                        "permission_or_scope": "device_dispatch_cross_owner",
                        "required_by": "admin",
                        "why": "Cross-device dispatch is controlled by administrator policy and ownership mapping.",
                        "guidance": [
                            "Ask an administrator to assign this device to your account or perform the action for you.",
                            "Admin path: Management Console -> Device Registry -> Assign owner.",
                            "After policy is updated, Jarvis can resume the action.",
                        ],
                        "resume_automatically": True,
                    },
                },
            )
        did = _get_owner_device_id(username)
        if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
            did = DEFAULT_DEVICE_ID
        if not did:
            _log_requirement_event(
                user_id=username,
                device_id=None,
                requested_action=requested_action,
                requirement_type="missing_user_information",
                target="Device Selection",
                permission_or_scope="device_id",
                status="pending",
                actor_role=role,
                source="device_dispatch",
                details={"message": "No device assigned to this user"},
            )
            queued = _queue_delegated_task(
                username=username,
                role=role,
                device_id=None,
                feature="device_dispatch",
                source_text=req.source_text or requested_action,
                actions=[a for a in (req.actions or []) if isinstance(a, dict)],
                status_value="awaiting_agent",
                reason="missing_device_assignment",
            )
            return {
                "status": "awaiting_agent",
                "mode": "cloud",
                "task": queued,
                "message": "No device assigned yet. Action queued and will resume after device configuration.",
                "action": "configure_pc",
                "hint": "Configure a device via /api/user/device/configure (auto-pick) or /api/user/device/set (explicit device_id).",
            }

    if not await device_hub.is_connected(did):
        _log_requirement_event(
            user_id=username,
            device_id=did,
            requested_action=requested_action,
            requirement_type="third_party_app_permission",
            target="PC Agent",
            permission_or_scope="agent_connection",
            status="pending",
            actor_role=role,
            source="device_dispatch",
            details={"message": "Device agent is not connected"},
        )
        queued = _queue_delegated_task(
            username=username,
            role=role,
            device_id=did,
            feature="device_dispatch",
            source_text=req.source_text or requested_action,
            actions=[a for a in (req.actions or []) if isinstance(a, dict)],
            status_value="queued_for_agent",
            reason="agent_offline",
        )
        return {
            "status": "queued_for_agent",
            "mode": "cloud",
            "device_id": did,
            "task": queued,
            "message": "Device agent is offline. Action queued for manual resume after reconnect.",
            "hint": "Start pc_agent.py on the target PC and ensure JARVIS_SERVER_URL and JARVIS_AGENT_SHARED_SECRET match the server.",
        }

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
        _log_requirement_event(
            user_id=username,
            device_id=did,
            requested_action=requested_action,
            requirement_type="third_party_app_permission",
            target="PC Agent Capabilities",
            permission_or_scope="capabilities_payload",
            status="pending",
            actor_role=role,
            source="device_dispatch",
            details={"message": "Agent is connected but did not report capabilities"},
        )
        queued = _queue_delegated_task(
            username=username,
            role=role,
            device_id=did,
            feature="device_dispatch",
            source_text=req.source_text or requested_action,
            actions=[a for a in (req.actions or []) if isinstance(a, dict)],
            status_value="pending_permission",
            reason="missing_agent_capabilities",
        )
        return {
            "status": "pending_permission",
            "mode": "cloud",
            "device_id": did,
            "task": queued,
            "message": "Agent is connected but missing capability metadata.",
            "action": "update_pc_agent",
            "hint": "Update pc_agent.py and restart the agent so it reports capabilities on connect.",
        }

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
            _log_requirement_event(
                user_id=username,
                device_id=did,
                requested_action=at or requested_action,
                requirement_type="assistant_permission",
                target="PC Agent Runtime Permission",
                permission_or_scope=key,
                status="pending",
                actor_role=role,
                source="device_dispatch",
                details={"env_var": env_name, "action_type": at},
            )
            queued = _queue_delegated_task(
                username=username,
                role=role,
                device_id=did,
                feature="device_dispatch",
                source_text=req.source_text or requested_action,
                actions=[a for a in (req.actions or []) if isinstance(a, dict)],
                status_value="pending_permission",
                reason=f"missing_{key}",
            )
            return {
                "status": "pending_permission",
                "mode": "cloud",
                "device_id": did,
                "task": queued,
                "message": f"No permission: '{at}' is disabled on your PC agent.",
                "action_type": at,
                "required_capability": key,
                "env_var": env_name,
                "suggestion": f"Enable {env_name}=true on the PC agent.",
            }

    job = await _dispatch_actions_to_device(did, username=username or "user", actions=actions, source_text=req.source_text or "")
    return {"status": "delegated", "job": job, "device_id": did, "request_id": f"disp_{uuid.uuid4().hex[:12]}"}


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


class RequirementAuditLogRequest(BaseModel):
    session_id: str
    requested_action: str
    requirement_type: str
    target: str
    permission_or_scope: str | None = None
    status: str = "pending"
    device_id: str | None = None
    source: str | None = "frontend"
    details: Dict[str, Any] | None = None


@app.post("/api/requirements/audit/log")
async def requirements_audit_log(req: RequirementAuditLogRequest):
    principal = _require_authenticated_session(req.session_id)
    username = (principal.get("username") or "").strip().lower() or None
    role = (principal.get("role") or "user").strip().lower()

    _log_requirement_event(
        user_id=username,
        device_id=req.device_id,
        requested_action=req.requested_action,
        requirement_type=req.requirement_type,
        target=req.target,
        permission_or_scope=req.permission_or_scope,
        status=req.status,
        actor_role=role,
        source=req.source,
        details=req.details,
    )
    return {"status": "success"}


@app.get("/api/admin/requirements/audit")
async def admin_requirements_audit(session_id: str, limit: int = 100):
    _require_admin_session(session_id)
    col = _requirements_audit_collection()
    if col is None:
        return {"status": "error", "message": "Database unavailable", "events": []}

    lim = max(1, min(int(limit), 500))
    rows = list(col.find({}, {"_id": 0}).sort("created_at", -1).limit(lim))
    return {"status": "success", "events": rows, "count": len(rows)}


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
        _log_requirement_event(
            user_id=username,
            device_id=did,
            requested_action="agent_set_permissions",
            requirement_type="third_party_app_permission",
            target="PC Agent",
            permission_or_scope="agent_connection",
            status="pending",
            actor_role=role,
            source="device_permissions_grant",
            details={"permissions": normalized},
        )
        return {"status": "saved", "offline": True, "device_id": did, "permissions": normalized}

    job = await _dispatch_actions_to_device(
        did,
        username=username or "user",
        actions=[{"type": "agent_set_permissions", "permissions": normalized}],
        source_text="permission_grant",
    )
    for key, enabled in normalized.items():
        _log_requirement_event(
            user_id=username,
            device_id=did,
            requested_action="agent_set_permissions",
            requirement_type="assistant_permission",
            target="PC Agent Runtime Permission",
            permission_or_scope=key,
            status="granted" if bool(enabled) else "denied",
            actor_role=role,
            source="device_permissions_grant",
            details={"enabled": bool(enabled)},
        )

    # Manual mode: permission grants do not auto-resume queued tasks.

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
    "https://frontend.onrender.com",
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
module_cycle_service = ModuleUpdateCycleService(executor=executor, self_add_feature=self_add_feature)
autonomy_runtime = AutonomyRuntime(
    device_hub=device_hub,
    enabled=bool(getattr(rd, "AUTONOMY_ENABLED", True)),
    poll_interval_seconds=int(getattr(rd, "AUTONOMY_POLL_INTERVAL_SECONDS", 20)),
)


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
            kept = 0
            for idx, item in enumerate(results[:3], start=1):
                if not isinstance(item, dict):
                    continue
                if not _web_result_is_relevant(query, item):
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
                    kept += 1
                    lines.append(f"{kept}) " + " | ".join(parts))
            if len(lines) > 1 and kept > 0:
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
            query = str(r.get("query") or "").strip()
            if isinstance(results, list) and any(_web_result_is_relevant(query, i) for i in results if isinstance(i, dict)):
                return True
            try:
                if int(r.get("results_count") or 0) > 0 and isinstance(results, list):
                    # If we got count>0 but none are relevant, keep found=False to avoid random sources.
                    if any(_web_result_is_relevant(query, i) for i in results if isinstance(i, dict)):
                        return True
                    return False
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
            urls = re.findall(r"https?://\S+", web_context or "")
            urls = [u.rstrip(").,;") for u in urls]
            urls = [u for i, u in enumerate(urls) if u and u not in urls[:i]]
            if not has_url:
                if urls:
                    txt = (txt + "\n\nSource URLs:\n" + "\n".join([f"{i+1}. {u}" for i, u in enumerate(urls[:2])])).strip()

            if bool(getattr(rd, "GLOBAL_FACTUAL_INCLUDE_CONFIDENCE", False)):
                # Simple deterministic confidence estimate based on number of unique source URLs.
                # This is confidence in available evidence quality, not absolute truth.
                source_count = len(urls)
                if source_count >= 3:
                    confidence = "High"
                elif source_count == 2:
                    confidence = "Medium"
                elif source_count == 1:
                    confidence = "Low"
                else:
                    confidence = "Low"
                if "Confidence:" not in txt:
                    txt = (txt + f"\n\nConfidence: {confidence} (based on {source_count} source URL(s))").strip()

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


class GoalCreateRequest(BaseModel):
    goal: str
    session_id: str | None = None
    priority: int = 5


class GoalGraphNodePatch(BaseModel):
    task_id: str
    title: str | None = None
    description: str | None = None
    dependencies: List[str] | None = None
    status: str | None = None


class GoalGraphMoveRequest(BaseModel):
    task_id: str
    to_index: int


class GoalGraphUpdateRequest(BaseModel):
    session_id: str | None = None
    nodes: List[GoalGraphNodePatch] | None = None
    move: GoalGraphMoveRequest | None = None
    rerun_failed: bool = False


class GoalCancelRequest(BaseModel):
    session_id: str | None = None
    reason: str | None = None


class AutonomyControlRequest(BaseModel):
    session_id: str | None = None
    action: str


class AdminAutoUpdateRunRequest(BaseModel):
    session_id: str
    description: str
    scopes: List[str] | None = None
    auto_install_deps: bool | None = None
    dry_run: bool = False


@app.post("/api/autonomy/goals")
async def create_autonomy_goal(req: GoalCreateRequest):
    principal = _get_principal(req.session_id) if req.session_id else {"username": "system", "role": "anonymous"}
    if CLOUD_MODE and not req.session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if CLOUD_MODE:
        principal = _require_authenticated_session(req.session_id)

    owner = str(principal.get("username") or "system")
    goal_id = autonomy_runtime.goals.create_goal(
        goal=req.goal,
        owner=owner,
        priority=req.priority,
        metadata={"origin": "api"},
    )
    return {"status": "ok", "goal_id": goal_id}


@app.get("/api/autonomy/goals")
async def list_autonomy_goals(statuses: str = "pending,running,failed,completed", limit: int = 25, session_id: str | None = None):
    try:
        principal = None
        if CLOUD_MODE:
            principal = _require_authenticated_session(session_id)

        status_list = [s.strip() for s in statuses.split(",") if s.strip()]
        goals = autonomy_runtime.goals.list_goals(statuses=status_list, limit=max(1, min(limit, 100)))

        if CLOUD_MODE and principal and str((principal or {}).get("role") or "").strip().lower() != "admin":
            username = str((principal or {}).get("username") or "").strip().lower()
            goals = [g for g in goals if str((g or {}).get("owner") or "").strip().lower() == username]

        return {"status": "ok", "goals": goals}
    except Exception as e:
        return {"status": "error", "message": str(e), "goals": []}


@app.post("/api/autonomy/goals/{goal_id}/cancel")
async def cancel_autonomy_goal(goal_id: str, req: GoalCancelRequest):
    principal = _get_principal(req.session_id) if req.session_id else {"username": "system", "role": "anonymous"}
    if CLOUD_MODE:
        principal = _require_authenticated_session(req.session_id)

    goal = autonomy_runtime.goals.get_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")

    role = str((principal or {}).get("role") or "user").strip().lower()
    username = str((principal or {}).get("username") or "system").strip()
    owner = str((goal or {}).get("owner") or "").strip().lower()
    if role != "admin" and owner and owner != username.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Goal ownership required")

    previous_status = str((goal or {}).get("status") or "").strip().lower()
    if previous_status in {"completed", "failed", "cancelled"}:
        return {
            "status": "ok",
            "goal_id": goal_id,
            "goal_status": previous_status or "unknown",
            "message": "Goal already in a terminal state",
        }

    autonomy_runtime.goals.update_goal_status(goal_id, "cancelled", last_error="cancelled_by_user")
    autonomy_runtime.goals.append_report(
        goal_id,
        {
            "type": "cancelled",
            "requested_by": username or "system",
            "reason": str(req.reason or "user_requested")[:200],
            "previous_status": previous_status or "unknown",
        },
    )
    return {
        "status": "ok",
        "goal_id": goal_id,
        "goal_status": "cancelled",
        "previous_status": previous_status or "unknown",
    }


@app.get("/api/autonomy/status")
async def autonomy_status(session_id: str | None = None):
    try:
        if CLOUD_MODE:
            _require_authenticated_session(session_id)

        delegated_rows = []
        if session_id:
            try:
                principal = _require_authenticated_session(session_id)
                delegated_rows = _list_delegated_tasks_for_principal(principal, limit=120)
            except Exception:
                delegated_rows = []

        return {
            "status": "ok",
            "runtime": {
                "enabled": bool(getattr(rd, "AUTONOMY_ENABLED", True)),
                "poll_interval_seconds": int(getattr(rd, "AUTONOMY_POLL_INTERVAL_SECONDS", 20)),
                "control": autonomy_runtime.control_state(),
            },
            "health": autonomy_runtime._health_check(),
            "tools": autonomy_runtime.tools.list_tools(),
            "delegated_summary": _delegated_status_counts(delegated_rows),
        }
    except Exception as e:
        return {
            "status": "error",
            "runtime": {
                "enabled": bool(getattr(rd, "AUTONOMY_ENABLED", True)),
                "poll_interval_seconds": int(getattr(rd, "AUTONOMY_POLL_INTERVAL_SECONDS", 20)),
                "control": autonomy_runtime.control_state(),
            },
            "health": {"status": "error", "message": str(e)},
            "tools": [],
            "message": str(e),
            "delegated_summary": _delegated_status_counts([]),
        }


def _extract_graph_from_goal(goal: dict | None) -> dict:
    reports = (goal or {}).get("reports") if isinstance(goal, dict) else None
    if not isinstance(reports, list):
        return {"goal": str((goal or {}).get("goal") or ""), "nodes": []}
    for r in reversed(reports):
        if isinstance(r, dict) and isinstance(r.get("graph"), dict):
            graph = dict(r.get("graph") or {})
            if not isinstance(graph.get("nodes"), list):
                graph["nodes"] = []
            if "goal" not in graph:
                graph["goal"] = str((goal or {}).get("goal") or "")
            return graph
    return {"goal": str((goal or {}).get("goal") or ""), "nodes": []}


@app.patch("/api/autonomy/goals/{goal_id}/graph")
async def update_autonomy_goal_graph(goal_id: str, req: GoalGraphUpdateRequest):
    principal = _get_principal(req.session_id) if req.session_id else {"username": "system", "role": "anonymous"}
    if CLOUD_MODE:
        principal = _require_authenticated_session(req.session_id)

    goal = autonomy_runtime.goals.get_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Goal not found")

    owner = str(goal.get("owner") or "").strip().lower()
    username = str(principal.get("username") or "").strip().lower()
    is_admin = str(principal.get("role") or "").strip().lower() == "admin"
    if owner and username and owner != username and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to edit this goal graph")

    graph = _extract_graph_from_goal(goal)
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    if not isinstance(nodes, list):
        nodes = []

    by_id: dict[str, dict] = {}
    for n in nodes:
        if isinstance(n, dict):
            tid = str(n.get("task_id") or "").strip()
            if tid:
                by_id[tid] = n

    for patch in (req.nodes or []):
        tid = str(patch.task_id or "").strip()
        if not tid:
            continue
        node = by_id.get(tid)
        if not node:
            continue
        if patch.title is not None:
            node["title"] = str(patch.title).strip() or node.get("title")
        if patch.description is not None:
            node["description"] = str(patch.description).strip() or node.get("description")
        if patch.status is not None:
            node["status"] = str(patch.status).strip().lower() or node.get("status")
        if patch.dependencies is not None:
            deps = [str(d).strip() for d in patch.dependencies if str(d).strip() and str(d).strip() != tid]
            deps = [d for d in deps if d in by_id]
            node["dependencies"] = deps

    if req.move is not None and nodes:
        move_tid = str(req.move.task_id or "").strip()
        target_idx = max(0, min(int(req.move.to_index), len(nodes) - 1))
        cur_idx = next((i for i, n in enumerate(nodes) if str((n or {}).get("task_id") or "") == move_tid), None)
        if cur_idx is not None:
            item = nodes.pop(cur_idx)
            nodes.insert(target_idx, item)

    rerun_count = 0
    if bool(req.rerun_failed):
        for n in nodes:
            if not isinstance(n, dict):
                continue
            st = str(n.get("status") or "").strip().lower()
            if st in {"failed", "blocked"}:
                n["status"] = "pending"
                n["result"] = None
                rerun_count += 1
        if rerun_count:
            autonomy_runtime.goals.update_goal_status(goal_id, "pending", last_error=None)
            autonomy_runtime.goals.append_report(
                goal_id,
                {
                    "type": "graph_rerun_requested",
                    "requested_by": username or "system",
                    "rerun_count": rerun_count,
                },
            )

    graph["nodes"] = nodes
    autonomy_runtime.goals.append_report(
        goal_id,
        {
            "type": "graph_updated",
            "updated_by": username or "system",
            "graph": graph,
        },
    )

    return {
        "status": "ok",
        "goal_id": goal_id,
        "graph": graph,
        "rerun_count": rerun_count,
    }


@app.post("/api/autonomy/control")
async def autonomy_control(req: AutonomyControlRequest):
    principal = _get_principal(req.session_id) if req.session_id else {"username": "system", "role": "anonymous"}
    if CLOUD_MODE:
        principal = _require_authenticated_session(req.session_id)

    if str(principal.get("role") or "").lower() != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    action = str(req.action or "").strip().lower()
    if action == "pause":
        autonomy_runtime.set_paused(True)
    elif action == "resume":
        autonomy_runtime.set_paused(False)
    elif action == "tick":
        await autonomy_runtime.run_tick_once()
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported action")

    return {"status": "ok", "control": autonomy_runtime.control_state()}


@app.get("/api/anatomy/state")
async def anatomy_state(session_id: str | None = None):
    principal = None
    if CLOUD_MODE:
        principal = _require_authenticated_session(session_id)
    elif session_id:
        principal = _get_principal(session_id)

    goals = autonomy_runtime.goals.list_goals(statuses=["pending", "running", "awaiting_confirmation", "failed", "completed", "blocked"], limit=120)
    goal_counts = {
        "pending": 0,
        "running": 0,
        "awaiting_confirmation": 0,
        "failed": 0,
        "completed": 0,
        "blocked": 0,
    }
    for g in goals:
        s = str((g or {}).get("status") or "").strip().lower()
        if s in goal_counts:
            goal_counts[s] += 1

    tasks = task_manager.get_all_tasks() or []
    task_counts = {
        "pending": 0,
        "in_progress": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "blocked": 0,
    }
    for t in tasks:
        s = str((t or {}).get("status") or "").strip().lower()
        if s in task_counts:
            task_counts[s] += 1

    connected = await device_hub.list_agents()
    delegated_rows = []
    try:
        if principal and (principal.get("username") or principal.get("role")):
            delegated_rows = _list_delegated_tasks_for_principal(principal, limit=150)
    except Exception:
        delegated_rows = []
    knowledge = {
        "learning_examples": 0,
        "web_training_items": 0,
    }
    try:
        database._ensure_connected()
        if database.db is not None:
            knowledge["learning_examples"] = int(database.db["learning_examples"].count_documents({}))
            knowledge["web_training_items"] = int(database.db["web_training_data"].count_documents({}))
    except Exception:
        pass

    health = autonomy_runtime._health_check()
    return {
        "status": "ok",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "health": health,
            "control": autonomy_runtime.control_state(),
        },
        "agents": {
            "definitions": autonomy_runtime.controller.list_agents(),
            "count": len(autonomy_runtime.controller.list_agents()),
        },
        "task_graph": {
            "goals": goal_counts,
            "tasks": task_counts,
            "recent_goals": goals[:20],
        },
        "knowledge_store": knowledge,
        "runtime_services": {
            "tools": autonomy_runtime.tools.list_tools(),
            "tool_count": len(autonomy_runtime.tools.list_tools()),
            "self_improvement": health.get("self_improvement") if isinstance(health, dict) else {},
        },
        "device_connections": {
            "connected_count": len(connected),
            "devices": list(connected.values()),
            "delegated_summary": _delegated_status_counts(delegated_rows),
            "delegated_tasks": delegated_rows[:20],
        },
    }


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
            validate_pcm16_audio_quality,
        )

        if auth_req.action == "register":
            if not (auth_req.voice_sample_hash or auth_req.voice_sample_text):
                return {"status": "error", "message": "Voice sample hash or text required for registration"}

            # Prevent privilege escalation on hosted deployments.
            uname = (auth_req.username or "").strip().lower()
            requested_role = (auth_req.role or "user").strip().lower()

            # Only allow role=admin when registering the configured admin username.
            # In cloud mode this preserves least privilege for normal users while still
            # allowing admin-only product surfaces (update console/runtime control).
            if requested_role == "admin" and uname == ADMIN_USERNAME:
                role = "admin"
            else:
                role = "user"

            result = voice_auth.register_user(
                uname,
                auth_req.voice_sample_hash or "",
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
                        auth_req.voice_sample_hash or "",
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
            if not (auth_req.voice_sample_hash or auth_req.voice_sample_text):
                return {"status": "error", "message": "Voice sample hash or text required for login"}

            is_valid, session_or_error = voice_auth.authenticate_by_voice(
                auth_req.username,
                auth_req.voice_sample_hash or "",
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
                            ok_audio, audio_code, audio_msg = validate_pcm16_audio_quality(
                                audio_bytes,
                                int(auth_req.sample_rate_hz or 16000),
                            )
                            if not ok_audio:
                                return {
                                    "status": "error",
                                    "message": f"Voice biometrics audio invalid: {audio_msg}",
                                    "code": f"biometrics_{audio_code}",
                                }
                            emb = compute_embedding_from_pcm16(audio_bytes, int(auth_req.sample_rate_hz or 16000))
                            if emb is None:
                                if VOICE_BIOMETRICS_STRICT_LOGIN:
                                    return {
                                        "status": "error",
                                        "message": "Could not extract voice biometrics from this sample. Try again.",
                                        "code": "biometrics_extract_failed",
                                    }
                                try:
                                    logging.getLogger(__name__).warning(
                                        "Biometrics extraction failed but tolerated for user=%s (strict_login=false)",
                                        uname,
                                    )
                                except Exception:
                                    pass
                            ok, score = should_accept(emb, stored_vecs, threshold=VOICE_BIOMETRICS_THRESHOLD)
                            if not ok:
                                if VOICE_BIOMETRICS_STRICT_LOGIN:
                                    return {
                                        "status": "error",
                                        "message": "Voice biometrics did not match this account.",
                                        "code": "biometrics_mismatch",
                                        "score": score,
                                    }
                                try:
                                    logging.getLogger(__name__).warning(
                                        "Biometrics mismatch tolerated for user=%s score=%.3f threshold=%.3f",
                                        uname,
                                        float(score or 0.0),
                                        float(VOICE_BIOMETRICS_THRESHOLD),
                                    )
                                except Exception:
                                    pass
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
        validate_pcm16_audio_quality,
    )
    if not VOICE_BIOMETRICS_ENABLED:
        raise HTTPException(status_code=501, detail="VOICE_BIOMETRICS_ENABLED is false")

    if not req.audio_b64 or len(req.audio_b64) > 6_000_000:
        raise HTTPException(status_code=413, detail="Audio payload too large")

    audio_bytes = _decode_pcm16_b64(req.audio_b64)
    ok_audio, audio_code, audio_msg = validate_pcm16_audio_quality(audio_bytes, int(req.sample_rate_hz or 16000))
    if not ok_audio:
        raise HTTPException(status_code=400, detail={"code": f"biometrics_{audio_code}", "message": audio_msg})

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
        validate_pcm16_audio_quality,
    )

    if not VOICE_BIOMETRICS_ENABLED:
        raise HTTPException(status_code=501, detail="VOICE_BIOMETRICS_ENABLED is false")

    if not req.audio_b64 or len(req.audio_b64) > 6_000_000:
        raise HTTPException(status_code=413, detail="Audio payload too large")

    audio_bytes = _decode_pcm16_b64(req.audio_b64)
    ok_audio, audio_code, audio_msg = validate_pcm16_audio_quality(audio_bytes, int(req.sample_rate_hz or 16000))
    if not ok_audio:
        raise HTTPException(status_code=400, detail={"code": f"biometrics_{audio_code}", "message": audio_msg})

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
            from pymongo.errors import PyMongoError

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
            except (PyMongoError, OSError):
                pass
        except (PyMongoError, ValueError, OSError) as e:
            db_ping_ok = False
            db_ping_error = str(e)[:200]

    payload = {
        "status": "ok",
        "time_utc": datetime.now(timezone.utc).isoformat(),
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


def _extract_delete_task_title(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    m = re.match(
        r"^\s*(?:delete|remove|cancel)\s+(?:the\s+)?task\s+(?:title\s+)?[\"']?(.+?)[\"']?\s*$",
        t,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    title = (m.group(1) or "").strip(" .,!?:;\"'")
    return title or None


def _parse_admin_update_voice_command(text: str) -> dict | None:
    t = (text or "").strip()
    if not t:
        return None
    tl = t.lower()

    if re.search(r"\b(show|get|check|open|list)\b.*\b(update\s+history|updates\s+history|update\s+log|update\s+logs)\b", tl):
        return {"kind": "history", "limit": 20}

    m = re.match(
        r"^\s*(?:dry\s*run|validate)\s+update\s+file\s+(.+?)\s*(?::|,|\s+with\s+)\s*(.+)\s*$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        return {
            "kind": "update",
            "file_path": (m.group(1) or "").strip(),
            "description": (m.group(2) or "").strip(),
            "dry_run": True,
            "auto_install_deps": ("auto install" in tl) or ("install dependencies" in tl),
        }

    m = re.match(
        r"^\s*(?:run\s+)?update\s+file\s+(.+?)\s*(?::|,|\s+with\s+)\s*(.+)\s*$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        return {
            "kind": "update",
            "file_path": (m.group(1) or "").strip(),
            "description": (m.group(2) or "").strip(),
            "dry_run": False,
            "auto_install_deps": ("auto install" in tl) or ("install dependencies" in tl),
        }

    m = re.match(
        r"^\s*rollback\s+file\s+(.+?)(?:\s+backup\s+(.+))?\s*$",
        t,
        flags=re.IGNORECASE,
    )
    if m:
        return {
            "kind": "rollback",
            "file_path": (m.group(1) or "").strip(),
            "backup_path": (m.group(2) or "").strip() or None,
        }

    return None

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
    if CLOUD_MODE:
        username = principal.get("username")
    elif msg.session_id:
        username = _require_voice_session(msg.session_id)
    if username:
        msg.user = username

    logger.info(
        "[chat.entry] request_id=%s mode=%s cloud=%s user=%s role=%s text_len=%s",
        request_id,
        str(msg.mode or "chat"),
        str(bool(CLOUD_MODE)),
        str(username or msg.user or principal.get("username") or "anonymous"),
        str(role),
        len((msg.text or "").strip()),
    )

    # Admin-only module update cycle flow (start/continue/delete by title).
    # Uses explicit phrases so unrelated conversations continue normally.
    message_text = (msg.text or "").strip()
    if message_text and role == "admin":
        update_voice_cmd = _parse_admin_update_voice_command(message_text)
        if update_voice_cmd and SELF_UPDATE_AVAILABLE:
            actor = (username or principal.get("username") or "admin").strip().lower() or "admin"
            kind = (update_voice_cmd.get("kind") or "").strip().lower()

            if kind == "history" and get_update_history is not None:
                limit = int(update_voice_cmd.get("limit") or 20)
                history_res = get_update_history(limit=limit)
                rows = history_res.get("history") if isinstance(history_res, dict) else []
                rows = rows if isinstance(rows, list) else []
                top = rows[:5]
                if not top:
                    text = "No update history found."
                else:
                    lines = []
                    for row in top:
                        if not isinstance(row, dict):
                            continue
                        lines.append(
                            f"- {(row.get('action') or 'update')} | {(row.get('status') or 'unknown')} | {(row.get('target_file') or '-') }"
                        )
                    text = "Recent update history:\n" + ("\n".join(lines) if lines else "No update history found.")
                return {
                    "text": text,
                    "actions": [],
                    "request_id": request_id,
                    "update_history": history_res,
                }

            if kind == "update" and self_update_file is not None:
                upd = self_update_file(
                    str(update_voice_cmd.get("description") or "").strip(),
                    str(update_voice_cmd.get("file_path") or "").strip(),
                    actor=actor,
                    auto_install_deps=bool(update_voice_cmd.get("auto_install_deps")),
                    dry_run=bool(update_voice_cmd.get("dry_run")),
                )
                status_text = (upd.get("message") or "Update processed.") if isinstance(upd, dict) else "Update processed."
                return {
                    "text": status_text,
                    "actions": [],
                    "request_id": request_id,
                    "update_result": upd,
                }

            if kind == "rollback" and rollback_file is not None:
                rb = rollback_file(
                    str(update_voice_cmd.get("file_path") or "").strip(),
                    update_voice_cmd.get("backup_path"),
                    actor=actor,
                )
                status_text = (rb.get("message") or "Rollback processed.") if isinstance(rb, dict) else "Rollback processed."
                return {
                    "text": status_text,
                    "actions": [],
                    "request_id": request_id,
                    "rollback_result": rb,
                }

        delete_title = _extract_delete_task_title(message_text)
        if delete_title:
            delete_result = task_manager.delete_tasks_by_title(
                delete_title,
                owner=(username or principal.get("username") or "admin"),
                is_admin=True,
            )
            return {
                "text": delete_result.get("message") or "Task deletion processed.",
                "actions": [],
                "request_id": request_id,
                "task_delete": delete_result,
            }

        start_title = module_cycle_service.parse_start_module_title(message_text)
        if start_title:
            cycle = await module_cycle_service.start_cycle(
                title=start_title,
                username=(username or principal.get("username") or "admin"),
                background_tasks=background_tasks,
            )
            if cycle.get("handled"):
                response = cycle.get("response") or {"text": "Module cycle started.", "actions": []}
                response["request_id"] = request_id
                return response

        if module_cycle_service.is_continue_command(message_text):
            cycle = await module_cycle_service.continue_cycle(
                text=message_text,
                username=(username or principal.get("username") or "admin"),
                background_tasks=background_tasks,
            )
            if cycle.get("handled"):
                response = cycle.get("response") or {"text": "Module cycle continued.", "actions": []}
                response["request_id"] = request_id
                return response
    
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
    logger.info(
        "[chat.mode] request_id=%s mode=%s source=%s action_count=%s",
        request_id,
        str(msg.mode or "chat"),
        str((response or {}).get("source") or "unknown"),
        len(actions) if isinstance(actions, list) else 0,
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
        logger.info(
            "[chat.response] request_id=%s mode=cloud source=%s actions=%s",
            request_id,
            str((response or {}).get("source") or "unknown"),
            len(remaining_actions),
        )
        return response

    # Local mode
    try:
        response["request_id"] = request_id
    except Exception:
        pass
    logger.info(
        "[chat.response] request_id=%s mode=local source=%s actions=%s",
        request_id,
        str((response or {}).get("source") or "unknown"),
        len((response or {}).get("actions") or []),
    )
    return response

# Alias for backward compatibility
@app.post("/api/message")
async def message_endpoint(msg: MessageIn, background_tasks: BackgroundTasks):
    """Alias for /api/chat endpoint for backward compatibility"""
    return await chat_endpoint(msg, background_tasks)

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
    auto_install_deps: bool | None = None
    dry_run: bool = False


class AdminUpdateRunRequest(BaseModel):
    description: str
    file_path: str
    session_id: str
    auto_install_deps: bool | None = None
    dry_run: bool = False


class AdminRollbackRequest(BaseModel):
    file_path: str
    backup_path: str | None = None
    session_id: str


def _admin_update_target_map() -> dict[str, list[str]]:
    return {
        "backend": [
            "apps/web/app.py",
            "src/core/chat_orchestrator.py",
            "src/core/llm_adapter.py",
            "src/core/jarvis_brain.py",
        ],
        "frontend": [
            "frontend/src/App.jsx",
            "frontend/src/pages/AutonomyDashboard.jsx",
            "frontend/src/components/UpdateManagementConsole.jsx",
        ],
        "agents": [
            "apps/pc_agent/pc_agent.py",
            "src/agents/agent_controller.py",
            "src/autonomy/runtime.py",
        ],
        "tools": [
            "src/tools/tool_registry.py",
            "mcp_server/server.py",
            "src/internet/internet.py",
        ],
    }

@app.post("/api/self-update")
async def handle_self_update(request: SelfUpdateRequest):
    """Handle self-update commands from voice input."""
    try:
        if CLOUD_MODE:
            delegated = await _delegate_or_queue_cloud_action(
                session_id=request.session_id,
                feature="Self-update",
                actions=[
                    {
                        "type": "self_update",
                        "description": request.description or request.command,
                        "file_path": request.file_path or "",
                        "auto_install_deps": bool(request.auto_install_deps),
                        "dry_run": bool(request.dry_run),
                    }
                ],
                source_text=request.command or request.description or "self_update",
                require_admin=True,
                await_timeout_s=12.0,
            )
            if delegated.get("status") == "completed":
                first = _delegated_first_result(delegated) or {}
                if isinstance(first, dict):
                    first.setdefault("mode", "cloud")
                    first["execution"] = delegated
                    return first
            return {
                "status": delegated.get("status") or "delegated",
                "mode": "cloud",
                "message": "Self-update delegated to PC agent.",
                "execution": delegated,
            }

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
            result = self_update_file(
                description,
                file_path,
                actor=username,
                auto_install_deps=request.auto_install_deps,
                dry_run=bool(request.dry_run),
            )
            return result
        
        elif action == "add":
            feature_type = parsed.get("feature_type", "module")
            description = parsed.get("description", request.description or request.command)
            result = self_add_feature(description, feature_type, actor=username)
            return result
        
        return {"status": "error", "message": f"Unknown action: {action}"}
        
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/admin/updates/history")
async def admin_updates_history(session_id: str, limit: int = 100):
    _require_admin_session(session_id)
    if not SELF_UPDATE_AVAILABLE or get_update_history is None:
        return {"status": "error", "message": "Self-update is unavailable", "history": []}
    return get_update_history(limit=limit)


@app.post("/api/admin/updates/run")
async def admin_updates_run(req: AdminUpdateRunRequest):
    principal = _require_admin_session(req.session_id)
    if not SELF_UPDATE_AVAILABLE or self_update_file is None:
        return {"status": "error", "message": "Self-update is unavailable"}
    actor = (principal.get("username") or "admin").strip().lower() or "admin"
    return self_update_file(
        req.description,
        req.file_path,
        actor=actor,
        auto_install_deps=req.auto_install_deps,
        dry_run=bool(req.dry_run),
    )


@app.post("/api/admin/updates/rollback")
async def admin_updates_rollback(req: AdminRollbackRequest):
    principal = _require_admin_session(req.session_id)
    if not SELF_UPDATE_AVAILABLE or rollback_file is None:
        return {"status": "error", "message": "Rollback is unavailable"}
    actor = (principal.get("username") or "admin").strip().lower() or "admin"
    return rollback_file(req.file_path, req.backup_path, actor=actor)


@app.get("/api/admin/updates/progressive-report")
async def admin_progressive_update_report(session_id: str):
    _require_admin_session(session_id)
    if not SCHEDULER_AVAILABLE or get_progressive_llm_update_report is None:
        return {
            "status": "error",
            "message": "Scheduler/reporting is unavailable",
            "report": None,
        }
    return get_progressive_llm_update_report()


@app.get("/api/admin/updates/config")
async def admin_updates_config(session_id: str):
    _require_admin_session(session_id)
    return {
        "status": "success",
        "llm": {
            "provider": str(getattr(rd, "LLM_PROVIDER", "openai_compatible") or "openai_compatible"),
            "primary_model": str(getattr(rd, "PRIMARY_MODEL", "") or ""),
            "primary_endpoint": str(getattr(rd, "PRIMARY_ENDPOINT", "") or ""),
            "smart_model": str(getattr(rd, "SMART_MODEL", "") or ""),
        },
        "targets": _admin_update_target_map(),
    }


@app.post("/api/admin/updates/auto")
async def admin_updates_auto(req: AdminAutoUpdateRunRequest):
    principal = _require_admin_session(req.session_id)
    if not SELF_UPDATE_AVAILABLE or self_update_file is None:
        return {"status": "error", "message": "Self-update is unavailable", "results": []}

    actor = (principal.get("username") or "admin").strip().lower() or "admin"
    scopes = [str(s).strip().lower() for s in (req.scopes or ["backend", "frontend", "agents", "tools"]) if str(s).strip()]
    target_map = _admin_update_target_map()

    selected_files: list[str] = []
    for scope in scopes:
        selected_files.extend(target_map.get(scope, []))

    # Keep deterministic order and avoid duplicates.
    seen: set[str] = set()
    ordered_files: list[str] = []
    for fp in selected_files:
        if fp not in seen:
            seen.add(fp)
            ordered_files.append(fp)

    results: list[dict] = []
    for fp in ordered_files:
        try:
            out = self_update_file(
                req.description,
                fp,
                actor=actor,
                auto_install_deps=req.auto_install_deps,
                dry_run=bool(req.dry_run),
            )
            results.append({"file_path": fp, **(out or {})})
        except Exception as e:
            results.append({"file_path": fp, "status": "error", "message": str(e)})

    ok = all(str(r.get("status") or "").lower() == "success" for r in results) if results else False
    return {
        "status": "success" if ok else "partial",
        "message": "Auto-update run complete" if ok else "Auto-update completed with errors",
        "scopes": scopes,
        "results": results,
    }

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


class DeleteTaskByTitleRequest(BaseModel):
    title: str
    session_id: str | None = None

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


@app.post("/api/delete-task-by-title")
async def delete_task_by_title_endpoint(request: DeleteTaskByTitleRequest):
    if CLOUD_MODE:
        principal = _require_authenticated_session(request.session_id)
        if principal.get("role") != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
        username = (principal.get("username") or "").strip().lower()
    else:
        principal = _require_admin_session(request.session_id)
        username = (principal.get("username") or "admin").strip().lower() or "admin"

    return task_manager.delete_tasks_by_title(
        request.title,
        owner=username,
        is_admin=True,
    )

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
        return {"tasks": tasks, "delegated_tasks": []}

    principal = _require_authenticated_session(session_id)
    username = (principal.get("username") or "").strip().lower()
    is_admin = (principal.get("role") == "admin")
    delegated_rows = _list_delegated_tasks_for_principal(principal, limit=120)
    if is_admin:
        return {
            "tasks": tasks,
            "delegated_tasks": delegated_rows,
            "delegated_summary": _delegated_status_counts(delegated_rows),
        }

    filtered = []
    for t in tasks:
        try:
            meta = t.get("meta") if isinstance(t, dict) else None
            owner = (meta.get("user_id") or "").strip().lower() if isinstance(meta, dict) else ""
            if owner and owner == username:
                filtered.append(t)
        except Exception:
            continue
    return {
        "tasks": filtered,
        "delegated_tasks": delegated_rows,
        "delegated_summary": _delegated_status_counts(delegated_rows),
    }


@app.get("/api/delegated/tasks")
async def list_delegated_tasks(session_id: str, limit: int = 120, statuses: str | None = None):
    principal = _require_authenticated_session(session_id)
    rows = _list_delegated_tasks_for_principal(principal, limit=limit, statuses=statuses)
    return {
        "status": "success",
        "tasks": rows,
        "count": len(rows),
        "summary": _delegated_status_counts(rows),
    }


@app.get("/api/agents")
async def list_autonomy_agents(session_id: str | None = None):
    """List autonomous agent definitions and currently connected device agents."""
    if CLOUD_MODE:
        _require_authenticated_session(session_id)

    connected = await device_hub.list_agents()
    return {
        "status": "ok",
        "agents": autonomy_runtime.controller.list_agents(),
        "connected_device_agents": list(connected.values()),
        "count": len(autonomy_runtime.controller.list_agents()),
    }


@app.get("/api/device/list")
async def list_devices(session_id: str | None = None):
    """List connected device agents and persisted registry rows."""
    if CLOUD_MODE:
        _require_authenticated_session(session_id)

    connected = await device_hub.list_agents()
    rows: list[dict[str, Any]] = []
    try:
        database._ensure_connected()
        if database.db is not None:
            rows = list(database.db["device_registry"].find({}, {"_id": 0}).sort("updated_at", -1).limit(200))
    except Exception:
        rows = []

    connected_ids = {str(v.get("device_id") or k) for k, v in connected.items()}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in rows:
        device_id = str(row.get("device_id") or "").strip()
        if not device_id:
            continue
        seen.add(device_id)
        merged.append(
            {
                **row,
                "device_id": device_id,
                "connected": bool(row.get("connected") or (device_id in connected_ids)),
            }
        )

    for key, conn in connected.items():
        device_id = str(conn.get("device_id") or key).strip()
        if not device_id or device_id in seen:
            continue
        merged.append(
            {
                "device_id": device_id,
                "connected": True,
                "capabilities": conn.get("capabilities") if isinstance(conn, dict) else {},
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    return {
        "status": "ok",
        "devices": merged,
        "count": len(merged),
    }


class SelfImprovementDecisionRequest(BaseModel):
    proposal_id: str
    decision: str
    session_id: str | None = None


@app.get("/api/self-improvement/proposals")
async def list_self_improvement_proposals(session_id: str | None = None):
    """Return pending self-improvement proposals for explicit human approval."""
    principal = None
    if CLOUD_MODE:
        principal = _require_authenticated_session(session_id)
    elif session_id:
        principal = _get_principal(session_id)

    role = str((principal or {}).get("role") or "user").lower()
    if CLOUD_MODE and role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    proposals: list[dict[str, Any]] = []
    try:
        database._ensure_connected()
        if database.db is not None:
            proposals = list(
                database.db["self_improvement_proposals"]
                .find({"status": "pending"}, {"_id": 0})
                .sort("created_at", -1)
                .limit(200)
            )
    except Exception:
        proposals = []

    return {"status": "ok", "proposals": proposals, "count": len(proposals)}


@app.post("/api/self-improvement/proposals/decision")
async def decide_self_improvement_proposal(req: SelfImprovementDecisionRequest):
    principal = None
    if CLOUD_MODE:
        principal = _require_authenticated_session(req.session_id)
    elif req.session_id:
        principal = _get_principal(req.session_id)

    role = str((principal or {}).get("role") or "user").lower()
    if role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    decision = str(req.decision or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="decision must be approve or reject")

    next_status = "approved" if decision == "approve" else "rejected"

    updated = False
    try:
        database._ensure_connected()
        if database.db is not None:
            result = database.db["self_improvement_proposals"].update_one(
                {"proposal_id": req.proposal_id},
                {
                    "$set": {
                        "status": next_status,
                        "reviewed_at": datetime.now(timezone.utc),
                        "reviewed_by": str((principal or {}).get("username") or "admin"),
                    }
                },
                upsert=False,
            )
            updated = bool(result.modified_count or result.matched_count)
    except Exception:
        updated = False

    return {
        "status": "ok",
        "message": f"Proposal {decision}",
        "proposal_id": req.proposal_id,
        "proposal_status": next_status,
        "updated": updated,
    }

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
        delegated = await _delegate_or_queue_cloud_action(
            session_id=req.session_id,
            feature="File operations",
            actions=[{"type": "read", "path": req.path}],
            source_text=f"file_read:{req.path}",
            require_admin=True,
            await_timeout_s=8.0,
        )
        if delegated.get("status") == "completed":
            first = _delegated_first_result(delegated) or {}
            if isinstance(first, dict):
                first["execution"] = delegated
                return first
        return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
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
        delegated = await _delegate_or_queue_cloud_action(
            session_id=req.session_id,
            feature="File operations",
            actions=[{"type": "write", "path": req.path, "content": req.content}],
            source_text=f"file_write:{req.path}",
            require_admin=True,
            await_timeout_s=10.0,
        )
        if delegated.get("status") == "completed":
            first = _delegated_first_result(delegated) or {}
            if isinstance(first, dict):
                first["execution"] = delegated
                return first
        return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
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
        delegated = await _delegate_or_queue_cloud_action(
            session_id=req.session_id,
            feature="File operations",
            actions=[{"type": "list", "path": req.path}],
            source_text=f"file_list:{req.path}",
            require_admin=True,
            await_timeout_s=8.0,
        )
        if delegated.get("status") == "completed":
            first = _delegated_first_result(delegated) or {}
            if isinstance(first, dict):
                first["execution"] = delegated
                return first
        return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
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
        delegated = await _delegate_or_queue_cloud_action(
            session_id=req.session_id,
            feature="File operations",
            actions=[{"type": "delete", "path": req.path}],
            source_text=f"file_delete:{req.path}",
            require_admin=True,
            await_timeout_s=8.0,
        )
        if delegated.get("status") == "completed":
            first = _delegated_first_result(delegated) or {}
            if isinstance(first, dict):
                first["execution"] = delegated
                return first
        return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
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
        delegated = await _delegate_or_queue_cloud_action(
            session_id=req.session_id,
            feature="File operations",
            actions=[{"type": "mkdir", "path": req.path}],
            source_text=f"file_mkdir:{req.path}",
            require_admin=True,
            await_timeout_s=8.0,
        )
        if delegated.get("status") == "completed":
            first = _delegated_first_result(delegated) or {}
            if isinstance(first, dict):
                first["execution"] = delegated
                return first
        return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
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
        delegated = await _delegate_or_queue_cloud_action(
            session_id=req.session_id,
            feature="File operations",
            actions=[{"type": "copy", "source": req.source, "destination": req.destination}],
            source_text=f"file_copy:{req.source}",
            require_admin=True,
            await_timeout_s=10.0,
        )
        if delegated.get("status") == "completed":
            first = _delegated_first_result(delegated) or {}
            if isinstance(first, dict):
                first["execution"] = delegated
                return first
        return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
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
        sid = (req or {}).get("session_id") if isinstance(req, dict) else None
        delegated = await _delegate_or_queue_cloud_action(
            session_id=sid,
            feature="File operations",
            actions=[{"type": "cleanup"}],
            source_text="file_cleanup",
            require_admin=True,
            await_timeout_s=12.0,
        )
        if delegated.get("status") == "completed":
            first = _delegated_first_result(delegated) or {}
            if isinstance(first, dict):
                first["execution"] = delegated
                return first
        return {"status": delegated.get("status") or "delegated", "mode": "cloud", "execution": delegated}
    _require_admin_session((req or {}).get("session_id"))
    try:
        result = file_ops.cleanup_project()
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}

app.include_router(build_telegram_router(
    voice_only_mode=bool(VOICE_ONLY_MODE),
    telegram_bot=telegram_bot,
    brain=brain,
    executor=executor,
    env=env,
    admin_only_action_types=ADMIN_ONLY_ACTION_TYPES,
    user_explicitly_requested_screen_capture=_user_explicitly_requested_screen_capture,
    build_web_context_from_action_results=_build_web_context_from_action_results,
    persist_web_context_items=_persist_web_context_items,
    web_lookup_found=_web_lookup_found,
    continue_user_using_web_context=_continue_user_using_web_context,
))

app.include_router(build_internet_router(_require_voice_session))

app.include_router(build_system_control_router(
    cloud_mode=bool(CLOUD_MODE),
    cloud_feature_disabled=_cloud_feature_disabled,
    cloud_safe_system_info=_build_cloud_safe_system_info,
    cloud_delegate_or_queue=_delegate_or_queue_cloud_action,
    require_admin_session=_require_admin_session,
    require_authenticated_session=_require_authenticated_session,
    screen_access=screen_access,
    app_manager=app_manager,
    system_ops_available=bool(SYSTEM_OPS_AVAILABLE),
    system_ops=system_ops if SYSTEM_OPS_AVAILABLE else None,
))

app.include_router(build_session_router(session_manager))

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
