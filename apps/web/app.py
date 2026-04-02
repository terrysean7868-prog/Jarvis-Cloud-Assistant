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
from urllib.parse import quote_plus
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
try:
    from src.learning import SelfLearningEngine
except Exception:
    SelfLearningEngine = None

try:
    from src.model_ops import (
        list_models as model_ops_list_models,
        capability_summary as model_ops_capability_summary,
        recommend_with_mode as model_ops_recommend_with_mode,
        inspect_dataset as model_ops_inspect_dataset,
        compute_readiness as model_ops_compute_readiness,
        prepare_finetune_run as model_ops_prepare_finetune_run,
        list_profiles as model_ops_list_profiles,
        update_profile as model_ops_update_profile,
        load_registry as model_ops_load_registry,
        check_health as model_ops_check_health,
        run_benchmark as model_ops_run_benchmark,
        latest_benchmark_report as model_ops_latest_benchmark_report,
        update_benchmark as model_ops_update_benchmark,
        update_health as model_ops_update_health,
        update_readiness as model_ops_update_readiness,
    )
    from src.model_ops.model_recommender import save_recommendation as model_ops_save_recommendation
    MODEL_OPS_AVAILABLE = True
except Exception:
    model_ops_list_models = None
    model_ops_capability_summary = None
    model_ops_recommend_with_mode = None
    model_ops_inspect_dataset = None
    model_ops_compute_readiness = None
    model_ops_prepare_finetune_run = None
    model_ops_list_profiles = None
    model_ops_update_profile = None
    model_ops_load_registry = None
    model_ops_check_health = None
    model_ops_run_benchmark = None
    model_ops_latest_benchmark_report = None
    model_ops_update_benchmark = None
    model_ops_update_health = None
    model_ops_update_readiness = None
    model_ops_save_recommendation = None
    MODEL_OPS_AVAILABLE = False

try:
    from src.ai_training.data_schemas import normalize_for_collection as normalize_training_doc
except Exception:
    normalize_training_doc = None

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


def _load_agent_shared_secret_from_db() -> str:
    """Return the persisted agent shared secret from MongoDB when available."""
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return ""

    try:
        col = database.db["agent_configs"]
        doc = col.find_one(
            {
                "$or": [
                    {"shared_secret": {"$exists": True, "$ne": ""}},
                    {"agent_shared_secret": {"$exists": True, "$ne": ""}},
                ]
            },
            sort=[("updated_at", -1), ("created_at", -1)],
        )
        if not doc:
            return ""
        return _clean_cfg_str(doc.get("shared_secret") or doc.get("agent_shared_secret"))
    except Exception:
        return ""

# =========================================================
# FastAPI Initialization
# =========================================================


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup
    print("[OK] Jarvis server startup (lifespan)")

    try:
        db_secret = _load_agent_shared_secret_from_db()
        if db_secret:
            global AGENT_SHARED_SECRET
            AGENT_SHARED_SECRET = db_secret
            try:
                if hasattr(device_hub, "_shared_secret"):
                    device_hub._shared_secret = db_secret
            except Exception:
                pass
    except Exception:
        pass

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

    try:
        _start_stability_monitor()
        print("[OK] Stability monitor started")
    except Exception as e:
        print(f"[INFO] Stability monitor start failed: {e}")

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

        global _STABILITY_MONITOR_TASK
        if _STABILITY_MONITOR_TASK and not _STABILITY_MONITOR_TASK.done():
            _STABILITY_MONITOR_TASK.cancel()
            try:
                await _STABILITY_MONITOR_TASK
            except Exception:
                pass
            _STABILITY_MONITOR_TASK = None


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

# Production stability safeguards (process-local).
GLOBAL_LLM_CALL_TIMEOUT_S = 20.0
GLOBAL_AGENT_RESPONSE_TIMEOUT_S = 8.0
GLOBAL_TASK_EXEC_TIMEOUT_S = 25.0
DELEGATED_MAX_RETRIES = 2
AGENT_UNAVAILABLE_COOLDOWN_S = 30.0
DELEGATED_EXECUTION_STUCK_S = 120
HEALTH_MONITOR_INTERVAL_S = 15.0

_AGENT_UNAVAILABLE_UNTIL: dict[str, float] = {}
_STABILITY_MONITOR_TASK: asyncio.Task | None = None


def _trace_log(
    *,
    event: str,
    request_id: str | None = None,
    user_id: str | None = None,
    task_id: str | None = None,
    lifecycle_state: str | None = None,
    execution_time_ms: float | None = None,
    level: str = "info",
    **extra: Any,
) -> None:
    try:
        payload = {
            "event": str(event or "trace"),
            "request_id": (request_id or "").strip() or None,
            "user_id": (user_id or "").strip().lower() or None,
            "task_id": (task_id or "").strip() or None,
            "lifecycle_state": (lifecycle_state or "").strip().lower() or None,
            "execution_time_ms": round(float(execution_time_ms), 2) if execution_time_ms is not None else None,
        }
        for k, v in (extra or {}).items():
            payload[k] = v
        payload = {k: v for k, v in payload.items() if v is not None}
        msg = "[trace] " + " ".join(f"{k}={v}" for k, v in payload.items())
        if str(level).lower() == "warning":
            logger.warning(msg)
        elif str(level).lower() == "error":
            logger.error(msg)
        else:
            logger.info(msg)
    except Exception:
        pass


def _is_agent_temporarily_unavailable(device_id: str | None) -> bool:
    did = _normalize_device_id(device_id)
    if not did:
        return False
    until = float(_AGENT_UNAVAILABLE_UNTIL.get(did) or 0.0)
    return time.time() < until


def _mark_agent_unavailable(device_id: str | None, *, reason: str = "unresponsive") -> None:
    did = _normalize_device_id(device_id)
    if not did:
        return
    _AGENT_UNAVAILABLE_UNTIL[did] = time.time() + AGENT_UNAVAILABLE_COOLDOWN_S
    _ops_inc("agent_unavailable_events")
    logger.warning("[agent.circuit.open] device_id=%s reason=%s cooldown_s=%s", did, reason, AGENT_UNAVAILABLE_COOLDOWN_S)


def _clear_agent_unavailable(device_id: str | None) -> None:
    did = _normalize_device_id(device_id)
    if not did:
        return
    _AGENT_UNAVAILABLE_UNTIL.pop(did, None)


def _retry_or_fail_delegated_task(task_id: str | None, *, reason: str, fallback_status: str = "queued_for_agent") -> None:
    try:
        tid = str(task_id or "").strip()
        if not tid:
            return
        col = _delegated_tasks_collection()
        if col is None:
            return
        row = col.find_one({"task_id": tid}, {"_id": 0}) or {}
        attempts = int(row.get("attempts") or 0)
        next_attempts = attempts + 1
        if next_attempts <= int(DELEGATED_MAX_RETRIES):
            _mark_delegated_task(
                task_id=tid,
                status_value=fallback_status,
                extra={
                    "attempts": next_attempts,
                    "last_error": str(reason or "retry")[:300],
                    "last_retry_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        else:
            _mark_delegated_task(
                task_id=tid,
                status_value="failed",
                extra={
                    "attempts": next_attempts,
                    "error": str(reason or "max_retries_exceeded")[:300],
                    "timed_out": "timeout" in str(reason or "").lower(),
                },
            )
    except Exception:
        return

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
ENABLE_SCHEDULER = True

# =========================================================
# Runtime Mode / Security
# =========================================================
# Cloud mode is intended for hosted deployments (e.g., Render). In this mode we:
# - Require an authenticated session for chat + internet endpoints (to prevent public abuse)
# - Disable local/PC control and local filesystem endpoints (these are unsafe + meaningless in cloud)
CLOUD_MODE = bool(jarvis_settings.cloud_mode)
VOICE_ONLY_MODE = False
PC_AGENT_ENABLED = True
AGENT_SHARED_SECRET = ""
EXPOSE_AGENT_SHARED_SECRET = False
DEFAULT_DEVICE_ID = "primary"
DEVICE_OWNER_USERNAME = ""
LOCAL_DEFAULT_DEVICE_FALLBACK = True
ADMIN_USERNAME = "admin"
ADMIN_BOOTSTRAP_SECRET = ""
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

# Lightweight in-memory production telemetry (best-effort, process-local).
_OPS_TELEMETRY = {
    "chat_total": 0,
    "error_total": 0,
    "timeout_total": 0,
    "fallback_total": 0,
    "latency_ms_total": 0.0,
    "latency_samples": 0,
    "delegated_exec_success": 0,
    "delegated_exec_failure": 0,
    "recovery_restarts": 0,
    "agent_unavailable_events": 0,
    "updated_at": datetime.now(timezone.utc).isoformat(),
}


def _ops_touch() -> None:
    try:
        _OPS_TELEMETRY["updated_at"] = datetime.now(timezone.utc).isoformat()
    except Exception:
        pass


def _ops_inc(key: str, amount: int = 1) -> None:
    try:
        _OPS_TELEMETRY[key] = int(_OPS_TELEMETRY.get(key) or 0) + int(amount)
        _ops_touch()
    except Exception:
        pass


def _ops_add_latency_ms(value_ms: float) -> None:
    try:
        v = float(value_ms)
        if v < 0:
            return
        _OPS_TELEMETRY["latency_ms_total"] = float(_OPS_TELEMETRY.get("latency_ms_total") or 0.0) + v
        _OPS_TELEMETRY["latency_samples"] = int(_OPS_TELEMETRY.get("latency_samples") or 0) + 1
        _ops_touch()
    except Exception:
        pass


def _parse_latency_to_ms(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value) * 1000.0 if float(value) < 1000 else float(value)
        s = str(value).strip().lower()
        if not s:
            return None
        if s.endswith("ms"):
            return float(s[:-2].strip())
        if s.endswith("s"):
            return float(s[:-1].strip()) * 1000.0
        return float(s)
    except Exception:
        return None


def _collect_global_delegated_counts() -> dict[str, int]:
    counts = {"queued_for_agent": 0, "pending_permission": 0}
    try:
        col = _delegated_tasks_collection()
        if col is None:
            return counts
        for st in ("queued_for_agent", "pending_permission"):
            counts[st] = int(col.count_documents({"status": st}))
    except Exception:
        return counts
    return counts


def _ops_telemetry_snapshot() -> dict[str, Any]:
    chat_total = int(_OPS_TELEMETRY.get("chat_total") or 0)
    error_total = int(_OPS_TELEMETRY.get("error_total") or 0)
    timeout_total = int(_OPS_TELEMETRY.get("timeout_total") or 0)
    fallback_total = int(_OPS_TELEMETRY.get("fallback_total") or 0)
    latency_samples = int(_OPS_TELEMETRY.get("latency_samples") or 0)
    latency_total = float(_OPS_TELEMETRY.get("latency_ms_total") or 0.0)
    delegated_counts = _collect_global_delegated_counts()
    delegated_success = int(_OPS_TELEMETRY.get("delegated_exec_success") or 0)
    delegated_failure = int(_OPS_TELEMETRY.get("delegated_exec_failure") or 0)
    delegated_total = delegated_success + delegated_failure

    return {
        "chat_total": chat_total,
        "error_total": error_total,
        "error_rate": (float(error_total) / float(chat_total)) if chat_total else 0.0,
        "timeout_total": timeout_total,
        "timeout_rate": (float(timeout_total) / float(chat_total)) if chat_total else 0.0,
        "fallback_total": fallback_total,
        "fallback_rate": (float(fallback_total) / float(chat_total)) if chat_total else 0.0,
        "average_response_latency_ms": (latency_total / float(latency_samples)) if latency_samples else 0.0,
        "latency_samples": latency_samples,
        "queued_for_agent_count": int(delegated_counts.get("queued_for_agent") or 0),
        "pending_permission_count": int(delegated_counts.get("pending_permission") or 0),
        "delegated_execution": {
            "success": delegated_success,
            "failure": delegated_failure,
            "success_rate": (float(delegated_success) / float(delegated_total)) if delegated_total else 0.0,
        },
        "recovery_restarts": int(_OPS_TELEMETRY.get("recovery_restarts") or 0),
        "agent_unavailable_events": int(_OPS_TELEMETRY.get("agent_unavailable_events") or 0),
        "updated_at": _OPS_TELEMETRY.get("updated_at") or datetime.now(timezone.utc).isoformat(),
    }


def _ops_alert_if_needed(snapshot: dict[str, Any]) -> None:
    try:
        fallback_rate = float(snapshot.get("fallback_rate") or 0.0)
        error_rate = float(snapshot.get("error_rate") or 0.0)
        queued = int(snapshot.get("queued_for_agent_count") or 0)
        pending = int(snapshot.get("pending_permission_count") or 0)
        queue_size = queued + pending
        if fallback_rate > 0.40:
            logger.warning("[stability.alert] metric=fallback_rate value=%.3f threshold=0.40", fallback_rate)
        if error_rate > 0.20:
            logger.warning("[stability.alert] metric=error_rate value=%.3f threshold=0.20", error_rate)
        if queue_size > 100:
            logger.warning("[stability.alert] metric=queue_size value=%s threshold=100", queue_size)
    except Exception:
        pass


def _intent_telemetry_collection():
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["intent_decision_telemetry"]
    try:
        col.create_index([("created_at", -1)])
    except Exception:
        pass
    try:
        col.create_index([("request_id", 1)], unique=False)
    except Exception:
        pass
    try:
        col.create_index([("user_id", 1), ("created_at", -1)])
    except Exception:
        pass
    try:
        col.create_index([("intent_type", 1), ("created_at", -1)])
    except Exception:
        pass
    return col


def _intent_mismatch_reason(*, intent_type: str, response_strategy: str, has_actions: bool, proactive_followup_added: bool) -> str | None:
    it = str(intent_type or "").strip().lower()
    rs = str(response_strategy or "").strip().lower()
    if it == "informational" and has_actions:
        return "unexpected_execution_for_informational"
    if it == "direct_action" and not has_actions:
        return "missing_execution_for_direct_action"
    if it == "ambiguous" and has_actions:
        return "execution_on_ambiguous"
    if it == "goal_oriented" and rs.startswith("explain_plus") and (not proactive_followup_added):
        return "goal_plan_without_proactive_prompt"
    return None


def _record_intent_telemetry(
    *,
    request_id: str,
    user_id: str | None,
    intent_type: str,
    intent_depth: str,
    response_strategy: str,
    has_execution_actions: bool,
    proactive_followup_added: bool,
    user_preference_influenced: bool,
    weak_outcome: bool,
    response_status: str,
    source: str,
    fallback_used: bool,
) -> None:
    now = datetime.now(timezone.utc)
    mismatch = _intent_mismatch_reason(
        intent_type=intent_type,
        response_strategy=response_strategy,
        has_actions=bool(has_execution_actions),
        proactive_followup_added=bool(proactive_followup_added),
    )
    row = {
        "request_id": str(request_id or "").strip() or None,
        "user_id": _normalize_user_id(user_id),
        "intent_type": str(intent_type or "ambiguous").strip().lower() or "ambiguous",
        "intent_depth": str(intent_depth or "low").strip().lower() or "low",
        "response_strategy": str(response_strategy or "unknown").strip().lower() or "unknown",
        "has_execution_actions": bool(has_execution_actions),
        "decision_mode": "execution" if bool(has_execution_actions) else "explanation",
        "proactive_followup_added": bool(proactive_followup_added),
        "user_preference_influenced": bool(user_preference_influenced),
        "weak_outcome": bool(weak_outcome),
        "response_status": str(response_status or "unknown").strip().lower() or "unknown",
        "source": str(source or "unknown").strip().lower() or "unknown",
        "fallback_used": bool(fallback_used),
        "mismatch_reason": mismatch,
        "created_at": now,
        "created_at_iso": now.isoformat(),
    }

    try:
        _trace_log(
            event="intent_decision",
            request_id=str(request_id or ""),
            user_id=(user_id or ""),
            lifecycle_state="decided",
            intent_type=row["intent_type"],
            intent_depth=row["intent_depth"],
            response_strategy=row["response_strategy"],
            has_execution_actions=row["has_execution_actions"],
            proactive_followup_added=row["proactive_followup_added"],
            user_preference_influenced=row["user_preference_influenced"],
            weak_outcome=row["weak_outcome"],
            mismatch_reason=(mismatch or "none"),
        )
    except Exception:
        pass

    try:
        col = _intent_telemetry_collection()
        if col is not None:
            col.insert_one(row)
    except Exception:
        pass


def _aggregate_intent_telemetry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    intent_counts: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    weak_by_intent: dict[str, int] = {}
    mismatch_by_intent: dict[str, int] = {}
    exec_count = 0
    explanation_count = 0

    for r in rows:
        if not isinstance(r, dict):
            continue
        it = str((r or {}).get("intent_type") or "ambiguous").strip().lower() or "ambiguous"
        st = str((r or {}).get("response_strategy") or "unknown").strip().lower() or "unknown"
        dm = str((r or {}).get("decision_mode") or "").strip().lower()
        weak = bool((r or {}).get("weak_outcome"))
        mismatch = str((r or {}).get("mismatch_reason") or "").strip().lower()

        intent_counts[it] = int(intent_counts.get(it) or 0) + 1
        strategy_counts[st] = int(strategy_counts.get(st) or 0) + 1
        if dm == "execution":
            exec_count += 1
        else:
            explanation_count += 1
        if weak:
            weak_by_intent[it] = int(weak_by_intent.get(it) or 0) + 1
        if mismatch:
            mismatch_by_intent[it] = int(mismatch_by_intent.get(it) or 0) + 1

    def _pct(v: int) -> float:
        return round((float(v) / float(max(1, total))) * 100.0, 2)

    weak_ranked = sorted(
        [
            {
                "intent_type": k,
                "weak_count": int(v),
                "mismatch_count": int(mismatch_by_intent.get(k) or 0),
            }
            for k, v in weak_by_intent.items()
        ],
        key=lambda x: (int(x.get("weak_count") or 0), int(x.get("mismatch_count") or 0)),
        reverse=True,
    )[:5]

    return {
        "total": total,
        "intent_type_distribution": intent_counts,
        "strategy_distribution": strategy_counts,
        "percentages": {
            "direct_action": _pct(int(intent_counts.get("direct_action") or 0)),
            "goal_oriented": _pct(int(intent_counts.get("goal_oriented") or 0)),
            "ambiguous": _pct(int(intent_counts.get("ambiguous") or 0)),
            "execution_decisions": _pct(exec_count),
            "explanation_decisions": _pct(explanation_count),
        },
        "top_weak_or_mismatch_by_intent_type": weak_ranked,
    }


async def _collect_full_health_checks() -> dict[str, Any]:
    checks = {
        "database": False,
        "redis": False,
        "llm_provider": False,
        "pc_agent_connected": False,
    }

    try:
        database._ensure_connected()
        checks["database"] = bool(getattr(database, "client", None) is not None and getattr(database, "db", None) is not None)
    except Exception:
        checks["database"] = False

    try:
        checks["redis"] = bool(_BROKER is not None)
    except Exception:
        checks["redis"] = False

    try:
        checks["llm_provider"] = bool(llm.primary_key or llm.backup_key) and (
            llm._provider_available("openai") or llm._provider_available("groq")
        )
    except Exception:
        checks["llm_provider"] = False

    try:
        agents = await device_hub.list_agents()
        checks["pc_agent_connected"] = bool(agents)
    except Exception:
        checks["pc_agent_connected"] = False

    ops = _ops_telemetry_snapshot()
    _ops_alert_if_needed(ops)

    status_value = "ok"
    if not all(bool(v) for v in checks.values()):
        status_value = "degraded" if any(bool(v) for v in checks.values()) else "failed"

    return {
        "status": status_value,
        "checks": checks,
        "uptime_seconds": int(max(0, time.time() - START_TS)),
        "error_rate": float(ops.get("error_rate") or 0.0),
        "fallback_rate": float(ops.get("fallback_rate") or 0.0),
    }


async def _stability_monitor_loop() -> None:
    while True:
        try:
            # Auto-recovery: restart autonomy runtime loop if it is unexpectedly disabled.
            ctrl = autonomy_runtime.control_state() if autonomy_runtime is not None else {}
            if bool(ctrl) and bool(ctrl.get("enabled")) and bool(ctrl.get("paused")) is False:
                # Runtime is enabled; no action needed.
                pass
            elif autonomy_runtime is not None:
                try:
                    await autonomy_runtime.start()
                    _ops_inc("recovery_restarts")
                    logger.warning("[stability.recovery] component=autonomy_runtime action=restart")
                except Exception:
                    pass

            # Auto-recovery: scheduler restart (best-effort).
            if ENABLE_SCHEDULER and SCHEDULER_AVAILABLE and initialize_scheduler:
                try:
                    initialize_scheduler()
                except Exception:
                    pass

            # Reset stuck delegated lifecycle states: executing/delegated -> timeout -> failed.
            col = _delegated_tasks_collection()
            if col is not None:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=DELEGATED_EXECUTION_STUCK_S)
                stale = list(
                    col.find(
                        {
                            "status": {"$in": ["executing", "delegated"]},
                            "updated_at": {"$lt": cutoff},
                        },
                        {"_id": 0, "task_id": 1, "attempts": 1},
                    ).limit(80)
                )
                for row in stale:
                    _mark_delegated_task(
                        task_id=row.get("task_id"),
                        status_value="failed",
                        extra={
                            "error": "timeout",
                            "timed_out": True,
                            "timeout_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )

            # Trigger health checks and alerts from monitor cadence.
            await _collect_full_health_checks()
        except Exception:
            pass

        await asyncio.sleep(HEALTH_MONITOR_INTERVAL_S)


def _start_stability_monitor() -> None:
    global _STABILITY_MONITOR_TASK
    if _STABILITY_MONITOR_TASK and not _STABILITY_MONITOR_TASK.done():
        return
    _STABILITY_MONITOR_TASK = asyncio.create_task(_stability_monitor_loop(), name="jarvis-stability-monitor")


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


def _plan_learning_collection():
    try:
        database._ensure_connected()
    except Exception:
        pass
    if database.db is None:
        return None
    col = database.db["learning_memory_plans"]
    try:
        col.create_index([("signature", 1), ("updated_at", -1)])
    except Exception:
        pass
    return col


def _plan_signature(text: str) -> str:
    s = str(text or "").strip().lower()
    if not s:
        return ""
    toks = re.findall(r"[a-z0-9]{3,}", s)
    stop = {
        "please", "jarvis", "open", "and", "then", "for", "with", "from", "into", "that", "this", "the", "your",
        "search", "find", "look", "show", "run", "start", "launch",
    }
    kept = [t for t in toks if t not in stop]
    if not kept:
        kept = toks
    return " ".join(sorted(set(kept[:12])))


def _load_reusable_plan(source_text: str) -> list[dict[str, Any]]:
    col = _plan_learning_collection()
    if col is None:
        return []
    sig = _plan_signature(source_text)
    if not sig:
        return []
    sig_set = set(sig.split())
    best = None
    best_score = 0.0
    try:
        rows = list(col.find({}, {"_id": 0, "signature": 1, "steps": 1, "success_rate": 1}).sort("updated_at", -1).limit(40))
        for r in rows:
            rs = str((r or {}).get("signature") or "")
            rs_set = set(rs.split())
            if not rs_set:
                continue
            overlap = len(sig_set.intersection(rs_set))
            score = float(overlap) / float(max(1, len(sig_set.union(rs_set))))
            score *= max(0.1, float((r or {}).get("success_rate") or 0.5))
            if score > best_score:
                best = r
                best_score = score
    except Exception:
        return []
    if best_score < 0.55 or not isinstance((best or {}).get("steps"), list):
        return []
    return [dict(x) for x in ((best or {}).get("steps") or []) if isinstance(x, dict)]


def _plan_steps_key(steps: list[dict[str, Any]]) -> str:
    if not isinstance(steps, list) or not steps:
        return ""
    chunks: list[str] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        action = str((s or {}).get("action") or "").strip().lower()
        params = (s or {}).get("params") if isinstance((s or {}).get("params"), dict) else {}
        param_keys = ",".join(sorted([str(k).strip().lower() for k in params.keys()]))
        chunks.append(f"{action}({param_keys})")
    return "|".join(chunks)[:500]


def _action_complexity_weight(action_name: str) -> float:
    name = str(action_name or "").strip().lower()
    if name in {"open_url", "wait", "focus_window", "close_app"}:
        return 0.9
    if name in {"search_web", "browser_click", "browser_type", "open_app"}:
        return 1.2
    if name in {"run_command", "run_code", "filesystem_write", "install_package"}:
        return 1.8
    return 1.3


def _recent_device_error_rate(*, device_id: str | None, username: str | None = None) -> float:
    did = _normalize_device_id(device_id)
    try:
        col = _delegated_tasks_collection()
        if col is None:
            return 0.0
        query: dict[str, Any] = {"status": {"$in": ["failed", "completed", "queued_for_agent"]}}
        if did:
            query["device_id"] = did
        elif username:
            query["username"] = str(username or "").strip().lower()
        rows = list(col.find(query, {"_id": 0, "status": 1, "error": 1, "last_error": 1, "updated_at": 1}).sort("updated_at", -1).limit(20))
        if not rows:
            return 0.0
        failed = 0
        for row in rows:
            status = str((row or {}).get("status") or "").strip().lower()
            if status == "failed":
                failed += 1
                continue
            if status == "queued_for_agent" and str((row or {}).get("last_error") or "").strip():
                failed += 1
        return max(0.0, min(1.0, float(failed) / float(max(1, len(rows)))))
    except Exception:
        return 0.0


def _plan_history_stats(source_text: str, steps: list[dict[str, Any]]) -> dict[str, float]:
    sig = _plan_signature(source_text)
    key = _plan_steps_key(steps)
    sig_set = set(sig.split())
    weighted = 0.0
    weight_sum = 0.0
    exact_plan_success_rate = -1.0
    recent_failure_penalty = 0.0
    try:
        col = _plan_learning_collection()
        if col is None or not sig:
            return {
                "similar_success_probability": 0.5,
                "past_success_rate": 0.5,
                "recent_failure_penalty": 0.0,
            }
        rows = list(col.find({}, {"_id": 0, "signature": 1, "success_rate": 1, "plan_stats": 1, "updated_at": 1}).sort("updated_at", -1).limit(80))
        now = datetime.now(timezone.utc)
        for row in rows:
            rs = str((row or {}).get("signature") or "")
            rs_set = set(rs.split())
            if not rs_set:
                continue
            overlap = len(sig_set.intersection(rs_set))
            union = len(sig_set.union(rs_set))
            sim = float(overlap) / float(max(1, union))
            if sim <= 0.0:
                continue
            row_success = max(0.0, min(1.0, float((row or {}).get("success_rate") or 0.5)))
            weighted += sim * row_success
            weight_sum += sim

            if rs == sig and isinstance((row or {}).get("plan_stats"), dict) and key:
                stat = (row.get("plan_stats") or {}).get(key)
                if isinstance(stat, dict):
                    exact_plan_success_rate = max(0.0, min(1.0, float((stat or {}).get("success_rate") or row_success)))
                    fails = (stat or {}).get("recent_failures") if isinstance((stat or {}).get("recent_failures"), list) else []
                    recent_fail_count = 0
                    for stamp in fails:
                        try:
                            ts = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            age_h = (now - ts).total_seconds() / 3600.0
                            if age_h <= 12.0:
                                recent_fail_count += 1
                        except Exception:
                            continue
                    recent_failure_penalty = min(0.35, float(recent_fail_count) * 0.12)
    except Exception:
        pass

    similar_success_probability = weighted / weight_sum if weight_sum > 0 else 0.5
    past_success_rate = exact_plan_success_rate if exact_plan_success_rate >= 0 else similar_success_probability
    return {
        "similar_success_probability": max(0.0, min(1.0, float(similar_success_probability))),
        "past_success_rate": max(0.0, min(1.0, float(past_success_rate))),
        "recent_failure_penalty": max(0.0, min(0.5, float(recent_failure_penalty))),
    }


def _score_plan(
    *,
    source_text: str,
    steps: list[dict[str, Any]],
    device_id: str | None,
    agent_online: bool,
    agent_temporarily_unavailable: bool,
    recent_device_error_rate: float,
) -> dict[str, float]:
    valid_steps = [s for s in (steps or []) if isinstance(s, dict)]
    step_count = len(valid_steps)
    if step_count <= 0:
        return {
            "success_probability": 0.0,
            "step_complexity": 1.0,
            "dependency_risk": 1.0,
            "past_success_rate": 0.0,
            "overall": 0.0,
        }

    history = _plan_history_stats(source_text, valid_steps)
    action_weight = sum(_action_complexity_weight(str((s or {}).get("action") or "")) for s in valid_steps)
    avg_action_weight = action_weight / float(max(1, step_count))
    step_complexity = max(0.0, min(1.0, (min(8, step_count) / 8.0) * 0.6 + (min(2.5, avg_action_weight) / 2.5) * 0.4))

    dep_count = 0
    chain_like = 0
    for s in valid_steps:
        dep = str((s or {}).get("depends_on") or "").strip()
        if dep:
            dep_count += 1
            if dep.startswith("step_"):
                chain_like += 1
    dependency_risk = max(0.0, min(1.0, (float(dep_count) / float(max(1, step_count))) * 0.7 + (float(chain_like) / float(max(1, step_count))) * 0.3))

    context_penalty = 0.0
    if not agent_online and step_count > 1:
        context_penalty += 0.18
    if agent_temporarily_unavailable:
        context_penalty += 0.14
    context_penalty += min(0.2, max(0.0, float(recent_device_error_rate)) * 0.2)

    past_success_rate = max(0.0, min(1.0, float(history.get("past_success_rate") or 0.5)))
    success_probability = (
        (float(history.get("similar_success_probability") or 0.5) * 0.58)
        + (past_success_rate * 0.3)
        + ((1.0 - dependency_risk) * 0.12)
        - float(history.get("recent_failure_penalty") or 0.0)
        - context_penalty
    )
    success_probability = max(0.0, min(1.0, success_probability))

    overall = (
        (success_probability * 0.45)
        + (past_success_rate * 0.25)
        + ((1.0 - step_complexity) * 0.2)
        + ((1.0 - dependency_risk) * 0.1)
    )
    overall = max(0.0, min(1.0, overall))
    return {
        "success_probability": round(success_probability, 4),
        "step_complexity": round(step_complexity, 4),
        "dependency_risk": round(dependency_risk, 4),
        "past_success_rate": round(past_success_rate, 4),
        "overall": round(overall, 4),
    }


def _save_plan_learning(
    source_text: str,
    steps: list[dict[str, Any]],
    *,
    success: bool,
    execution_time_ms: float | None = None,
    retries_used: int = 0,
    score: dict[str, Any] | None = None,
) -> None:
    try:
        col = _plan_learning_collection()
        if col is None:
            return
        sig = _plan_signature(source_text)
        if not sig or not isinstance(steps, list) or not steps:
            return
        now = datetime.now(timezone.utc)
        col.update_one(
            {"signature": sig},
            {
                "$set": {
                    "signature": sig,
                    "source_example": str(source_text or "")[:240],
                    "steps": [s for s in steps if isinstance(s, dict)],
                    "updated_at": now,
                    "last_outcome": "success" if bool(success) else "failed",
                    "last_execution_time_ms": round(float(execution_time_ms), 2) if execution_time_ms is not None else None,
                    "last_retries_used": max(0, int(retries_used or 0)),
                    "last_plan_score": dict(score or {}),
                },
                "$setOnInsert": {"created_at": now},
                "$inc": {"attempts": 1, "successes": 1 if success else 0},
            },
            upsert=True,
        )
        row = col.find_one({"signature": sig}, {"_id": 0, "attempts": 1, "successes": 1}) or {}
        attempts = max(1, int(row.get("attempts") or 1))
        successes = int(row.get("successes") or 0)
        col.update_one({"signature": sig}, {"$set": {"success_rate": float(successes) / float(attempts)}})

        # Per-plan learning lets the planner avoid repeating weak strategies for the same intent.
        plan_key = _plan_steps_key(steps)
        if plan_key:
            row = col.find_one({"signature": sig}, {"_id": 0, "plan_stats": 1}) or {}
            stats = row.get("plan_stats") if isinstance(row.get("plan_stats"), dict) else {}
            cur = stats.get(plan_key) if isinstance(stats.get(plan_key), dict) else {}
            pa = int(cur.get("attempts") or 0) + 1
            ps = int(cur.get("successes") or 0) + (1 if success else 0)
            prev_avg_ms = float(cur.get("avg_execution_time_ms") or 0.0)
            prev_cnt = int(cur.get("timing_samples") or 0)
            if execution_time_ms is None:
                next_avg_ms = prev_avg_ms
                next_cnt = prev_cnt
            else:
                next_cnt = prev_cnt + 1
                next_avg_ms = ((prev_avg_ms * prev_cnt) + float(execution_time_ms)) / float(max(1, next_cnt))
            recent_failures = cur.get("recent_failures") if isinstance(cur.get("recent_failures"), list) else []
            if success:
                recent_failures = recent_failures[-3:]
            else:
                recent_failures.append(now.isoformat())
                recent_failures = recent_failures[-10:]

            stats[plan_key] = {
                "attempts": pa,
                "successes": ps,
                "success_rate": float(ps) / float(max(1, pa)),
                "avg_execution_time_ms": round(float(next_avg_ms), 2),
                "timing_samples": next_cnt,
                "recent_failures": recent_failures,
                "updated_at": now,
            }
            col.update_one({"signature": sig}, {"$set": {"plan_stats": stats, "last_plan_key": plan_key}})
    except Exception:
        return


def _normalize_plan_action(action: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if isinstance(action.get("action"), str):
        an = str(action.get("action") or "").strip()
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        return an, dict(params)
    an = str(action.get("type") or "").strip()
    params = {k: v for k, v in (action or {}).items() if k not in {"type", "task_id", "step_id", "depends_on"} and v is not None}
    return an, params


def _derive_dynamic_plan(source_text: str, actions: list[dict[str, Any]], *, prefer_reuse: bool = True) -> list[dict[str, Any]]:
    if prefer_reuse:
        reusable = _load_reusable_plan(source_text)
        if reusable:
            logger.info("[PLAN] Reused learned plan: signature=%s steps=%s", _plan_signature(source_text), len(reusable))
            return reusable

    steps: list[dict[str, Any]] = []
    incoming = [a for a in (actions or []) if isinstance(a, dict)]
    text = str(source_text or "").strip()
    tl = text.lower()

    if incoming:
        for idx, a in enumerate(incoming):
            action_name, params = _normalize_plan_action(a)
            if not action_name:
                continue
            steps.append(
                {
                    "step": idx + 1,
                    "step_id": str(a.get("step_id") or f"step_{idx + 1}"),
                    "depends_on": str(a.get("depends_on") or "").strip() or (f"step_{idx}" if idx > 0 else None),
                    "action": action_name,
                    "params": params,
                    "retry_once": True,
                }
            )

    # Dynamic intent/entity extraction fallback when model actions are sparse.
    if not steps:
        url_match = re.search(r"https?://\S+", text, flags=re.IGNORECASE)
        if url_match:
            steps.append({"step": 1, "step_id": "step_1", "depends_on": None, "action": "open_url", "params": {"url": url_match.group(0)}, "retry_once": True})

        app_match = re.search(r"\b(?:open|launch|start)\s+([a-z0-9_ .-]{2,40})", text, flags=re.IGNORECASE)
        if app_match:
            app = str(app_match.group(1) or "").strip(" .,!?")
            if app:
                steps.append(
                    {
                        "step": len(steps) + 1,
                        "step_id": f"step_{len(steps) + 1}",
                        "depends_on": f"step_{len(steps)}" if steps else None,
                        "action": "open_app",
                        "params": {"app_name": app},
                        "retry_once": True,
                    }
                )

        q_match = re.search(r"\b(?:search(?:\s+for)?|look\s+up|find)\s+(.+?)(?:$|\s+and\b)", text, flags=re.IGNORECASE)
        if q_match:
            q = str(q_match.group(1) or "").strip(" .,!?")
            if q:
                steps.append(
                    {
                        "step": len(steps) + 1,
                        "step_id": f"step_{len(steps) + 1}",
                        "depends_on": f"step_{len(steps)}" if steps else None,
                        "action": "open_url",
                        "params": {"url": f"https://www.google.com/search?q={quote_plus(q)}"},
                        "retry_once": True,
                    }
                )

    # Adaptive fallback suggestions by action type.
    for s in steps:
        act = str((s or {}).get("action") or "").strip().lower()
        if act == "open_app":
            s["fallback"] = {"action": "open_url", "params": {"url": "https://www.google.com"}}

    logger.info("[PLAN] Generated steps: %s", [f"{i + 1}:{str((x or {}).get('action') or '').strip()}" for i, x in enumerate(steps)])
    return steps


def _build_direct_plan_variant(source_text: str, base_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = str(source_text or "").strip()
    # Prefer one-hop execution when a clear URL exists.
    url_match = re.search(r"https?://\S+", text, flags=re.IGNORECASE)
    if url_match:
        return [{"step": 1, "step_id": "step_1", "depends_on": None, "action": "open_url", "params": {"url": url_match.group(0)}, "retry_once": True}]

    for step in base_steps:
        if not isinstance(step, dict):
            continue
        if str((step or {}).get("action") or "").strip().lower() == "open_url":
            params = (step or {}).get("params") if isinstance((step or {}).get("params"), dict) else {}
            url = str(params.get("url") or "").strip()
            if url:
                return [{"step": 1, "step_id": "step_1", "depends_on": None, "action": "open_url", "params": {"url": url}, "retry_once": True}]

    q_match = re.search(r"\b(?:search(?:\s+for)?|look\s+up|find)\s+(.+?)(?:$|\s+and\b)", text, flags=re.IGNORECASE)
    if q_match:
        q = str(q_match.group(1) or "").strip(" .,!?")
        if q:
            return [{"step": 1, "step_id": "step_1", "depends_on": None, "action": "open_url", "params": {"url": f"https://www.google.com/search?q={quote_plus(q)}"}, "retry_once": True}]
    return []


def _collect_plan_options(source_text: str, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []

    primary = _derive_dynamic_plan(source_text, actions, prefer_reuse=True)
    generated = _derive_dynamic_plan(source_text, actions, prefer_reuse=False)
    reusable = _load_reusable_plan(source_text)
    direct = _build_direct_plan_variant(source_text, generated or primary)

    seeds = [
        ("A", "primary", primary),
        ("B", "direct", direct),
        ("C", "learned", reusable),
        ("D", "generated", generated),
    ]
    seen: set[str] = set()
    for label, source, steps in seeds:
        valid = [s for s in (steps or []) if isinstance(s, dict)]
        if not valid:
            continue
        key = _plan_steps_key(valid)
        if not key or key in seen:
            continue
        seen.add(key)
        options.append({"id": label, "source": source, "steps": valid, "metadata": {}})
        if len(options) >= 3:
            break

    if len(options) < 2 and options:
        one_step = [dict(options[0]["steps"][0])] if options[0].get("steps") else []
        if one_step:
            key = _plan_steps_key(one_step)
            if key and key not in seen:
                options.append({"id": "F", "source": "fallback_single_step", "steps": one_step, "metadata": {}})

    return options


def _select_best_plan_option(
    *,
    source_text: str,
    options: list[dict[str, Any]],
    device_id: str | None,
    username: str | None,
    agent_online: bool,
    agent_temporarily_unavailable: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not options:
        return None, []

    error_rate = _recent_device_error_rate(device_id=device_id, username=username)
    scored: list[dict[str, Any]] = []
    for item in options:
        steps = item.get("steps") if isinstance(item.get("steps"), list) else []
        score = _score_plan(
            source_text=source_text,
            steps=[s for s in steps if isinstance(s, dict)],
            device_id=device_id,
            agent_online=bool(agent_online),
            agent_temporarily_unavailable=bool(agent_temporarily_unavailable),
            recent_device_error_rate=error_rate,
        )
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        meta["score"] = score
        item["metadata"] = meta
        scored.append(item)

    scored.sort(key=lambda x: float(((x.get("metadata") or {}).get("score") or {}).get("overall") or 0.0), reverse=True)

    logger.info("[PLAN OPTIONS] count=%s signature=%s", len(scored), _plan_signature(source_text))
    for item in scored:
        sid = str(item.get("id") or "?")
        score = (item.get("metadata") or {}).get("score") if isinstance((item.get("metadata") or {}).get("score"), dict) else {}
        logger.info(
            "[PLAN SCORE] Plan %s source=%s overall=%.4f success_probability=%.4f step_complexity=%.4f dependency_risk=%.4f past_success_rate=%.4f",
            sid,
            str(item.get("source") or "dynamic"),
            float(score.get("overall") or 0.0),
            float(score.get("success_probability") or 0.0),
            float(score.get("step_complexity") or 0.0),
            float(score.get("dependency_risk") or 0.0),
            float(score.get("past_success_rate") or 0.0),
        )

    selected = scored[0] if scored else None
    if selected is not None:
        logger.info("[SELECTED PLAN] %s source=%s overall=%.4f", str(selected.get("id") or "A"), str(selected.get("source") or "dynamic"), float((((selected.get("metadata") or {}).get("score") or {}).get("overall") or 0.0)))
    return selected, scored


async def _execute_dynamic_plan(
    *,
    device_id: str,
    username: str,
    source_text: str,
    task_id: str,
    plan_steps: list[dict[str, Any]],
    await_timeout_s: float,
) -> tuple[dict[str, Any] | None, str | None]:
    started_at = time.perf_counter()
    retries_used = 0
    results: list[dict[str, Any]] = []
    if not plan_steps:
        return None, "empty_plan"

    for idx, step in enumerate(plan_steps):
        action_name = str((step or {}).get("action") or "").strip()
        params = (step or {}).get("params") if isinstance((step or {}).get("params"), dict) else {}
        if not action_name:
            return None, "invalid_plan_step"

        _mark_delegated_task(task_id=task_id, status_value="executing", extra={"current_step": idx, "active_step": action_name})
        logger.info("[STEP %s] %s", idx + 1, action_name)

        async def _run_step(run_action: str, run_params: dict[str, Any], attempt: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            payload = {
                "action": run_action,
                "params": run_params,
                "task_id": f"{task_id}:{str((step or {}).get('step_id') or f'step_{idx + 1}')}",
                "step_id": str((step or {}).get("step_id") or f"step_{idx + 1}"),
                "depends_on": str((step or {}).get("depends_on") or "").strip() or (f"step_{idx}" if idx > 0 else None),
            }
            job = await _dispatch_actions_to_device(device_id, username=username, actions=[payload], source_text=source_text)
            _mark_delegated_task(task_id=task_id, status_value="delegated", extra={"last_job_id": job.get("job_id"), "current_step": idx, "attempts": int(attempt)})
            res = await _await_job_result(str((job or {}).get("job_id") or ""), timeout_s=await_timeout_s)
            return job, res

        success = False
        final_entry: dict[str, Any] = {}
        for attempt in (1, 2):
            job, payload = await _run_step(action_name, params, attempt)
            if attempt > 1:
                retries_used += 1
            if not payload:
                final_entry = {
                    "status": "failed",
                    "error": "agent_response_timeout",
                    "task_id": f"{task_id}:{idx + 1}",
                    "action": action_name,
                }
            else:
                first = _extract_agent_contract_entry(payload)
                s = str((first or {}).get("status") or "").strip().lower()
                mapped = "completed" if s in {"success", "ok", "completed"} else "failed"
                final_entry = dict(first or {})
                final_entry["status"] = mapped

            if str(final_entry.get("status") or "").lower() == "completed":
                success = True
                break
            if attempt == 1 and not bool((step or {}).get("retry_once", True)):
                break

        if not success:
            fb = (step or {}).get("fallback") if isinstance((step or {}).get("fallback"), dict) else None
            if fb:
                fb_action = str((fb or {}).get("action") or "").strip()
                fb_params = (fb or {}).get("params") if isinstance((fb or {}).get("params"), dict) else {}
                try:
                    job, payload = await _run_step(fb_action, fb_params, 1)
                    retries_used += 1
                    first = _extract_agent_contract_entry(payload) if isinstance(payload, dict) else {}
                    s = str((first or {}).get("status") or "").strip().lower()
                    if s in {"success", "ok", "completed"}:
                        success = True
                        final_entry = dict(first or {})
                        final_entry["status"] = "completed"
                        final_entry["fallback_used"] = True
                except Exception:
                    pass

        results.append(final_entry)
        if not success:
            elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
            return {
                "type": "result",
                "job_id": f"plan_{task_id}",
                "results": results,
                "plan_metrics": {
                    "execution_time_ms": elapsed_ms,
                    "retries_used": retries_used,
                    "steps_total": len(plan_steps),
                    "success": False,
                },
            }, str(final_entry.get("error") or "step_failed")

    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
    return {
        "type": "result",
        "job_id": f"plan_{task_id}",
        "results": results,
        "plan_metrics": {
            "execution_time_ms": elapsed_ms,
            "retries_used": retries_used,
            "steps_total": len(plan_steps),
            "success": True,
        },
    }, None


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


def _extract_agent_contract_entry(payload: dict[str, Any] | None) -> dict[str, Any]:
    rows = (payload or {}).get("results") or []
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return dict(rows[0])
    return {}


def _extract_agent_contract_result(payload: dict[str, Any] | None) -> dict[str, Any]:
    first = _extract_agent_contract_entry(payload)
    result = first.get("result")
    if isinstance(result, dict):
        return _to_json_safe(dict(result))
    # Backward compatibility with legacy agent shape.
    if first:
        return _to_json_safe(dict(first))
    return {}


def _cloud_envelope(*, status: str, execution: Any, result: Any = None, message: str = "") -> dict[str, Any]:
    return {
        "status": _normalize_flow_status(status, default="delegated"),
        "mode": "cloud",
        "execution": _to_json_safe(execution),
        "result": _to_json_safe(result) if result is not None else None,
        "message": str(message or "").strip() or None,
    }


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
    except asyncio.TimeoutError:
        _ops_inc("timeout_total")
        pass
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
    plan_steps: list[dict[str, Any]] | None = None,
    plan_source: str | None = None,
    current_step: int = 0,
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
        "steps": [s for s in (plan_steps or []) if isinstance(s, dict)],
        "current_step": max(0, int(current_step or 0)),
        "plan_source": str(plan_source or "dynamic").strip() or "dynamic",
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
            _retry_or_fail_delegated_task(row.get("task_id"), reason=str(e), fallback_status="queued_for_agent")


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
            _retry_or_fail_delegated_task(row.get("task_id"), reason=str(e), fallback_status="pending_permission")


async def _delegate_or_queue_cloud_action(
    *,
    session_id: str | None,
    feature: str,
    actions: list[dict[str, Any]],
    source_text: str,
    require_admin: bool = False,
    await_timeout_s: float = GLOBAL_AGENT_RESPONSE_TIMEOUT_S,
) -> dict[str, Any]:
    _require_pc_agent_enabled()
    executable_actions, blocked_actions = _split_agent_executable_actions(actions)
    if blocked_actions:
        out = _cloud_envelope(
            status="failed",
            execution={"feature": feature, "blocked_actions": blocked_actions},
            result=None,
            message="This request includes generation/planning actions. Generate content first, then delegate executable device actions.",
        )
        out["feature"] = feature
        out["blocked_actions"] = blocked_actions
        return out

    actions = executable_actions
    plan_options = _collect_plan_options(source_text, actions)
    if not plan_options and actions:
        fallback_steps = _derive_dynamic_plan("", actions, prefer_reuse=False)
        if fallback_steps:
            plan_options = [{"id": "A", "source": "generated", "steps": fallback_steps, "metadata": {}}]

    selected_option = plan_options[0] if plan_options else None
    scored_options = list(plan_options)
    plan_steps = [s for s in ((selected_option or {}).get("steps") or []) if isinstance(s, dict)]
    selected_score = ((selected_option or {}).get("metadata") or {}).get("score") if isinstance(((selected_option or {}).get("metadata") or {}).get("score"), dict) else {}

    principal = _require_admin_session(session_id) if require_admin else _require_authenticated_session(session_id)
    username = str((principal or {}).get("username") or "").strip().lower()
    role = str((principal or {}).get("role") or "user").strip().lower()

    did = _get_owner_device_id(username)
    if (not did) and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
        did = DEFAULT_DEVICE_ID
    if (not did) and role == "admin":
        did = DEFAULT_DEVICE_ID

    if did:
        agent_online_now = await device_hub.is_connected(did)
        selected_option, scored_options = _select_best_plan_option(
            source_text=source_text,
            options=plan_options,
            device_id=did,
            username=username,
            agent_online=bool(agent_online_now),
            agent_temporarily_unavailable=_is_agent_temporarily_unavailable(did),
        )
        if selected_option is not None:
            plan_steps = [s for s in ((selected_option or {}).get("steps") or []) if isinstance(s, dict)]
            selected_score = ((selected_option or {}).get("metadata") or {}).get("score") if isinstance(((selected_option or {}).get("metadata") or {}).get("score"), dict) else {}

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
            plan_steps=plan_steps,
            plan_source=str((selected_option or {}).get("source") or "dynamic"),
        )
        out = _cloud_envelope(
            status="awaiting_agent",
            execution={"feature": feature, "task": task},
            result=None,
            message="No device is assigned yet. Task is waiting for agent assignment.",
        )
        out["feature"] = feature
        out["task"] = task
        out["plan"] = plan_steps
        out["plan_score"] = selected_score
        return out

    if _is_agent_temporarily_unavailable(did):
        queued_steps = plan_steps[:1] if len(plan_steps) > 1 else plan_steps
        task = _queue_delegated_task(
            username=username,
            role=role,
            device_id=did,
            feature=feature,
            source_text=source_text,
            actions=actions,
            status_value="queued_for_agent",
            reason="agent_circuit_open",
            plan_steps=queued_steps,
            plan_source=str((selected_option or {}).get("source") or "dynamic"),
        )
        out = _cloud_envelope(
            status="queued_for_agent",
            execution={"feature": feature, "device_id": did, "task": task},
            result=None,
            message="PC agent temporarily unavailable. Using fallback single-step strategy and queueing for retry after cooldown.",
        )
        out["feature"] = feature
        out["device_id"] = did
        out["task"] = task
        out["plan"] = queued_steps
        out["plan_score"] = selected_score
        return out

    if not await device_hub.is_connected(did):
        queued_steps = plan_steps[:1] if len(plan_steps) > 1 else plan_steps
        task = _queue_delegated_task(
            username=username,
            role=role,
            device_id=did,
            feature=feature,
            source_text=source_text,
            actions=actions,
            status_value="queued_for_agent",
            reason="agent_offline",
            plan_steps=queued_steps,
            plan_source=str((selected_option or {}).get("source") or "dynamic"),
        )
        out = _cloud_envelope(
            status="queued_for_agent",
            execution={"feature": feature, "device_id": did, "task": task},
            result=None,
            message="PC agent is offline. Multi-step execution avoided; fallback strategy queued for resume after reconnect.",
        )
        out["feature"] = feature
        out["device_id"] = did
        out["task"] = task
        out["plan"] = queued_steps
        out["plan_score"] = selected_score
        return out

    running_task = _queue_delegated_task(
        username=username,
        role=role,
        device_id=did,
        feature=feature,
        source_text=source_text,
        actions=actions,
        plan_steps=plan_steps,
        plan_source=str((selected_option or {}).get("source") or "dynamic"),
        current_step=0,
        status_value="executing",
        reason="plan_generated",
    )
    payload, plan_error = await _execute_dynamic_plan(
        device_id=did,
        username=username or "user",
        source_text=source_text,
        task_id=str(running_task.get("task_id") or ""),
        plan_steps=plan_steps,
        await_timeout_s=await_timeout_s,
    )
    if not payload:
        _mark_agent_unavailable(did, reason="agent_response_timeout")
        _retry_or_fail_delegated_task(running_task.get("task_id"), reason=str(plan_error or "agent_response_timeout"), fallback_status="queued_for_agent")
        out = _cloud_envelope(
            status="queued_for_agent",
            execution={"feature": feature, "device_id": did, "plan": plan_steps},
            result=None,
            message="PC agent response timeout. Task queued for automatic retry.",
        )
        out["feature"] = feature
        out["device_id"] = did
        out["plan"] = plan_steps
        out["plan_score"] = selected_score
        return out

    step_results = (payload or {}).get("results") if isinstance((payload or {}).get("results"), list) else []
    failed_entry = None
    for r in step_results:
        if not isinstance(r, dict):
            continue
        st = str((r or {}).get("status") or "").strip().lower()
        if st in {"failed", "error", "forbidden"}:
            failed_entry = r
            break

    # Intelligent fallback: if selected plan failed, try next best options before final failure.
    if failed_entry and scored_options:
        selected_key = _plan_steps_key(plan_steps)
        for option in scored_options:
            candidate_steps = [s for s in ((option or {}).get("steps") or []) if isinstance(s, dict)]
            if not candidate_steps or _plan_steps_key(candidate_steps) == selected_key:
                continue
            logger.warning("[PLAN FALLBACK] trying alternative=%s source=%s", str(option.get("id") or "?"), str(option.get("source") or "dynamic"))
            alt_payload, alt_error = await _execute_dynamic_plan(
                device_id=did,
                username=username or "user",
                source_text=source_text,
                task_id=str(running_task.get("task_id") or ""),
                plan_steps=candidate_steps,
                await_timeout_s=await_timeout_s,
            )
            alt_results = (alt_payload or {}).get("results") if isinstance((alt_payload or {}).get("results"), list) else []
            alt_failed = None
            for rr in alt_results:
                if not isinstance(rr, dict):
                    continue
                s2 = str((rr or {}).get("status") or "").strip().lower()
                if s2 in {"failed", "error", "forbidden"}:
                    alt_failed = rr
                    break
            if alt_payload and not alt_failed and not alt_error:
                payload = alt_payload
                plan_error = None
                plan_steps = candidate_steps
                selected_option = option
                selected_score = ((option or {}).get("metadata") or {}).get("score") if isinstance(((option or {}).get("metadata") or {}).get("score"), dict) else {}
                failed_entry = None
                step_results = alt_results
                break

    first_status = "failed" if failed_entry else "completed"
    result_body = {}
    if step_results:
        last = step_results[-1] if isinstance(step_results[-1], dict) else {}
        if isinstance(last.get("result"), dict):
            result_body = _to_json_safe(last.get("result"))
        else:
            result_body = _to_json_safe(last)

    out = _cloud_envelope(
        status=first_status,
        execution={"feature": feature, "device_id": did, "plan": plan_steps, "agent_result": payload},
        result=result_body,
        message="PC agent execution completed." if first_status == "completed" else "PC agent execution finished with errors.",
    )
    out["feature"] = feature
    out["device_id"] = did
    out["plan"] = plan_steps
    out["plan_score"] = selected_score
    out["agent_result"] = payload
    if scored_options:
        out["plan_options"] = [
            {
                "id": str(o.get("id") or "?"),
                "source": str(o.get("source") or "dynamic"),
                "score": dict(((o.get("metadata") or {}).get("score") or {})),
            }
            for o in scored_options
        ]

    plan_metrics = (payload or {}).get("plan_metrics") if isinstance((payload or {}).get("plan_metrics"), dict) else {}
    exec_time_ms = float(plan_metrics.get("execution_time_ms") or 0.0)
    retries_used = int(plan_metrics.get("retries_used") or 0)

    if first_status == "failed":
        failure_reason = str((failed_entry or {}).get("error") or plan_error or "agent_execution_failed")
        _retry_or_fail_delegated_task(running_task.get("task_id"), reason=failure_reason)
        _save_plan_learning(source_text, plan_steps, success=False, execution_time_ms=exec_time_ms, retries_used=retries_used, score=selected_score)
        out["suggestion"] = "Plan step failed. Try re-running or allow fallback action permissions."
    else:
        _mark_delegated_task(
            task_id=running_task.get("task_id"),
            status_value="completed",
            extra={
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result_preview": _truncate_notification_payload((payload or {}).get("results") or []),
                "current_step": len(plan_steps),
            },
        )
        _clear_agent_unavailable(did)
        _save_plan_learning(source_text, plan_steps, success=True, execution_time_ms=exec_time_ms, retries_used=retries_used, score=selected_score)
    return out


def _delegated_first_result(delegated: dict[str, Any]) -> dict[str, Any] | None:
    payload = delegated.get("agent_result") if isinstance(delegated.get("agent_result"), dict) else {}
    first = _extract_agent_contract_result(payload)
    if isinstance(first, dict) and first:
        return first
    # fallback: some callers may already pass normalized cloud envelope result
    direct = delegated.get("result")
    if isinstance(direct, dict):
        return _to_json_safe(dict(direct))
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


# Prefer the persisted DB shared secret when available.
# If no DB value exists, keep the local/dev fallback so the UI can still bootstrap.
AGENT_SHARED_SECRET = _load_agent_shared_secret_from_db()
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
auth_tokens = AuthTokens()
# Local/dev convenience: agent token issuance requires JARVIS_JWT_SECRET.
# In cloud mode this must be explicitly configured; in local mode we can
# generate a per-run secret so PC agent pairing works out of the box.
if (not CLOUD_MODE) and (not (auth_tokens.secret or "").strip()):
    try:
        auth_tokens.secret = secrets.token_urlsafe(48)
    except Exception:
        auth_tokens.secret = os.urandom(48).hex()

PUBLIC_SERVER_URL = ""
AGENT_TOKEN_TTL_SECONDS = 2592000  # 30d


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

        # New normalized schema write (keeps legacy collection intact).
        try:
            if callable(normalize_training_doc) and hasattr(database, "db") and database.db is not None:
                n = normalize_training_doc(
                    "requirement_logs",
                    {
                        "timestamp": payload.get("ts"),
                        "user_id": payload.get("user_id") or "user",
                        "session_id": (details or {}).get("session_id") if isinstance(details, dict) else "unknown",
                        "correlation_id": (details or {}).get("request_id") if isinstance(details, dict) else None,
                        "source": "system",
                        "mode": "cloud" if CLOUD_MODE else "local",
                        "lifecycle_state": payload.get("status") or "pending",
                        "type": payload.get("requirement_type") or "requirement",
                        "message": payload.get("requested_action") or payload.get("target") or "requirement",
                        "legacy_payload": payload,
                    },
                )
                database.db["requirement_logs"].update_one(
                    {"event_id": n.get("event_id")},
                    {"$set": n},
                    upsert=True,
                )
        except Exception:
            pass
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


def _learn_user_behavior_preferences(*, user_id: str | None, user_text: str, response_text: str, actions: list[dict[str, Any]] | None) -> None:
    uid = _normalize_user_id(user_id)
    if not uid:
        return
    try:
        col = _user_prefs_collection()
    except Exception:
        return

    try:
        doc = col.find_one({"user_id": uid}, {"_id": 0, "preferences": 1}) or {}
        prefs = doc.get("preferences") if isinstance(doc.get("preferences"), dict) else {}
        behavior = prefs.get("behavior") if isinstance(prefs.get("behavior"), dict) else {}

        execute_requests = int(behavior.get("execution_requests") or 0)
        explanation_requests = int(behavior.get("explanation_requests") or 0)
        short_pref_signals = int(behavior.get("short_pref_signals") or 0)
        detailed_pref_signals = int(behavior.get("detailed_pref_signals") or 0)

        t = str(user_text or "").strip().lower()
        has_actions = bool(isinstance(actions, list) and actions)
        asks_execution = bool(re.search(r"\b(open|run|execute|launch|start|do\s+it|perform)\b", t))
        asks_explanation = bool(re.search(r"\b(what|why|how|explain|overview|define)\b", t))
        asks_short = bool(re.search(r"\b(short|brief|concise|one\s+line)\b", t))
        asks_detailed = bool(re.search(r"\b(detailed|step\s+by\s+step|deep\s+dive|more\s+detail)\b", t))

        if asks_execution or has_actions:
            execute_requests += 1
        if asks_explanation and not has_actions:
            explanation_requests += 1
        if asks_short:
            short_pref_signals += 1
        if asks_detailed:
            detailed_pref_signals += 1

        behavior.update(
            {
                "execution_requests": execute_requests,
                "explanation_requests": explanation_requests,
                "short_pref_signals": short_pref_signals,
                "detailed_pref_signals": detailed_pref_signals,
                "last_seen_at": datetime.utcnow(),
            }
        )

        prefs["behavior"] = behavior
        prefs["prefers_execution"] = bool(execute_requests >= (explanation_requests + 2))
        if detailed_pref_signals > short_pref_signals:
            prefs["verbosity"] = "high"
        elif short_pref_signals > detailed_pref_signals:
            prefs["verbosity"] = "low"

        col.update_one(
            {"user_id": uid},
            {
                "$set": {
                    "preferences": prefs,
                    "updated_at": datetime.utcnow(),
                },
                "$setOnInsert": {
                    "user_id": uid,
                    "created_at": datetime.utcnow(),
                },
            },
            upsert=True,
        )
    except Exception:
        return


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
        "execute_command", "run_command",
        "open_url",
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


def _is_generation_or_thinking_action(a: dict) -> bool:
    t = str((a or {}).get("type") or "").strip().lower()
    if t in {"generate_email", "generate_content", "summarize", "analyze", "think", "reason"}:
        return True
    # mcp_tool and similar meta-tools should not be sent to device execution agent.
    if t in {"mcp_tool", "set_mode"}:
        return True
    return False


def _split_agent_executable_actions(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    executable: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        if _is_generation_or_thinking_action(a):
            blocked.append(a)
            continue
        if not _is_remote_device_action(a):
            blocked.append(a)
            continue
        executable.append(a)
    return executable, blocked


def _extract_chain_search_query(source_text: str) -> str:
    t = str(source_text or "").strip()
    if not t:
        return ""
    m = re.search(r"\b(?:search(?:\s+for)?|look\s+up|find)\s+(.+?)(?:\s*(?:and\s+then|then)\b|$)", t, flags=re.IGNORECASE)
    if not m:
        return ""
    q = str(m.group(1) or "").strip(" .,!?")
    return q


def _expand_multi_step_chain_actions(source_text: str, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add minimal chained actions for common multi-step intents without changing flow architecture."""
    out = [a for a in (actions or []) if isinstance(a, dict)]
    t = str(source_text or "").strip().lower()
    if not t:
        return out

    has_open_app = any(str((a or {}).get("type") or "").strip().lower() == "open_app" for a in out)
    has_open_url = any(str((a or {}).get("type") or "").strip().lower() == "open_url" for a in out)
    search_q = _extract_chain_search_query(source_text)

    # Rule: open -> search -> action
    if search_q and has_open_app and not has_open_url:
        out.append({
            "type": "open_url",
            "url": f"https://www.google.com/search?q={quote_plus(search_q)}",
            "chain_reason": "open_then_search",
        })

    # Rule: open youtube -> play/search
    if "youtube" in t and ("play" in t or "music" in t):
        if not has_open_url:
            out.append({"type": "open_url", "url": "https://www.youtube.com", "chain_reason": "open_then_action"})
        play_q = ""
        m = re.search(r"\bplay\s+(.+?)(?:\s*(?:on\s+youtube|in\s+youtube)|$)", str(source_text or ""), flags=re.IGNORECASE)
        if m:
            play_q = str(m.group(1) or "").strip(" .,!?")
        if not play_q and "music" in t:
            play_q = "music"
        has_youtube_search_url = any(
            "youtube.com/results" in str((a or {}).get("url") or "").lower()
            for a in out
            if str((a or {}).get("type") or "").strip().lower() == "open_url"
        )
        if play_q and not has_youtube_search_url:
            out.append(
                {
                    "type": "open_url",
                    "url": f"https://www.youtube.com/results?search_query={quote_plus(play_q)}",
                    "chain_reason": "open_then_search_then_action",
                }
            )

    return out


def _validate_action_params(action_name: str, params: dict[str, Any]) -> None:
    name = str(action_name or "").strip().lower()
    req = {
        "open_url": ["url"],
        "open_app": ["app_name", "app"],
        "execute_command": ["command", "cmd"],
        "run_command": ["command", "cmd"],
    }
    required_keys = req.get(name) or []
    if not required_keys:
        return
    for k in required_keys:
        v = (params or {}).get(k)
        if isinstance(v, str) and v.strip():
            return
        if v not in (None, ""):
            return
    raise ValueError(f"missing_required_params_for_action:{name}")

async def _dispatch_actions_to_device(device_id: str, username: str, actions: list[dict], source_text: str):
    """Forward actions to a connected local agent."""
    _require_pc_agent_enabled()
    has_explicit_plan_step = any(
        isinstance(a, dict) and (a.get("step_id") is not None or a.get("depends_on") is not None)
        for a in (actions or [])
    )
    expanded_actions = actions if has_explicit_plan_step else _expand_multi_step_chain_actions(source_text, actions)
    executable_actions, blocked_actions = _split_agent_executable_actions(expanded_actions)
    if blocked_actions:
        blocked_types = [str((a or {}).get("type") or "") for a in blocked_actions]
        raise ValueError(f"non_executable_actions_for_agent: {blocked_types}")

    actions = executable_actions
    job_id = f"job_{os.urandom(8).hex()}"

    contract_actions: list[dict[str, Any]] = []
    for idx, a in enumerate(actions or []):
        if not isinstance(a, dict):
            continue
        if isinstance(a.get("action"), str):
            action_name = str(a.get("action") or "").strip()
            params = a.get("params") if isinstance(a.get("params"), dict) else {}
            params = {k: v for k, v in params.items() if v is not None}
            _validate_action_params(action_name, params)
            contract_actions.append(
                {
                    "action": action_name,
                    "params": params,
                    "task_id": str(a.get("task_id") or f"{job_id}:{idx}").strip(),
                    "step_id": str(a.get("step_id") or f"step_{idx + 1}").strip(),
                    "depends_on": str(a.get("depends_on") or "").strip() or (f"step_{idx}" if idx > 0 else None),
                }
            )
            continue

        action_name = str(a.get("type") or "").strip()
        params = {k: v for k, v in a.items() if k not in {"type", "task_id", "step_id", "depends_on"} and v is not None}
        _validate_action_params(action_name, params)
        contract_actions.append(
            {
                "action": action_name,
                "params": params,
                "task_id": str(a.get("task_id") or f"{job_id}:{idx}").strip(),
                "step_id": str(a.get("step_id") or f"step_{idx + 1}").strip(),
                "depends_on": str(a.get("depends_on") or "").strip() or (f"step_{idx}" if idx > 0 else None),
            }
        )

    job = {
        "job_id": job_id,
        "device_id": device_id,
        "username": username,
        "source_text": source_text,
        "actions": contract_actions,
    }
    _remember_job_owner(job)
    _trace_log(
        event="delegated_dispatch",
        user_id=username,
        task_id=job_id,
        lifecycle_state="executing",
        device_id=device_id,
        action_count=len(contract_actions),
    )
    await asyncio.wait_for(device_hub.send_job(device_id, job), timeout=GLOBAL_TASK_EXEC_TIMEOUT_S)
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
            "actions": [
                {
                    "type": (
                        (r.get("result") or {}).get("action_type")
                        if isinstance(r.get("result"), dict)
                        else r.get("action_type")
                    )
                }
                for r in execution_results
                if isinstance(r, dict)
            ]
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

        # Re-apply any saved runtime permissions so the hub state matches what
        # the agent will enforce immediately after reconnect.
        try:
            saved_caps = _get_saved_device_permissions(device_id) or {}
            if isinstance(saved_caps, dict) and saved_caps:
                await device_hub.update_capabilities(device_id, saved_caps)
        except Exception:
            pass

        logger = __import__('logging').getLogger(__name__)
        logger.info("[AGENT] Connected: device_id=%s", device_id)

        await ws.send_json({"type": "ack", "device_id": device_id, "status": "connected"})

        # Auto-resume queued tasks on reconnect using existing recovery paths.
        try:
            asyncio.create_task(_resume_queued_delegations_for_device(device_id))
            asyncio.create_task(_resume_pending_permission_delegations_for_device(device_id))
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
                        if flow_status == "completed":
                            _ops_inc("delegated_exec_success")
                            _clear_agent_unavailable(device_id)
                            _trace_log(
                                event="delegated_result",
                                task_id=jid,
                                lifecycle_state="completed",
                                device_id=device_id,
                            )
                        elif flow_status == "failed":
                            _ops_inc("delegated_exec_failure")
                            _trace_log(
                                event="delegated_result",
                                task_id=jid,
                                lifecycle_state="failed",
                                device_id=device_id,
                                level="warning",
                            )
                        if flow_status == "failed":
                            reason = str((first or {}).get("error") or (first or {}).get("message") or "failed")
                            if "timeout" in reason.lower():
                                _mark_agent_unavailable(device_id, reason="agent_result_timeout")
                            try:
                                col = _delegated_tasks_collection()
                                row = col.find_one({"last_job_id": jid}, {"_id": 0, "task_id": 1}) if col is not None else None
                                _retry_or_fail_delegated_task((row or {}).get("task_id") if isinstance(row, dict) else None, reason=reason)
                            except Exception:
                                _mark_delegated_task(job_id=jid, status_value="failed", extra={"error": reason[:300]})
                        else:
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
            payload = dict(cached[1])
            owner_device = _get_owner_device_id(username)
            allow_secret = bool(AGENT_SHARED_SECRET) and (
                (not CLOUD_MODE)
                or EXPOSE_AGENT_SHARED_SECRET
                or _is_local_request(request)
                or role == "admin"
                or (_normalize_device_id(owner_device or "") == _normalize_device_id(did))
            )
            if allow_secret:
                payload["agent_shared_secret"] = AGENT_SHARED_SECRET
            else:
                payload.pop("agent_shared_secret", None)
            return payload
    except Exception:
        pass

    server_url = _effective_server_url(request)

    # Record config in MongoDB, including the resolved shared secret when available.
    # Do this in a background task so the UI gets the token immediately.
    def _persist_agent_cfg():
        try:
            col = _agent_config_collection()
            if col is None:
                return
            secret_fields = {}
            if AGENT_SHARED_SECRET:
                secret_fields = {
                    "shared_secret": AGENT_SHARED_SECRET,
                    "agent_shared_secret": AGENT_SHARED_SECRET,
                }
            col.update_one(
                {"device_id": did},
                {
                    "$set": {
                        "device_id": did,
                        "owner_username": username if role != "admin" else (_get_device_owner(did) or None),
                        "server_url": server_url,
                        "updated_at": datetime.utcnow(),
                        "updated_by": username,
                        **secret_fields,
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
    owner_device = _get_owner_device_id(username)
    allow_secret = bool(AGENT_SHARED_SECRET) and (
        (not CLOUD_MODE)
        or EXPOSE_AGENT_SHARED_SECRET
        or _is_local_request(request)
        or role == "admin"
        or (_normalize_device_id(owner_device or "") == _normalize_device_id(did))
    )
    if allow_secret:
        payload["agent_shared_secret"] = AGENT_SHARED_SECRET

    cached_payload = dict(payload)
    cached_payload.pop("agent_shared_secret", None)

    # Cache briefly to make refresh/login feel instant.
    try:
        _AGENT_CONFIG_CACHE[(req.session_id, _normalize_device_id(did))] = (time.time(), cached_payload)
    except Exception:
        pass

    return payload


# =========================================================
# Speech-to-Text (Mobile fallback)
# =========================================================

GOOGLE_SPEECH_ENABLED = False
GOOGLE_SPEECH_LANGUAGE_DEFAULT = "en-US"
GOOGLE_SPEECH_CREDENTIALS_JSON = ""
GOOGLE_SPEECH_CREDENTIALS_B64 = ""


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

    executable_actions, blocked_actions = _split_agent_executable_actions(actions)
    if blocked_actions:
        raise HTTPException(status_code=400, detail={
            "message": "Invalid device dispatch payload",
            "hint": "Only executable device/file/system actions can be dispatched to PC agent. Generation/thinking actions must run before dispatch.",
            "blocked_actions": [str((a or {}).get("type") or "") for a in blocked_actions],
        })
    actions = executable_actions

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
    task = _queue_delegated_task(
        username=username,
        role=role,
        device_id=did,
        feature="device_dispatch",
        source_text=req.source_text or requested_action,
        actions=actions,
        status_value="executing",
        reason="dispatched",
    )
    _mark_delegated_task(task_id=task.get("task_id"), status_value="executing", extra={"last_job_id": job.get("job_id")})
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
    api_base = ""
    api_key = ""
    auto_create = False
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
    write_role = "user"
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
    write_role = "user"
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

    current_saved = _get_saved_device_permissions(did) or {}
    try:
        agent = await device_hub.get_agent(did)
    except Exception:
        agent = None
    current_caps = (agent or {}).get("capabilities") or {}

    if current_saved == normalized and all(bool(current_caps.get(k)) == bool(v) for k, v in normalized.items()):
        return {
            "status": "saved",
            "device_id": did,
            "permissions": normalized,
            "online": bool(await device_hub.is_connected(did)),
            "already_applied": True,
        }

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

    try:
        job = await _dispatch_actions_to_device(
            did,
            username=username or "user",
            actions=[{"type": "agent_set_permissions", "permissions": normalized}],
            source_text="permission_grant",
        )
    except Exception as exc:
        logger = __import__('logging').getLogger(__name__)
        logger.warning("[DEVICE PERMISSIONS] Live grant dispatch failed for %s: %s", did, exc)
        return {"status": "saved", "device_id": did, "permissions": normalized, "online": True, "queued": False}

    try:
        await device_hub.update_capabilities(did, normalized)
    except Exception:
        pass

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
    health = await _collect_full_health_checks()
    if role == "admin":
        connected = bool(agents_by_id)
        return {
            "status": "success",
            "agents": list(agents_by_id.values()),
            "default_device_id": DEFAULT_DEVICE_ID,
            "system_health": health,
            "degraded": str(health.get("status") or "ok") != "ok",
            "agent_offline": not connected,
            "device_status": {
                "connected": connected,
                "device_id": DEFAULT_DEVICE_ID,
            },
        }

    did = _get_owner_device_id(username)
    if not did and DEVICE_OWNER_USERNAME and username == DEVICE_OWNER_USERNAME.lower():
        did = DEFAULT_DEVICE_ID

    # Local/dev convenience: allow issuing a token for the default device even before
    # the user has explicitly configured a device binding.
    if not did and (not CLOUD_MODE) and LOCAL_DEFAULT_DEVICE_FALLBACK and DEFAULT_DEVICE_ID:
        did = DEFAULT_DEVICE_ID
    if not did:
        return {
            "status": "success",
            "agents": [],
            "default_device_id": DEFAULT_DEVICE_ID,
            "system_health": health,
            "degraded": str(health.get("status") or "ok") != "ok",
            "agent_offline": True,
            "device_status": {
                "connected": False,
                "device_id": None,
            },
        }
    agent = agents_by_id.get(did)
    connected = bool(agent)
    return {
        "status": "success",
        "agents": ([agent] if agent else []),
        "default_device_id": did,
        "system_health": health,
        "degraded": str(health.get("status") or "ok") != "ok",
        "agent_offline": not connected,
        "device_status": {
            "connected": connected,
            "device_id": did,
        },
    }


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
    extra = ""
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
learning_engine = SelfLearningEngine() if SelfLearningEngine is not None else None
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
            "ops_telemetry": _ops_telemetry_snapshot(),
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
            "ops_telemetry": _ops_telemetry_snapshot(),
        }


@app.get("/api/ops/telemetry")
async def ops_telemetry(session_id: str | None = None):
    """Lightweight production telemetry snapshot for staging verification."""
    if CLOUD_MODE:
        _require_authenticated_session(session_id)
    return {
        "status": "ok",
        "telemetry": _ops_telemetry_snapshot(),
        "uptime_seconds": int(max(0, time.time() - START_TS)),
    }


@app.get("/api/admin/intent-telemetry")
async def admin_intent_telemetry(session_id: str | None = None, limit: int = 120, lookback: int = 500):
    """Admin/debug endpoint for recent intent decisions and compact distribution report."""
    _require_admin_session(session_id)
    lim = max(10, min(int(limit or 120), 300))
    lb = max(50, min(int(lookback or 500), 2000))

    recent: list[dict[str, Any]] = []
    sample: list[dict[str, Any]] = []
    try:
        col = _intent_telemetry_collection()
        if col is not None:
            recent = list(col.find({}, {"_id": 0}).sort("created_at", -1).limit(lim))
            sample = list(col.find({}, {"_id": 0}).sort("created_at", -1).limit(lb))
    except Exception:
        recent = []
        sample = []

    compact_recent = []
    for r in recent:
        if not isinstance(r, dict):
            continue
        compact_recent.append(
            {
                "created_at": r.get("created_at_iso") or r.get("created_at"),
                "request_id": r.get("request_id"),
                "user_id": r.get("user_id"),
                "intent_type": r.get("intent_type"),
                "intent_depth": r.get("intent_depth"),
                "response_strategy": r.get("response_strategy"),
                "decision_mode": r.get("decision_mode"),
                "proactive_followup_added": bool(r.get("proactive_followup_added")),
                "user_preference_influenced": bool(r.get("user_preference_influenced")),
                "weak_outcome": bool(r.get("weak_outcome")),
                "mismatch_reason": r.get("mismatch_reason"),
            }
        )

    report = _aggregate_intent_telemetry(sample)
    return {
        "status": "ok",
        "lookback": lb,
        "recent_count": len(compact_recent),
        "report": report,
        "recent": compact_recent,
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
    db_uri = env.get("MONGODB_URI")
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


@app.get("/api/health/full")
async def health_full():
    full = await _collect_full_health_checks()
    return JSONResponse(full, status_code=200)


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
    started_at = time.perf_counter()
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
    _trace_log(
        event="chat_request_start",
        request_id=request_id,
        user_id=(username or msg.user or principal.get("username") or "anonymous"),
        lifecycle_state="received",
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
    try:
        response, actions = await asyncio.wait_for(
            chat_orchestrator.run_chat(
                text=msg.text,
                mode=(msg.mode or "chat"),
                principal=principal,
                role=role,
                acting_user=(msg.user or username or "user"),
                background_tasks=background_tasks,
                user_id=((username or msg.user) if (username or msg.user) else None),
            ),
            timeout=GLOBAL_LLM_CALL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _ops_inc("chat_total")
        _ops_inc("timeout_total")
        _ops_inc("error_total")
        response = {
            "status": "failed",
            "source": "timeout_guard",
            "text": "Request timed out while waiting for generation. Please retry.",
            "actions": [],
        }
        actions = []
        _trace_log(
            event="chat_request_timeout",
            request_id=request_id,
            user_id=(username or msg.user or principal.get("username") or "anonymous"),
            lifecycle_state="failed",
            execution_time_ms=max(0.0, (time.perf_counter() - started_at) * 1000.0),
            level="warning",
        )
    except Exception as e:
        _ops_inc("chat_total")
        _ops_inc("error_total")
        response = {
            "status": "failed",
            "source": "error_guard",
            "text": "Request failed due to a transient error. Please retry.",
            "actions": [],
        }
        actions = []
        _trace_log(
            event="chat_request_error",
            request_id=request_id,
            user_id=(username or msg.user or principal.get("username") or "anonymous"),
            lifecycle_state="failed",
            execution_time_ms=max(0.0, (time.perf_counter() - started_at) * 1000.0),
            level="error",
            error=str(e)[:180],
        )
    logger.info(
        "[chat.mode] request_id=%s mode=%s source=%s action_count=%s",
        request_id,
        str(msg.mode or "chat"),
        str((response or {}).get("source") or "unknown"),
        len(actions) if isinstance(actions, list) else 0,
    )

    # Lightweight production telemetry (best-effort, never blocks chat flow).
    try:
        _ops_inc("chat_total")
        elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
        parsed_latency = _parse_latency_to_ms((response or {}).get("latency"))
        _ops_add_latency_ms(parsed_latency if parsed_latency is not None else elapsed_ms)

        src = str((response or {}).get("source") or "").strip().lower()
        routing = (response or {}).get("routing") if isinstance((response or {}).get("routing"), dict) else {}
        fallback_used = bool(routing.get("fallback_used")) or src.startswith("fallback")
        if fallback_used:
            _ops_inc("fallback_total")

        txt = str((response or {}).get("text") or "").strip().lower()
        if ("timeout" in txt) or ("timed out" in txt):
            _ops_inc("timeout_total")
        if str((response or {}).get("status") or "").strip().lower() in {"failed", "error"}:
            _ops_inc("error_total")
        _ops_alert_if_needed(_ops_telemetry_snapshot())
    except Exception:
        pass

    # Persist normalized chat event (best effort, no impact on runtime path).
    try:
        if username or msg.user:
            database.save_chat(
                user_input=msg.text,
                bot_response=str((response or {}).get("text") or ""),
                session_id=msg.session_id,
                intent=str((response or {}).get("intent") or "chat"),
                context={
                    "user_id": (username or msg.user or "user"),
                    "request_id": request_id,
                    "mode": msg.mode or "chat",
                    "source": (response or {}).get("source") or "unknown",
                },
            )
    except Exception:
        pass

    # Controlled learning signal + periodic evaluator (best effort, suggestion-only).
    try:
        if learning_engine is not None:
            learning_signal = learning_engine.log_response_quality(
                user_id=(username or msg.user or "user"),
                query=str(msg.text or ""),
                response_text=str((response or {}).get("text") or ""),
                actions=(actions if isinstance(actions, list) else []),
                source=str((response or {}).get("source") or "chat"),
                request_id=request_id,
                response_status=str((response or {}).get("status") or ""),
                fallback_used=bool(((response or {}).get("routing") or {}).get("fallback_used")) if isinstance((response or {}).get("routing"), dict) else False,
                task_result_status=str((response or {}).get("task_result_status") or (response or {}).get("status") or ""),
            )
            response["learning_signal"] = {
                "quality_score": learning_signal.get("quality_score"),
                "weak": learning_signal.get("weak"),
                "response_outcome": learning_signal.get("response_outcome"),
            }
            # Internally cooldown-gated to prevent loops/regressions.
            background_tasks.add_task(learning_engine.run_controlled_learning_cycle, lookback_hours=48)
    except Exception:
        pass

    # Learn user behavior preferences (execution vs explanation and verbosity) into user_preferences.
    try:
        _learn_user_behavior_preferences(
            user_id=(username or msg.user),
            user_text=str(msg.text or ""),
            response_text=str((response or {}).get("text") or ""),
            actions=(actions if isinstance(actions, list) else []),
        )
    except Exception:
        pass

    # Intent-aware telemetry: lightweight per-request observability.
    try:
        rdict = response if isinstance(response, dict) else {}
        intent_type = str(rdict.get("intent_type") or "ambiguous").strip().lower() or "ambiguous"
        intent_depth = str(rdict.get("intent_depth") or "low").strip().lower() or "low"
        response_strategy = str(rdict.get("response_strategy") or "unknown").strip().lower() or "unknown"
        has_execution_actions = bool(isinstance(actions, list) and any(isinstance(a, dict) for a in actions))
        proactive_followup_added = bool(rdict.get("proactive_followup_added"))
        user_preference_influenced = bool(rdict.get("user_preference_influenced"))
        weak_outcome = bool(((rdict.get("learning_signal") or {}).get("weak")) if isinstance(rdict.get("learning_signal"), dict) else False)
        source = str(rdict.get("source") or "unknown")
        status_value = str(rdict.get("status") or "completed")
        routing = rdict.get("routing") if isinstance(rdict.get("routing"), dict) else {}
        fallback_used = bool(routing.get("fallback_used")) or source.startswith("fallback")

        _record_intent_telemetry(
            request_id=request_id,
            user_id=(username or msg.user),
            intent_type=intent_type,
            intent_depth=intent_depth,
            response_strategy=response_strategy,
            has_execution_actions=has_execution_actions,
            proactive_followup_added=proactive_followup_added,
            user_preference_influenced=user_preference_influenced,
            weak_outcome=weak_outcome,
            response_status=status_value,
            source=source,
            fallback_used=fallback_used,
        )
    except Exception:
        pass

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
                server_results = await asyncio.wait_for(
                    executor.process_actions(server_actions, (username or "user")),
                    timeout=GLOBAL_TASK_EXEC_TIMEOUT_S,
                )
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

                if False:
                    response["action_results"] = server_results
            except Exception as e:
                _ops_inc("error_total")
                _trace_log(
                    event="cloud_server_action_error",
                    request_id=request_id,
                    user_id=(username or msg.user or principal.get("username") or "anonymous"),
                    lifecycle_state="failed",
                    execution_time_ms=max(0.0, (time.perf_counter() - started_at) * 1000.0),
                    level="warning",
                    error=str(e)[:180],
                )
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
        _trace_log(
            event="chat_request_complete",
            request_id=request_id,
            user_id=(username or msg.user or principal.get("username") or "anonymous"),
            lifecycle_state=str((response or {}).get("status") or "completed"),
            execution_time_ms=max(0.0, (time.perf_counter() - started_at) * 1000.0),
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
    _trace_log(
        event="chat_request_complete",
        request_id=request_id,
        user_id=(username or msg.user or principal.get("username") or "anonymous"),
        lifecycle_state=str((response or {}).get("status") or "completed"),
        execution_time_ms=max(0.0, (time.perf_counter() - started_at) * 1000.0),
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


class LearningEvaluateRequest(BaseModel):
    session_id: str | None = None
    lookback_hours: int = 48


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


@app.get("/api/self-improvement/suggestions")
async def list_self_improvement_suggestions(session_id: str | None = None, limit: int = 100):
    principal = None
    if CLOUD_MODE:
        principal = _require_authenticated_session(session_id)
    elif session_id:
        principal = _get_principal(session_id)

    role = str((principal or {}).get("role") or "user").lower()
    if CLOUD_MODE and role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    rows: list[dict[str, Any]] = []
    try:
        database._ensure_connected()
        if database.db is not None:
            rows = list(
                database.db["self_improvement_suggestions"]
                .find({}, {"_id": 0})
                .sort("created_at", -1)
                .limit(max(1, min(int(limit or 100), 300)))
            )
    except Exception:
        rows = []
    return {"status": "ok", "suggestions": rows, "count": len(rows)}


@app.post("/api/learning/evaluate")
async def run_learning_evaluation(req: LearningEvaluateRequest):
    principal = None
    if CLOUD_MODE:
        principal = _require_authenticated_session(req.session_id)
        if str((principal or {}).get("role") or "user").lower() != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")

    if learning_engine is None:
        return {"status": "skipped", "reason": "learning_engine_unavailable"}

    out = learning_engine.run_controlled_learning_cycle(lookback_hours=max(1, min(int(req.lookback_hours or 48), 168)))
    return {"status": "success", "result": out}


@app.get("/api/learning/metrics")
async def learning_metrics(session_id: str | None = None):
    principal = None
    if CLOUD_MODE:
        principal = _require_authenticated_session(session_id)
        if str((principal or {}).get("role") or "user").lower() != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    elif session_id:
        principal = _get_principal(session_id)

    _ = principal
    database._ensure_connected()
    if database.db is None:
        return {"status": "skipped", "reason": "db_unavailable"}

    cycle = database.db["learning_cycle_reports"].find_one({}, {"_id": 0}, sort=[("created_at", -1)]) or {}
    open_suggestion_count = int(database.db["self_improvement_suggestions"].count_documents({"status": "open"}))
    top_suggestion = (
        database.db["self_improvement_suggestions"].find_one({"status": "open"}, {"_id": 0, "issue": 1, "priority": 1}, sort=[("priority", -1), ("created_at", -1)])
        or {}
    )

    perf_rows = list(
        database.db["model_performance_stats"]
        .find({}, {"_id": 0, "model_id": 1, "success": 1, "latency_ms": 1, "fallback_used": 1})
        .sort("recorded_at", -1)
        .limit(1000)
    )
    by_model: dict[str, dict[str, Any]] = {}
    for row in perf_rows:
        mid = str((row or {}).get("model_id") or "unknown")
        item = by_model.setdefault(mid, {"model_id": mid, "calls": 0, "failure": 0, "latency_total": 0.0, "fallback": 0})
        item["calls"] += 1
        ok = bool((row or {}).get("success"))
        item["failure"] += 0 if ok else 1
        item["latency_total"] += float((row or {}).get("latency_ms") or 0.0)
        item["fallback"] += 1 if bool((row or {}).get("fallback_used")) else 0

    summary: list[dict[str, Any]] = []
    for v in by_model.values():
        calls = max(1, int(v.get("calls") or 1))
        summary.append(
            {
                "model_id": v.get("model_id"),
                "calls": calls,
                "failure_rate": round(float(v.get("failure") or 0) / calls, 4),
                "avg_latency_ms": round(float(v.get("latency_total") or 0.0) / calls, 3),
                "fallback_rate": round(float(v.get("fallback") or 0) / calls, 4),
            }
        )
    summary.sort(key=lambda x: float(x.get("failure_rate") or 0.0), reverse=True)
    top_failing = summary[0] if summary else {}

    latest_success_score = float(cycle.get("success_score") or 0.0)
    failure_patterns = cycle.get("failure_patterns") if isinstance(cycle.get("failure_patterns"), list) else []
    top_failure_pattern = str(failure_patterns[0]) if failure_patterns else ""

    return {
        "status": "success",
        "metrics": {
            "latest_success_score": round(latest_success_score, 4),
            "top_failure_pattern": top_failure_pattern,
            "open_suggestion_count": open_suggestion_count,
            "top_failing_model": top_failing,
            "last_learning_cycle_at": cycle.get("created_at"),
            "top_suggestion": top_suggestion,
        },
    }


@app.get("/api/model-ops/performance")
async def model_ops_performance(session_id: str, limit: int = 500):
    _require_authenticated_session(session_id)
    database._ensure_connected()
    if database.db is None:
        return {"status": "skipped", "reason": "db_unavailable"}

    rows = list(
        database.db["model_performance_stats"]
        .find({}, {"_id": 0})
        .sort("recorded_at", -1)
        .limit(max(1, min(int(limit or 500), 2000)))
    )
    if not rows:
        return {"status": "success", "summary": {}, "rows": []}

    by_model: dict[str, dict[str, Any]] = {}
    for r in rows:
        model_id = str((r or {}).get("model_id") or "unknown")
        it = by_model.setdefault(
            model_id,
            {
                "model_id": model_id,
                "provider": str((r or {}).get("provider") or "unknown"),
                "calls": 0,
                "success": 0,
                "failure": 0,
                "latency_total_ms": 0.0,
                "fallback_calls": 0,
            },
        )
        it["calls"] += 1
        ok = bool((r or {}).get("success"))
        it["success"] += 1 if ok else 0
        it["failure"] += 0 if ok else 1
        it["latency_total_ms"] += float((r or {}).get("latency_ms") or 0.0)
        it["fallback_calls"] += 1 if bool((r or {}).get("fallback_used")) else 0

    summary = []
    for v in by_model.values():
        calls = max(1, int(v.get("calls") or 1))
        summary.append(
            {
                "model_id": v.get("model_id"),
                "provider": v.get("provider"),
                "calls": calls,
                "success_rate": round(float(v.get("success") or 0) / calls, 4),
                "failure_rate": round(float(v.get("failure") or 0) / calls, 4),
                "avg_latency_ms": round(float(v.get("latency_total_ms") or 0.0) / calls, 3),
                "fallback_rate": round(float(v.get("fallback_calls") or 0) / calls, 4),
            }
        )
    summary.sort(key=lambda x: float(x.get("failure_rate") or 0.0), reverse=True)

    return {"status": "success", "summary": summary, "rows": rows[:200]}

@app.get("/api/wakeup-context")
async def get_wakeup_context(session_id: str | None = None):
    """Get wakeup context mapping"""
    if CLOUD_MODE:
        _require_authenticated_session(session_id)
    return {"context": task_manager.get_wakeup_context()}


# =========================================================
# Model Ops API
# =========================================================
class ModelOpsRecommendRequest(BaseModel):
    session_id: str
    mode: str | None = "hybrid"
    constraints: Dict[str, Any] | None = None


class ModelOpsReadinessRequest(BaseModel):
    session_id: str
    dataset_dir: str | None = None
    target_model_id: str | None = None


class ModelOpsPrepareFinetuneRequest(BaseModel):
    session_id: str
    profile_name: str | None = None
    target_model_id: str | None = None
    dataset_dir: str | None = None
    dry_run: bool = True


class ModelOpsSelectProfileRequest(BaseModel):
    session_id: str
    profile_name: str


class ModelOpsBenchmarkRequest(BaseModel):
    session_id: str
    profile_name: str | None = None


def _require_model_ops_available() -> None:
    if not MODEL_OPS_AVAILABLE:
        raise HTTPException(status_code=503, detail="Model Ops subsystem unavailable")


@app.get("/api/model-ops/status")
async def model_ops_status(session_id: str):
    _require_authenticated_session(session_id)
    _require_model_ops_available()
    reg = model_ops_load_registry() if callable(model_ops_load_registry) else {}
    health = model_ops_check_health() if callable(model_ops_check_health) else {}
    if callable(model_ops_update_health):
        try:
            model_ops_update_health(health)
        except Exception:
            pass
    return {
        "status": "success",
        "model_ops_available": True,
        "registry": reg,
        "health": health,
    }


@app.get("/api/model-ops/catalog")
async def model_ops_catalog(session_id: str):
    _require_authenticated_session(session_id)
    _require_model_ops_available()
    models = model_ops_list_models() if callable(model_ops_list_models) else []
    matrix = model_ops_capability_summary() if callable(model_ops_capability_summary) else {}
    return {"status": "success", "models": models, "capability_matrix": matrix}


@app.post("/api/model-ops/recommend")
async def model_ops_recommend(req: ModelOpsRecommendRequest):
    _require_authenticated_session(req.session_id)
    _require_model_ops_available()
    mode = (req.mode or "hybrid").strip().lower()
    constraints = req.constraints if isinstance(req.constraints, dict) else {}
    out = model_ops_recommend_with_mode(mode, constraints) if callable(model_ops_recommend_with_mode) else {}
    saved = None
    if callable(model_ops_save_recommendation):
        try:
            saved = model_ops_save_recommendation(out, prefix=f"recommend_{mode}")
        except Exception:
            saved = None
    return {"status": "success", "recommendation": out, "saved_path": saved}


@app.post("/api/model-ops/readiness-check")
async def model_ops_readiness_check(req: ModelOpsReadinessRequest):
    _require_authenticated_session(req.session_id)
    _require_model_ops_available()
    stats = model_ops_inspect_dataset(req.dataset_dir) if callable(model_ops_inspect_dataset) else {}
    readiness = model_ops_compute_readiness(stats, model_supports_finetune=True) if callable(model_ops_compute_readiness) else {}
    if callable(model_ops_update_readiness):
        try:
            model_ops_update_readiness(readiness)
        except Exception:
            pass
    return {"status": "success", "readiness": readiness}


@app.post("/api/model-ops/prepare-finetune")
async def model_ops_prepare_finetune(req: ModelOpsPrepareFinetuneRequest):
    _require_admin_session(req.session_id)
    _require_model_ops_available()
    res = model_ops_prepare_finetune_run(
        profile_name=req.profile_name,
        target_model_id=req.target_model_id,
        dataset_dir=req.dataset_dir,
        dry_run=bool(req.dry_run),
    ) if callable(model_ops_prepare_finetune_run) else {}
    return {"status": "success", "result": res}


@app.get("/api/model-ops/deployment-profiles")
async def model_ops_profiles(session_id: str):
    _require_authenticated_session(session_id)
    _require_model_ops_available()
    profiles = model_ops_list_profiles() if callable(model_ops_list_profiles) else {}
    reg = model_ops_load_registry() if callable(model_ops_load_registry) else {}
    return {"status": "success", "profiles": profiles, "active_profile": reg.get("active_profile")}


@app.post("/api/model-ops/select-profile")
async def model_ops_select_profile(req: ModelOpsSelectProfileRequest):
    _require_admin_session(req.session_id)
    _require_model_ops_available()
    profiles = model_ops_list_profiles() if callable(model_ops_list_profiles) else {}
    if req.profile_name not in profiles:
        raise HTTPException(status_code=400, detail="Unknown deployment profile")
    reg = model_ops_update_profile(req.profile_name) if callable(model_ops_update_profile) else {}
    return {"status": "success", "registry": reg}


@app.post("/api/model-ops/benchmark")
async def model_ops_benchmark(req: ModelOpsBenchmarkRequest):
    _require_admin_session(req.session_id)
    _require_model_ops_available()
    active = None
    if callable(model_ops_load_registry):
        try:
            active = (model_ops_load_registry() or {}).get("active_profile")
        except Exception:
            active = None
    profile_name = (req.profile_name or active or "local_primary_api_backup").strip()
    report = model_ops_run_benchmark(profile_name) if callable(model_ops_run_benchmark) else {}
    if callable(model_ops_update_benchmark):
        try:
            model_ops_update_benchmark(report.get("path") or "", report.get("summary") or {})
        except Exception:
            pass
    return {"status": "success", "benchmark": report}


@app.get("/api/model-ops/benchmark-report")
async def model_ops_benchmark_report(session_id: str):
    _require_authenticated_session(session_id)
    _require_model_ops_available()
    report = model_ops_latest_benchmark_report() if callable(model_ops_latest_benchmark_report) else {"status": "not_found"}
    return report

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
