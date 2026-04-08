# src/core/llm_adapter.py
import os
import json
import asyncio
import aiohttp
import re
import random
import time
import logging
from copy import deepcopy
from urllib.parse import quote_plus
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path
from src.utils.db import db
from src.config import runtime_defaults as rd
from src.config.secrets import llm_secrets
from src.config.settings import settings as jarvis_settings

logger = logging.getLogger(__name__)

try:
    from src.model_ops.runtime_router import resolve_route as model_ops_resolve_route
except Exception:
    model_ops_resolve_route = None

try:
    from src.learning import SelfLearningEngine
except Exception:
    SelfLearningEngine = None

# Import decision-making system
try:
    from src.core.decision_maker import ContextAwareDecisionMaker, initialize_decision_maker
    DECISION_MAKER_AVAILABLE = True
except Exception:
    DECISION_MAKER_AVAILABLE = False

load_dotenv()

class LLMAdapter:
    """
    Unified LLM Adapter with intelligent response structure and humanlike personality.
    Supports GPT (OpenAI) and fallback to local training.
    """

    def __init__(self):
        self.provider = str(getattr(rd, "LLM_PROVIDER", "openai_compatible") or "openai_compatible").strip().lower()
        # Primary: OpenAI (ChatGPT). Fallback: Groq (OpenAI-compatible endpoint).
        self.primary_model = rd.PRIMARY_MODEL
        # Optional smarter model for hard tasks (routing by heuristic complexity).
        # Keep PRIMARY_MODEL as a safe default to avoid breaking existing deployments.
        self.smart_model = (rd.SMART_MODEL or "").strip()
        self.fast_model = str(jarvis_settings.llm_fast_model or "").strip()
        self.smart_model_min_complexity = int(rd.SMART_MODEL_MIN_COMPLEXITY)
        self.primary_key = llm_secrets().primary_api_key
        self.primary_endpoint = (rd.PRIMARY_ENDPOINT or "").strip()

        # Fallback provider (Groq). If primary fails, we attempt this once.
        self.backup_model = rd.BACKUP_MODEL
        self.backup_key = llm_secrets().backup_api_key
        self.backup_endpoint = (rd.BACKUP_ENDPOINT or "").strip()
        self.cloud_mode = bool(jarvis_settings.cloud_mode)
        self.persona = rd.PERSONA
        self.session = None
        self.provider_timeout_s = max(5, int(jarvis_settings.llm_provider_timeout_s))
        self.provider_budget_s = max(
            self.provider_timeout_s,
            int(jarvis_settings.llm_provider_budget_s),
        )
        self.provider_cooldown_s = 30
        self.provider_failure_threshold = 3
        self.timeout = aiohttp.ClientTimeout(total=self.provider_timeout_s)
        self.max_retries = 2
        # Default response budget. We dynamically increase for complex queries.
        self.default_max_tokens = int(rd.LLM_MAX_TOKENS_DEFAULT)
        self.max_max_tokens = int(rd.LLM_MAX_TOKENS_MAX)

        self.personality = {
            "formal-gentle": {
                "tone": "polite, confident, and articulate",
                "prefix": "Sir" if random.random() > 0.5 else "Boss"
            },
            "friendly": {
                "tone": "casual and caring, like a human friend",
                "prefix": "Hey"
            },
            "analyst": {
                "tone": "logical, concise, technical",
                "prefix": "Observation"
            }
        }
        
        # Advanced decision-making system (optional)
        self.decision_maker = None
        if DECISION_MAKER_AVAILABLE:
            try:
                self.decision_maker = None  # Will be initialized on first use
            except Exception:
                pass
        self._skills_cache = None
        self._skills_cache_mtime = 0.0
        self._local_reasoner_state_key = self._resolve_local_reasoner_state_key()
        self._local_reasoner_state_path = self._resolve_local_reasoner_state_path()
        self._local_reasoner_state = self._load_local_reasoner_state()
        self._local_reasoner_daily_maintenance()
        self.model_ops_routing_enabled = bool(jarvis_settings.model_ops_routing_enabled)
        self._last_model_ops_route = None
        self._response_cache = {}
        self._provider_fail_until: dict[str, float] = {}
        self._provider_fail_count: dict[str, int] = {}
        self._last_provider_notice_at = 0.0
        self._recent_intents: list[dict] = []
        self.learning_engine = SelfLearningEngine(cooldown_seconds=60) if SelfLearningEngine is not None else None
        self._runtime_task_contexts: dict[str, dict] = {}

    async def _call_ollama_chat(
        self,
        messages,
        *,
        model: str | None,
        endpoint: str | None,
        temperature: float,
    ):
        await self._ensure_session()
        url = endpoint if endpoint is not None else self.primary_endpoint
        payload = {
            "model": (model or self.primary_model),
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": float(temperature),
            },
        }
        async with self.session.post(url, json=payload) as r:
            if r.status != 200:
                raise Exception(f"Ollama API error: {await r.text()}")
            return await r.json()

    @staticmethod
    def _resolve_local_reasoner_state_key() -> str:
        try:
            key = str(getattr(rd, "LOCAL_REASONER_STATE_KEY", "global") or "global").strip().lower()
            return key or "global"
        except Exception:
            return "global"

    @staticmethod
    def _is_local_provider_url(url: str | None) -> bool:
        try:
            u = str(url or "").strip().lower()
            if not u:
                return False
            return ("127.0.0.1" in u) or ("localhost" in u) or ("::1" in u)
        except Exception:
            return False

    @staticmethod
    def _resolve_local_reasoner_state_path() -> Path:
        configured = str(getattr(rd, "LOCAL_REASONER_STATE_FILE", "") or "").strip()
        if configured:
            try:
                return Path(configured).expanduser().resolve()
            except Exception:
                try:
                    return Path(configured)
                except Exception:
                    pass
        try:
            root = Path(__file__).resolve().parents[2]
        except Exception:
            root = Path.cwd()
        return root / "data" / "local_reasoner_state.json"

    @staticmethod
    def _default_local_reasoner_state() -> dict:
        return {
            "version": 1,
            "updated_at": "",
            "last_maintenance_day": "",
            "app_aliases": {},
            "site_aliases": {},
            "stats": {
                "learn_events": 0,
                "hits": 0,
            },
            "cycles": [],
            "user_habits": {},
        }

    @staticmethod
    def _default_user_habits_bucket() -> dict:
        return {
            "frequent_apps": {},
            "common_actions": {},
            "preferred_workflows": [],
            "repeated_patterns": {},
            "updated_at": "",
        }

    def _ensure_local_reasoner_defaults(self, data: dict | None) -> dict:
        out = data if isinstance(data, dict) else self._default_local_reasoner_state()
        if "stats" not in out:
            out["stats"] = {"learn_events": 0, "hits": 0}
        if "cycles" not in out:
            out["cycles"] = []
        if "user_habits" not in out or not isinstance(out.get("user_habits"), dict):
            out["user_habits"] = {}
        # Ensure habit buckets loaded from DB/files include all expected fields.
        try:
            normalized: dict[str, dict] = {}
            for k, v in (out.get("user_habits") or {}).items():
                if not isinstance(v, dict):
                    normalized[str(k)] = self._default_user_habits_bucket()
                    continue
                b = {**self._default_user_habits_bucket(), **v}
                if not isinstance(b.get("frequent_apps"), dict):
                    b["frequent_apps"] = {}
                if not isinstance(b.get("common_actions"), dict):
                    b["common_actions"] = {}
                if not isinstance(b.get("preferred_workflows"), list):
                    b["preferred_workflows"] = []
                if not isinstance(b.get("repeated_patterns"), dict):
                    b["repeated_patterns"] = {}
                normalized[str(k)] = b
            out["user_habits"] = normalized
        except Exception:
            pass
        return out

    def _user_habits_key(self, user_prefs: dict | None) -> str:
        u = self._extract_user_scope_key(user_prefs)
        return str(u or "global")

    def _get_user_habits_bucket(self, user_prefs: dict | None) -> dict:
        st = self._ensure_local_reasoner_defaults(self._local_reasoner_state)
        buckets = st.get("user_habits") if isinstance(st.get("user_habits"), dict) else {}
        key = self._user_habits_key(user_prefs)
        bucket = buckets.get(key) if isinstance(buckets.get(key), dict) else self._default_user_habits_bucket()
        buckets[key] = bucket
        st["user_habits"] = buckets
        self._local_reasoner_state = st
        return bucket

    @staticmethod
    def _normalize_pattern_tokens(actions: list[dict]) -> list[str]:
        toks: list[str] = []
        for a in (actions or [])[:4]:
            if not isinstance(a, dict):
                continue
            at = str(a.get("type") or "").strip().lower()
            if at in {"open_app", "switch_app", "close_app"}:
                app = str(a.get("app_name") or "").strip().lower()
                toks.append(f"{at}:{app}" if app else at)
            elif at == "device_action":
                nm = str(a.get("name") or "").strip().lower()
                toks.append(f"device_action:{nm}" if nm else "device_action")
            else:
                toks.append(at)
        return [t for t in toks if t]

    def _update_user_habits_from_step(self, *, user_prefs: dict | None, user_text: str, actions: list[dict], success: bool) -> None:
        try:
            if not bool(getattr(rd, "LOCAL_REASONER_LEARNING_ENABLED", True)):
                return
            if not isinstance(actions, list) or not actions:
                return

            bucket = self._get_user_habits_bucket(user_prefs)
            apps = bucket.get("frequent_apps") if isinstance(bucket.get("frequent_apps"), dict) else {}
            common = bucket.get("common_actions") if isinstance(bucket.get("common_actions"), dict) else {}
            patterns = bucket.get("repeated_patterns") if isinstance(bucket.get("repeated_patterns"), dict) else {}

            action_tokens = self._normalize_pattern_tokens(actions)
            for tok in action_tokens:
                common[tok] = int(common.get(tok) or 0) + 1

            for a in actions:
                if not isinstance(a, dict):
                    continue
                at = str(a.get("type") or "").strip().lower()
                if at in {"open_app", "switch_app"}:
                    app = str(a.get("app_name") or "").strip().lower()
                    if app:
                        apps[app] = int(apps.get(app) or 0) + 1

            if success:
                key = " -> ".join(action_tokens[:3]).strip()
                if key:
                    patterns[key] = int(patterns.get(key) or 0) + 1

            workflow = {
                "goal": self._infer_high_level_goal(user_text=user_text, actions=actions),
                "pattern": " -> ".join(action_tokens[:3]),
                "hits": int(patterns.get(" -> ".join(action_tokens[:3])) or 0),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            preferred = bucket.get("preferred_workflows") if isinstance(bucket.get("preferred_workflows"), list) else []
            preferred = [w for w in preferred if not (isinstance(w, dict) and str(w.get("pattern") or "") == str(workflow.get("pattern") or ""))]
            preferred.insert(0, workflow)

            bucket["frequent_apps"] = dict(sorted(apps.items(), key=lambda kv: int(kv[1] or 0), reverse=True)[:12])
            bucket["common_actions"] = dict(sorted(common.items(), key=lambda kv: int(kv[1] or 0), reverse=True)[:20])
            bucket["repeated_patterns"] = dict(sorted(patterns.items(), key=lambda kv: int(kv[1] or 0), reverse=True)[:20])
            bucket["preferred_workflows"] = preferred[:8]
            bucket["updated_at"] = datetime.now(timezone.utc).isoformat()

            buckets = self._local_reasoner_state.get("user_habits") if isinstance(self._local_reasoner_state.get("user_habits"), dict) else {}
            buckets[self._user_habits_key(user_prefs)] = bucket
            self._local_reasoner_state["user_habits"] = buckets

            stats = self._local_reasoner_state.get("stats") if isinstance(self._local_reasoner_state.get("stats"), dict) else {}
            stats["learn_events"] = int(stats.get("learn_events") or 0) + 1
            self._local_reasoner_state["stats"] = stats
            self._save_local_reasoner_state()
        except Exception:
            pass

    def _user_habits_prompt_block(self, user_prefs: dict | None) -> str:
        try:
            bucket = self._get_user_habits_bucket(user_prefs)
            apps = list((bucket.get("frequent_apps") or {}).keys())[:3]
            actions = list((bucket.get("common_actions") or {}).keys())[:4]
            flows = [str((w or {}).get("pattern") or "") for w in (bucket.get("preferred_workflows") or []) if isinstance(w, dict)]
            flows = [f for f in flows if f][:2]
            if not apps and not actions and not flows:
                return "(none)"
            return (
                f"frequent_apps: {', '.join(apps) if apps else '(none)'}\n"
                f"common_actions: {', '.join(actions) if actions else '(none)'}\n"
                f"preferred_workflows: {', '.join(flows) if flows else '(none)'}"
            )
        except Exception:
            return "(none)"

    def _personalize_next_possible_actions(self, *, base_actions: list[str], user_prefs: dict | None, context_state: dict | None) -> list[str]:
        base = [str(x).strip() for x in (base_actions or []) if str(x).strip()]
        try:
            bucket = self._get_user_habits_bucket(user_prefs)
            preferred = bucket.get("preferred_workflows") if isinstance(bucket.get("preferred_workflows"), list) else []
            common = bucket.get("common_actions") if isinstance(bucket.get("common_actions"), dict) else {}
            personalized: list[str] = []

            last_action = str((context_state or {}).get("last_action") or "").strip().lower()
            for wf in preferred[:4]:
                if not isinstance(wf, dict):
                    continue
                pat = str(wf.get("pattern") or "").strip().lower()
                if not pat or not last_action:
                    continue
                parts = [p.strip() for p in pat.split("->") if p.strip()]
                for i, part in enumerate(parts[:-1]):
                    if last_action in part:
                        nxt = parts[i + 1]
                        if nxt.startswith("open_app:"):
                            app = nxt.split(":", 1)[1].strip()
                            if app:
                                personalized.append(f"open {app}")
                        elif nxt.startswith("web_search"):
                            personalized.append("refine search")
                        elif nxt.startswith("type_text"):
                            personalized.append("compose message")
                        elif nxt:
                            personalized.append(nxt.replace("_", " "))

            for k in list(common.keys())[:6]:
                kk = str(k or "").strip().lower()
                if kk.startswith("device_action:set_volume"):
                    personalized.append("adjust volume")
                elif kk.startswith("open_url"):
                    personalized.append("open your usual site")

            merged = []
            for x in personalized + base:
                sx = str(x).strip()
                if sx and sx.lower() not in {m.lower() for m in merged}:
                    merged.append(sx)
                if len(merged) >= 2:
                    break
            return merged or base[:2]
        except Exception:
            return base[:2]

    def _load_local_reasoner_state(self) -> dict:
        state = self._default_local_reasoner_state()
        db_enabled = bool(getattr(rd, "LOCAL_REASONER_DB_ENABLED", True))
        cloud_db_mode = bool(self.cloud_mode) and db_enabled

        if db_enabled:
            try:
                data = db.local_reasoner_state_get(self._local_reasoner_state_key)
                if isinstance(data, dict):
                    return self._ensure_local_reasoner_defaults(data)
                if cloud_db_mode:
                    logger.warning(
                        "[local_reasoner.persistence] DB enabled but no state found for key=%s in cloud mode; using default state (no file fallback)",
                        self._local_reasoner_state_key,
                    )
                    return state
                logger.warning(
                    "[local_reasoner.persistence] DB enabled but no state found for key=%s; falling back to file/default",
                    self._local_reasoner_state_key,
                )
            except Exception as e:
                if cloud_db_mode:
                    logger.warning(
                        "[local_reasoner.persistence] DB read failed for key=%s in cloud mode: %s; using default state (no file fallback)",
                        self._local_reasoner_state_key,
                        str(e),
                    )
                    return state
                logger.warning(
                    "[local_reasoner.persistence] DB read failed for key=%s: %s; falling back to file/default",
                    self._local_reasoner_state_key,
                    str(e),
                )

        if cloud_db_mode:
            return state

        try:
            p = self._local_reasoner_state_path
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return self._ensure_local_reasoner_defaults(data)
        except Exception:
            pass

        return state

    def _save_local_reasoner_state(self) -> None:
        self._local_reasoner_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        db_enabled = bool(getattr(rd, "LOCAL_REASONER_DB_ENABLED", True))
        cloud_db_mode = bool(self.cloud_mode) and db_enabled
        if db_enabled:
            try:
                ok = db.local_reasoner_state_upsert(
                    state=self._local_reasoner_state,
                    state_key=self._local_reasoner_state_key,
                )
                if ok:
                    return
                if cloud_db_mode:
                    logger.warning(
                        "[local_reasoner.persistence] DB upsert returned false for key=%s in cloud mode; skipping file fallback",
                        self._local_reasoner_state_key,
                    )
                    return
                logger.warning(
                    "[local_reasoner.persistence] DB upsert returned false for key=%s; using file fallback",
                    self._local_reasoner_state_key,
                )
            except Exception as e:
                if cloud_db_mode:
                    logger.warning(
                        "[local_reasoner.persistence] DB upsert failed for key=%s in cloud mode: %s; skipping file fallback",
                        self._local_reasoner_state_key,
                        str(e),
                    )
                    return
                logger.warning(
                    "[local_reasoner.persistence] DB upsert failed for key=%s: %s; using file fallback",
                    self._local_reasoner_state_key,
                    str(e),
                )

        if cloud_db_mode:
            logger.warning(
                "[local_reasoner.persistence] Cloud mode with DB enabled uses DB-only persistence for key=%s; local file write skipped",
                self._local_reasoner_state_key,
            )
            return
        try:
            p = self._local_reasoner_state_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._local_reasoner_state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(
                "[local_reasoner.persistence] Local file fallback write failed for key=%s: %s",
                self._local_reasoner_state_key,
                str(e),
            )

    @staticmethod
    def _extract_user_scope_key(user_prefs: dict | None) -> str | None:
        if not isinstance(user_prefs, dict):
            return None
        candidates = (
            user_prefs.get("user_id"),
            user_prefs.get("uid"),
            user_prefs.get("username"),
            user_prefs.get("email"),
            user_prefs.get("sub"),
        )
        for raw in candidates:
            token = str(raw or "").strip().lower()
            if token:
                token = re.sub(r"[^a-z0-9._:@-]+", "_", token)
                token = token.strip("_")
                if token:
                    return token
        return None

    def _load_local_reasoner_state_for_key(self, state_key: str) -> dict:
        default_state = self._default_local_reasoner_state()
        key = str(state_key or "global").strip().lower() or "global"
        db_enabled = bool(getattr(rd, "LOCAL_REASONER_DB_ENABLED", True))
        cloud_db_mode = bool(self.cloud_mode) and db_enabled

        if db_enabled:
            try:
                data = db.local_reasoner_state_get(key)
                if isinstance(data, dict):
                    return self._ensure_local_reasoner_defaults(data)
                if cloud_db_mode:
                    logger.warning(
                        "[local_reasoner.persistence] DB enabled but no state found for key=%s in cloud mode; using default state (no file fallback)",
                        key,
                    )
                    return default_state
                logger.warning(
                    "[local_reasoner.persistence] DB enabled but no state found for key=%s; falling back to file/default",
                    key,
                )
            except Exception as e:
                if cloud_db_mode:
                    logger.warning(
                        "[local_reasoner.persistence] DB read failed for key=%s in cloud mode: %s; using default state (no file fallback)",
                        key,
                        str(e),
                    )
                    return default_state
                logger.warning(
                    "[local_reasoner.persistence] DB read failed for key=%s: %s; falling back to file/default",
                    key,
                    str(e),
                )

        if cloud_db_mode:
            return default_state

        try:
            if key == "global":
                p = self._local_reasoner_state_path
                if p.exists():
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return self._ensure_local_reasoner_defaults(data)
        except Exception:
            pass

        return default_state

    def _ensure_local_reasoner_state_scope(self, user_prefs: dict | None) -> None:
        configured_key = self._resolve_local_reasoner_state_key()
        target_key = configured_key

        if configured_key == "user":
            user_key = self._extract_user_scope_key(user_prefs)
            target_key = f"user:{user_key}" if user_key else "global"

        target_key = str(target_key or "global").strip().lower() or "global"
        if target_key == self._local_reasoner_state_key and isinstance(self._local_reasoner_state, dict):
            return

        target_state = self._load_local_reasoner_state_for_key(target_key)

        # For first-time user scopes in DB mode, seed from global context.
        if target_key.startswith("user:") and bool(getattr(rd, "LOCAL_REASONER_DB_ENABLED", True)):
            try:
                existing = db.local_reasoner_state_get(target_key)
                if not isinstance(existing, dict):
                    seed = self._load_local_reasoner_state_for_key("global")
                    if isinstance(seed, dict):
                        target_state = json.loads(json.dumps(seed))
            except Exception:
                pass

        self._local_reasoner_state_key = target_key
        self._local_reasoner_state = target_state if isinstance(target_state, dict) else self._default_local_reasoner_state()
        self._save_local_reasoner_state()

    def _local_reasoner_daily_maintenance(self) -> None:
        if not bool(getattr(rd, "LOCAL_REASONER_LEARNING_ENABLED", True)):
            return
        try:
            st = self._local_reasoner_state
            today = datetime.now(timezone.utc).date().isoformat()
            last = str(st.get("last_maintenance_day") or "").strip()
            if last == today:
                return

            decay = float(getattr(rd, "LOCAL_REASONER_DAILY_DECAY", 0.98) or 0.98)
            decay = max(0.50, min(1.0, decay))
            min_score = float(getattr(rd, "LOCAL_REASONER_MIN_ALIAS_SCORE", 0.20) or 0.20)
            max_aliases = int(getattr(rd, "LOCAL_REASONER_MAX_ALIASES", 400) or 400)

            for bucket_name in ("app_aliases", "site_aliases"):
                bucket = st.get(bucket_name) if isinstance(st.get(bucket_name), dict) else {}
                cleaned: dict[str, dict] = {}
                for alias, item in bucket.items():
                    if not isinstance(item, dict):
                        continue
                    score = float(item.get("score") or 0.0) * decay
                    if score < min_score:
                        continue
                    item["score"] = round(score, 4)
                    cleaned[str(alias)] = item
                if len(cleaned) > max_aliases:
                    ordered = sorted(cleaned.items(), key=lambda x: float((x[1] or {}).get("score") or 0.0), reverse=True)
                    cleaned = dict(ordered[:max_aliases])
                st[bucket_name] = cleaned

            st["last_maintenance_day"] = today
            self._save_local_reasoner_state()
        except Exception:
            pass

    @staticmethod
    def _normalize_alias_phrase(s: str) -> str:
        t = (s or "").strip().lower()
        if not t:
            return ""
        t = re.sub(r"^[\s,.;:!?\-]+|[\s,.;:!?\-]+$", "", t)
        t = re.sub(r"\b(?:please|pls|jarvis|can\s+you|could\s+you|would\s+you)\b", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    @staticmethod
    def _extract_command_target_phrase(user_text: str) -> str:
        tl = (user_text or "").strip().lower()
        if not tl:
            return ""
        m = re.search(
            r"\b(?:open|launch|start|close|quit|exit|switch\s+to|switch|go\s+to|visit|browse|navigate\s+to)\b\s+(.+)$",
            tl,
        )
        if not m:
            return ""
        target = (m.group(1) or "").strip()
        target = re.split(r"\b(and\s+then|then|after\s+that|and\s+type|and\s+write|and\s+search)\b", target, maxsplit=1)[0]
        return LLMAdapter._normalize_alias_phrase(target)

    @staticmethod
    def _extract_action_url(action: dict) -> str:
        try:
            if not isinstance(action, dict):
                return ""
            if str(action.get("type") or "").strip().lower() != "open_url":
                return ""
            url = str(action.get("url") or "").strip()
            if not url:
                return ""
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url):
                url = "https://" + url
            return url
        except Exception:
            return ""

    def _predict_action_from_learned_alias(self, user_text: str) -> dict | None:
        if not bool(getattr(rd, "LOCAL_REASONER_LEARNING_ENABLED", True)):
            return None
        try:
            self._local_reasoner_daily_maintenance()
            tl = (user_text or "").strip().lower()
            phrase = self._extract_command_target_phrase(user_text)
            if not phrase:
                return None
            min_score = float(getattr(rd, "LOCAL_REASONER_MIN_ALIAS_SCORE", 0.20) or 0.20)

            app_aliases = self._local_reasoner_state.get("app_aliases") if isinstance(self._local_reasoner_state.get("app_aliases"), dict) else {}
            site_aliases = self._local_reasoner_state.get("site_aliases") if isinstance(self._local_reasoner_state.get("site_aliases"), dict) else {}

            app_entry = app_aliases.get(phrase) if isinstance(app_aliases.get(phrase), dict) else None
            site_entry = site_aliases.get(phrase) if isinstance(site_aliases.get(phrase), dict) else None

            if app_entry and float(app_entry.get("score") or 0.0) >= min_score:
                app_name = str(app_entry.get("app_name") or "").strip()
                if app_name:
                    if re.search(r"\b(close|quit|exit)\b", tl):
                        out = {"text": f"Closing {app_name}.", "actions": [{"type": "close_app", "app_name": app_name}]}
                    elif re.search(r"\b(switch\s+to|switch|go\s+to)\b", tl):
                        out = {"text": f"Switching to {app_name}.", "actions": [{"type": "switch_app", "app_name": app_name}]}
                    else:
                        out = {"text": f"Opening {app_name}.", "actions": [{"type": "open_app", "app_name": app_name, "args": []}]}
                    st = self._local_reasoner_state.get("stats") if isinstance(self._local_reasoner_state.get("stats"), dict) else {}
                    st["hits"] = int(st.get("hits") or 0) + 1
                    self._local_reasoner_state["stats"] = st
                    self._save_local_reasoner_state()
                    return out

            if site_entry and float(site_entry.get("score") or 0.0) >= min_score:
                url = str(site_entry.get("url") or "").strip()
                if url and re.search(r"\b(open|visit|browse|navigate|go\s+to)\b", tl):
                    st = self._local_reasoner_state.get("stats") if isinstance(self._local_reasoner_state.get("stats"), dict) else {}
                    st["hits"] = int(st.get("hits") or 0) + 1
                    self._local_reasoner_state["stats"] = st
                    self._save_local_reasoner_state()
                    return {"text": "Opening it.", "actions": [{"type": "open_url", "url": url}]}
        except Exception:
            return None
        return None

    def _learn_from_actions(self, user_text: str, actions: list[dict]) -> None:
        if not bool(getattr(rd, "LOCAL_REASONER_LEARNING_ENABLED", True)):
            return
        if not isinstance(actions, list) or not actions:
            return
        try:
            self._local_reasoner_daily_maintenance()
            phrase = self._extract_command_target_phrase(user_text)
            if not phrase or len(phrase) < 2:
                return

            stop_phrases = {
                "it", "this", "that", "something", "anything", "app", "application", "website", "site"
            }
            if phrase in stop_phrases:
                return

            app_aliases = self._local_reasoner_state.get("app_aliases") if isinstance(self._local_reasoner_state.get("app_aliases"), dict) else {}
            site_aliases = self._local_reasoner_state.get("site_aliases") if isinstance(self._local_reasoner_state.get("site_aliases"), dict) else {}

            learned = False
            for a in actions[:3]:
                if not isinstance(a, dict):
                    continue
                at = str(a.get("type") or "").strip().lower()
                if at in {"open_app", "close_app", "switch_app"}:
                    app_name = str(a.get("app_name") or "").strip().lower()
                    if app_name and phrase != app_name:
                        item = app_aliases.get(phrase) if isinstance(app_aliases.get(phrase), dict) else {"app_name": app_name, "score": 0.0}
                        item["app_name"] = app_name
                        item["score"] = round(float(item.get("score") or 0.0) + 1.0, 4)
                        item["updated_at"] = datetime.now(timezone.utc).isoformat()
                        app_aliases[phrase] = item
                        learned = True
                elif at == "open_url":
                    url = self._extract_action_url(a)
                    if url and not re.search(r"\bhttps?://|www\.|\.[a-z]{2,}\b", phrase):
                        item = site_aliases.get(phrase) if isinstance(site_aliases.get(phrase), dict) else {"url": url, "score": 0.0}
                        item["url"] = url
                        item["score"] = round(float(item.get("score") or 0.0) + 1.0, 4)
                        item["updated_at"] = datetime.now(timezone.utc).isoformat()
                        site_aliases[phrase] = item
                        learned = True

            if learned:
                max_aliases = int(getattr(rd, "LOCAL_REASONER_MAX_ALIASES", 400) or 400)
                if len(app_aliases) > max_aliases:
                    app_aliases = dict(sorted(app_aliases.items(), key=lambda x: float((x[1] or {}).get("score") or 0.0), reverse=True)[:max_aliases])
                if len(site_aliases) > max_aliases:
                    site_aliases = dict(sorted(site_aliases.items(), key=lambda x: float((x[1] or {}).get("score") or 0.0), reverse=True)[:max_aliases])
                self._local_reasoner_state["app_aliases"] = app_aliases
                self._local_reasoner_state["site_aliases"] = site_aliases
                stats = self._local_reasoner_state.get("stats") if isinstance(self._local_reasoner_state.get("stats"), dict) else {}
                stats["learn_events"] = int(stats.get("learn_events") or 0) + 1
                self._local_reasoner_state["stats"] = stats
                self._save_local_reasoner_state()
        except Exception:
            pass

    def evaluate_execution_results(self, goal: str, planned_actions: list[dict], execution_results: list[dict]) -> dict:
        """
        EVALUATE phase: Assess if the executed actions achieved the goal.
        
        Returns {
            "goal_achieved": bool,
            "success_rate": float (0-1),
            "failed_actions": [action indices],
            "evaluation_notes": str,
            "outcome_summary": str,
        }
        """
        try:
            if not goal or not execution_results:
                return {
                    "goal_achieved": False,
                    "success_rate": 0.0,
                    "failed_actions": [],
                    "evaluation_notes": "Missing goal or execution results",
                    "outcome_summary": "Cannot evaluate without data",
                }

            success_statuses = {"success", "opened", "written", "typed", "sent", "completed", "done"}
            total_actions = len(execution_results)
            successful_actions = 0
            failed_indices = []

            for i, result in enumerate(execution_results):
                if not isinstance(result, dict):
                    failed_indices.append(i)
                    continue
                
                status = (result.get("status") or "").lower()
                if status in success_statuses:
                    successful_actions += 1
                else:
                    failed_indices.append(i)

            success_rate = successful_actions / total_actions if total_actions > 0 else 0.0
            # Heuristic: if >80% actions succeeded and goal keywords appear in results
            achieved = success_rate >= 0.8
            
            evaluation_notes = f"Executed {total_actions} actions: {successful_actions} succeeded, {len(failed_indices)} failed."
            
            if failed_indices:
                evaluation_notes += f" Failed action indices: {failed_indices}."
            
            if achieved:
                outcome_summary = f"Goal '{goal[:50]}...' appears achieved ({success_rate*100:.0f}% success rate)."
            else:
                outcome_summary = f"Goal '{goal[:50]}...' partially achieved ({success_rate*100:.0f}% success rate). Needs improvement."

            return {
                "goal_achieved": achieved,
                "success_rate": round(success_rate, 2),
                "failed_actions": failed_indices,
                "evaluation_notes": evaluation_notes,
                "outcome_summary": outcome_summary,
            }
        except Exception as e:
            return {
                "goal_achieved": False,
                "success_rate": 0.0,
                "failed_actions": [],
                "evaluation_notes": f"Evaluation error: {str(e)}",
                "outcome_summary": "Could not evaluate execution",
            }

    def generate_improvement_feedback(self, goal: str, evaluation: dict, execution_results: list[dict]) -> dict:
        """
        IMPROVE phase: Generate feedback to improve future planning.
        
        Returns {
            "action_feedbacks": [{"index": int, "feedback": str, "suggestion": str}],
            "retry_strategy": str,
            "improved_plan": [suggested actions],
            "learning_note": str,
        }
        """
        try:
            if not evaluation or not execution_results:
                return {
                    "action_feedbacks": [],
                    "retry_strategy": "RETRY_SAME",
                    "improved_plan": [],
                    "learning_note": "No evaluation data for improvement",
                }

            failed_indices = evaluation.get("failed_actions") or []
            action_feedbacks = []

            for idx in failed_indices:
                if 0 <= idx < len(execution_results):
                    result = execution_results[idx]
                    action_status = result.get("status", "unknown")
                    error_msg = result.get("error", "No error message")
                    
                    feedback = f"Action {idx} failed with status '{action_status}': {error_msg}"
                    suggestion = self._suggest_action_improvement(result, idx)
                    
                    action_feedbacks.append({
                        "index": idx,
                        "feedback": feedback,
                        "suggestion": suggestion,
                    })

            # Determine retry strategy
            success_rate = evaluation.get("success_rate", 0.0)
            if success_rate == 1.0:
                retry_strategy = "SUCCESS"
            elif success_rate >= 0.7:
                retry_strategy = "RETRY_FAILED_ONLY"
            elif success_rate >= 0.3:
                retry_strategy = "RETRY_WITH_DELAYS"
            else:
                retry_strategy = "REPLAN_REQUIRED"

            # Generate improved plan
            improved_plan = self._generate_improved_plan(goal, execution_results, action_feedbacks)

            learning_note = (
                f"Completed cycle for goal '{goal[:50]}...': Success rate {evaluation.get('success_rate', 0)*100:.0f}%. "
                f"Strategy: {retry_strategy}. Failed actions: {len(failed_indices)}."
            )

            return {
                "action_feedbacks": action_feedbacks,
                "retry_strategy": retry_strategy,
                "improved_plan": improved_plan,
                "learning_note": learning_note,
            }
        except Exception as e:
            return {
                "action_feedbacks": [],
                "retry_strategy": "RETRY_SAME",
                "improved_plan": [],
                "learning_note": f"Improvement generation error: {str(e)}",
            }

    def _suggest_action_improvement(self, failed_result: dict, action_index: int) -> str:
        """Generate specific improvement suggestion for a failed action."""
        status = (failed_result.get("status") or "").lower()
        action_type = (failed_result.get("action_type") or "").lower()
        error = (failed_result.get("error") or "").lower()

        if "not found" in error or "not installed" in error:
            return f"Action {action_index}: Verify {action_type} is available/installed. Consider fallback."
        elif "permission" in error or "denied" in error:
            return f"Action {action_index}: {action_type} requires elevated permissions. Request may be needed."
        elif "timeout" in error:
            return f"Action {action_index}: {action_type} timed out. Increase delay or reduce complexity."
        elif action_type == "type_text" and error:
            return f"Action {action_index}: Typing failed. Increase 'before_ms' delay or reduce text length."
        else:
            return f"Action {action_index}: {action_type} failed. Review error: {error[:60]}"

    def _generate_improved_plan(self, goal: str, execution_results: list[dict], feedbacks: list[dict]) -> list[dict]:
        """Generate an improved action plan based on failures."""
        # Enhanced plan adds delays and fallbacks
        improved = []
        for i, result in enumerate(execution_results):
            status = (result.get("status") or "").lower()
            if status in {"success", "opened", "written", "typed", "sent", "completed"}:
                # Keep successful actions, but add a small delay after them
                action = dict(result)  # shallow copy
                if "before_ms" not in action:
                    action["before_ms"] = 500
                improved.append(action)
            else:
                # For failed actions, add retry with longer delay
                action = dict(result)
                action["before_ms"] = max(2000, action.get("before_ms", 1000) + 500)
                action["retry_count"] = action.get("retry_count", 1)
                improved.append(action)

        return improved

    def _save_cycle_feedback(self, user_id: str, goal: str, evaluation: dict, improvement: dict) -> None:
        """Save cycle feedback for learning and future improvements."""
        try:
            feedback_record = {
                "user_id": user_id,
                "goal": goal,
                "evaluation": evaluation,
                "improvement": improvement,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cycle_timestamp": datetime.now(timezone.utc).isoformat(),
            }
            
            # Store in local reasoner state for retrieval
            if hasattr(self, "_local_reasoner_state"):
                cycles = self._local_reasoner_state.get("cycles", [])
                if not isinstance(cycles, list):
                    cycles = []
                cycles.append(feedback_record)
                # Keep last 50 cycles
                self._local_reasoner_state["cycles"] = cycles[-50:]
                self._save_local_reasoner_state()
        except Exception:
            pass

    def process_goal_plan_execute_cycle(self, goal: str, user_id: str, planned_response: dict, execution_results: list[dict]) -> dict:
        """
        PUBLIC method: Complete the Goal → Plan → Execute → Evaluate → Improve cycle.
        
        Called AFTER the agent executes the planned actions.
        
        Args:
            goal: Original user intent/goal
            user_id: User identifier for feedback persistence
            planned_response: Response from generate_response (Plan phase)
            execution_results: Results from agent execution (Execute phase)
        
        Returns: Enhanced response with evaluation and improvement data:
        {
            "evaluation": {...},
            "improvement_feedback": {...},
            "recommendations": str,
            "next_steps": [suggested actions],
        }
        """
        # EVALUATE PHASE
        evaluation = self.evaluate_execution_results(goal, planned_response.get("actions", []), execution_results)
        
        # IMPROVE PHASE
        improvement_feedback = self.generate_improvement_feedback(goal, evaluation, execution_results)
        
        # SAVE FEEDBACK FOR LEARNING
        self._save_cycle_feedback(user_id, goal, evaluation, improvement_feedback)
        
        # Generate recommendations
        recommendations = self._generate_cycle_recommendations(evaluation, improvement_feedback)
        
        return {
            "evaluation": evaluation,
            "improvement_feedback": improvement_feedback,
            "recommendations": recommendations,
            "next_steps": improvement_feedback.get("improved_plan", []),
            "cycle_status": "complete",
        }

    def _generate_cycle_recommendations(self, evaluation: dict, improvement: dict) -> str:
        """Generate user-facing recommendations from the cycle."""
        retry_strategy = improvement.get("retry_strategy", "UNKNOWN")
        outcome = evaluation.get("outcome_summary", "Cycle completed")
        
        if retry_strategy == "SUCCESS":
            return f"✓ {outcome} Goal achieved successfully!"
        elif retry_strategy == "RETRY_FAILED_ONLY":
            failed = ', '.join(str(i) for i in evaluation.get("failed_actions", []))
            return f"⚠ {outcome} Some actions failed. Will retry: {failed}"
        elif retry_strategy == "RETRY_WITH_DELAYS":
            return f"⚠ {outcome} Some actions need timing adjustments. Retrying with longer delays."
        elif retry_strategy == "REPLAN_REQUIRED":
            return f"✗ {outcome} Many actions failed. Needs replanning."
        else:
            return outcome

    def _get_cycle_context_for_goal(self, goal: str, max_records: int = 5) -> str:
        """
        Universal learning context: retrieve prior cycle feedback for goals similar to the
        current one and format it as a context string injected into the LLM system prompt.

        This is the mechanism that makes the LLM self-adaptive WITHOUT adding new
        deterministic if-blocks — the model learns from past successes/failures through
        the Goal → Plan → Execute → Evaluate → Improve cycle.
        """
        try:
            cycles = self._local_reasoner_state.get("cycles", [])
            if not isinstance(cycles, list) or not cycles:
                return ""

            goal_words = set(re.findall(r'\b\w{3,}\b', goal.lower()))
            if not goal_words:
                return ""

            # Score each record by keyword overlap with the current goal.
            scored: list[tuple[int, dict]] = []
            for record in reversed(cycles):  # most-recent-first
                rec_goal = (record.get("goal") or "").lower()
                rec_words = set(re.findall(r'\b\w{3,}\b', rec_goal))
                overlap = len(goal_words & rec_words)
                if overlap > 0:
                    scored.append((overlap, record))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = [r for _, r in scored[:max_records]]
            if not top:
                return ""

            lines: list[str] = []
            for record in top:
                rec_goal = record.get("goal", "")
                evaluation = record.get("evaluation") or {}
                improvement = record.get("improvement") or {}
                retry_strategy = improvement.get("retry_strategy", "")
                success_rate = float(evaluation.get("success_rate") or 0)
                actions_planned = evaluation.get("actions_planned") or []
                action_names = [
                    str(a.get("type", a) if isinstance(a, dict) else a)
                    for a in actions_planned
                ]

                if retry_strategy == "SUCCESS" or success_rate >= 0.8:
                    lines.append(
                        f"PAST SUCCESS: goal='{rec_goal}' → actions={action_names} "
                        f"(success_rate={success_rate:.0%})"
                    )
                else:
                    failed = evaluation.get("failed_actions") or []
                    improved = [
                        str(a.get("type", a) if isinstance(a, dict) else a)
                        for a in (improvement.get("improved_plan") or [])
                    ]
                    lines.append(
                        f"PAST ATTEMPT: goal='{rec_goal}' → tried={action_names} "
                        f"failed={failed} strategy={retry_strategy} improved_plan={improved}"
                    )

            if not lines:
                return ""

            return (
                "CYCLE LEARNING CONTEXT (use to improve action planning):\n"
                + "\n".join(lines)
            )
        except Exception:
            return ""

    @staticmethod
    def _normalize_query_key(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip().lower())

    def _cache_response(self, text: str, parsed: dict) -> None:
        try:
            key = self._normalize_query_key(text)
            if not key:
                return
            cached = {
                "text": str((parsed or {}).get("text") or "").strip(),
                "actions": (parsed or {}).get("actions") if isinstance((parsed or {}).get("actions"), list) else [],
                "timestamp": time.time(),
            }
            if not cached["text"] and not cached["actions"]:
                return
            self._response_cache[key] = cached
            # Keep cache bounded.
            if len(self._response_cache) > 80:
                oldest = sorted(self._response_cache.items(), key=lambda it: float((it[1] or {}).get("timestamp") or 0.0))[:20]
                for k, _ in oldest:
                    self._response_cache.pop(k, None)
        except Exception:
            pass

    def _get_cached_response(self, text: str, *, max_age_s: int = 900) -> dict | None:
        try:
            now = time.time()
            key = self._normalize_query_key(text)
            direct = self._response_cache.get(key)
            if isinstance(direct, dict) and (now - float(direct.get("timestamp") or 0.0)) <= max_age_s:
                return {
                    "text": str(direct.get("text") or "").strip(),
                    "actions": direct.get("actions") if isinstance(direct.get("actions"), list) else [],
                    "source": "fallback-cached-context",
                }

            best_item = None
            best_score = 0.0
            for k, item in self._response_cache.items():
                if (now - float((item or {}).get("timestamp") or 0.0)) > max_age_s:
                    continue
                score = self._text_similarity_score(key, k)
                if score > best_score:
                    best_score = score
                    best_item = item

            if isinstance(best_item, dict) and best_score >= 0.92:
                return {
                    "text": str(best_item.get("text") or "").strip(),
                    "actions": best_item.get("actions") if isinstance(best_item.get("actions"), list) else [],
                    "source": "fallback-cached-similar",
                }
            return None
        except Exception:
            return None

    def _compact_repeat_reply(self, user_text: str, cached_text: str) -> str:
        """Return a concise repeat-aware response that references prior output."""
        base = str(cached_text or "").strip()
        if not base:
            return "Same as above."

        try:
            first = self._first_sentences(base, max_sentences=1)
            if len(first) > 180:
                first = first[:180].rstrip(" ,.;:") + "."
        except Exception:
            first = base[:180].rstrip(" ,.;:") + ("." if len(base) > 180 else "")

        intent = self._classify_primary_intent(user_text)
        if intent == "generation_intent":
            return "I already drafted this above. Want me to apply it now?"
        if intent == "action_intent":
            return "Same as before. I can run this for you now if you want."
        return f"Same as above: {first}"

    def _provider_available(self, provider_name: str) -> bool:
        try:
            name = str(provider_name or "").strip().lower()
            if not name:
                return True
            until = float(self._provider_fail_until.get(name) or 0.0)
            return time.time() >= until
        except Exception:
            return True

    def _mark_provider_failure(self, provider_name: str) -> None:
        try:
            name = str(provider_name or "").strip().lower()
            if not name:
                return
            failures = int(self._provider_fail_count.get(name) or 0) + 1
            self._provider_fail_count[name] = failures
            if failures >= int(self.provider_failure_threshold):
                self._provider_fail_until[name] = time.time() + float(self.provider_cooldown_s)
                logger.warning(
                    "[llm.circuit.open] provider=%s failures=%s cooldown_s=%s",
                    name,
                    failures,
                    self.provider_cooldown_s,
                )
        except Exception:
            pass

    def _mark_provider_success(self, provider_name: str) -> None:
        try:
            name = str(provider_name or "").strip().lower()
            if name in self._provider_fail_count:
                self._provider_fail_count.pop(name, None)
            if name in self._provider_fail_until:
                self._provider_fail_until.pop(name, None)
        except Exception:
            pass

    @staticmethod
    def _knowledge_query_type(text: str) -> str:
        t = str(text or "").strip().lower()
        if not t:
            return "general"
        if re.search(r"\b(codebase|repository|repo|module|class|function|architecture|project)\b", t):
            return "project"
        if re.search(r"\b(debug|traceback|exception|stack|error|fail|crash|timeout)\b", t):
            return "debug"
        if re.search(r"\b(system|runtime|listener|routing|behavior|state|status)\b", t):
            return "system_behavior"
        if re.search(r"\b(task|history|last\s+task|previous\s+task|delegat)\b", t):
            return "task_history"
        return "general"

    @staticmethod
    def _token_set(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]{3,}", str(s or "").lower())}

    @classmethod
    def _text_similarity_score(cls, q: str, d: str) -> float:
        a = cls._token_set(q)
        b = cls._token_set(d)
        if not a or not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        return float(inter / union) if union else 0.0

    @staticmethod
    def _parse_iso_ts(s: str) -> datetime | None:
        try:
            raw = str(s or "").strip().replace("Z", "+00:00")
            if not raw:
                return None
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    def _rank_rag_candidates(self, query: str, rows: list[dict], *, task_correlation_id: str = "") -> list[dict]:
        now = datetime.now(timezone.utc)
        scored: list[tuple[float, dict]] = []
        qtype = self._knowledge_query_type(query)
        for r in rows:
            msg = str((r or {}).get("message") or "").strip()
            if not msg:
                continue
            if len(msg) < 14:
                continue
            if re.fullmatch(r"(?i)(ok|done|ack|noted|recorded|yes|no|success|failed)\.?", msg):
                continue
            sim = self._text_similarity_score(query, msg)
            ts = self._parse_iso_ts((r or {}).get("timestamp") or "")
            recency = 0.0
            if ts is not None:
                try:
                    age_h = max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds() / 3600.0)
                    recency = max(0.0, 1.0 - min(age_h / 72.0, 1.0))
                except Exception:
                    recency = 0.0
            corr_bonus = 0.0
            corr = str((r or {}).get("correlation_id") or "").strip()
            if corr and task_correlation_id and corr == task_correlation_id:
                corr_bonus = 0.25
            lifecycle = str((r or {}).get("lifecycle_state") or "").strip().lower()
            life_bonus = 0.12 if lifecycle in {"completed", "failed", "executing", "in_progress"} else 0.0
            error_relevance = 0.0
            if qtype == "debug":
                row_type = str((r or {}).get("type") or "").strip().lower()
                if row_type in {"error", "error_context", "exception"} or re.search(r"\b(error|failed|exception|traceback|timeout|denied)\b", msg.lower()):
                    error_relevance = 0.2
            score = (sim * 0.56) + (recency * 0.22) + corr_bonus + life_bonus + error_relevance
            if score <= 0.03:
                continue
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored]

    def _short_term_memory_block(self) -> str:
        try:
            if not self._recent_intents:
                return "(none)"
            items = self._recent_intents[-3:]
            return "\n".join(
                [
                    f"- intent={str(it.get('intent') or 'unknown')} task={str(it.get('task') or 'unknown')} source={str(it.get('source') or 'unknown')}"
                    for it in items
                ]
            )
        except Exception:
            return "(none)"

    def _long_term_memory_block(self, user_prefs: dict | None = None) -> str:
        try:
            lines = []
            up = user_prefs if isinstance(user_prefs, dict) else {}
            lang = str(up.get("language") or up.get("language_name") or "").strip() if up else ""
            persona = str(up.get("persona") or "").strip() if up else ""
            if lang:
                lines.append(f"- preferred_language={lang}")
            if persona:
                lines.append(f"- preferred_persona={persona}")

            st = self._local_reasoner_state if isinstance(self._local_reasoner_state, dict) else {}
            stats = st.get("stats") if isinstance(st.get("stats"), dict) else {}
            learn_events = int(stats.get("learn_events") or 0)
            hits = int(stats.get("hits") or 0)
            if learn_events or hits:
                lines.append(f"- usage_patterns: learn_events={learn_events}, hits={hits}")
            return "\n".join(lines) if lines else "(none)"
        except Exception:
            return "(none)"

    def _build_delegation_hint(self, text: str, decision_hint: dict | None = None) -> str:
        t = str(text or "").lower()
        conf = float((decision_hint or {}).get("confidence") or 0.0) if isinstance(decision_hint, dict) else 0.0
        if re.search(r"\b(open|close|click|type|screenshot|volume|brightness|wifi|bluetooth|app|process)\b", t):
            if conf >= 0.65:
                return "if device action is requested and confidence high: delegate to PC agent"
            return "if device action is requested but confidence low: ask one focused permission/clarification before delegating"
        return "if request is informational: answer directly; delegate only when explicit device execution is required"

    @staticmethod
    def _build_retry_suggestion(error_text: str) -> str:
        e = str(error_text or "").lower()
        if "timeout" in e:
            return "Retry with a smaller scope or shorter step sequence; if still timing out, run one step at a time."
        if "permission" in e or "denied" in e:
            return "Verify required permission and retry after confirming access for the target action."
        if "network" in e or "dns" in e or "connection" in e:
            return "Check connectivity and retry once; if unstable, switch to local fallback actions."
        if "not found" in e:
            return "Verify the target name or path and retry using an exact identifier."
        return "Retry once after verifying prerequisites and recent task state."

    def _build_error_intelligence_hints(self, text: str, *, limit: int = 3) -> list[str]:
        hints: list[str] = []
        try:
            qtype = self._knowledge_query_type(text)
            if qtype not in {"debug", "system_behavior", "task_history"}:
                return hints
            db._ensure_connected()
            if db.db is None:
                return hints
            q_words = [w for w in re.findall(r"[a-z0-9]{3,}", str(text or "").lower())[:8]]
            regex = {"$regex": "|".join(re.escape(w) for w in q_words), "$options": "i"} if q_words else None
            query = {"message": regex} if isinstance(regex, dict) else {}
            rows = list(db.db["error_logs"].find(query, {"message": 1, "payload": 1, "timestamp": 1}).sort("timestamp", -1).limit(18))
            for row in rows:
                msg = str((row or {}).get("message") or "").strip()
                if not msg:
                    continue
                payload = (row or {}).get("payload") if isinstance((row or {}).get("payload"), dict) else {}
                cause = str(payload.get("cause") or payload.get("reason") or payload.get("error_type") or "unknown").strip()
                retry = self._build_retry_suggestion(msg + " " + cause)
                hints.append(f"error={msg[:120]} | cause={cause[:80]} | fix={retry}")
                if len(hints) >= max(1, int(limit)):
                    break
        except Exception:
            return hints
        return hints

    def _has_local_fallback_capability(self, text: str) -> bool:
        t = str(text or "").strip().lower()
        if not t:
            return False
        try:
            d = self._preparse_deterministic_voice_actions(text)
            if isinstance(d, dict):
                return True
        except Exception:
            pass
        if re.match(r"^(hi|hello|hey|howdy|greetings|good\s+(morning|afternoon|evening|day))(\s+jarvis)?([\s!?.]*)$", t):
            return True
        if re.match(r"^(what\s+is\s+)?(-?\d+)\s*([+\-*/])\s*(-?\d+)\??$", t):
            return True
        if "what can you do" in t or "capabilit" in t:
            return True
        if "open " in t or "search " in t or "youtube" in t or "play " in t:
            return True
        return False

    def _build_actionable_fallback_text(self, text: str, *, include_provider_notice: bool = True) -> str:
        t = str(text or "").strip().lower()
        if not include_provider_notice:
            if re.search(r"\b(task|plan|workflow|steps|delegate)\b", t):
                return "Working on it."
            if re.search(r"\b(project|repo|codebase|architecture)\b", t):
                return "I can help with that."
            return "Done."
        if re.search(r"\b(debug|error|traceback|exception|fail|timeout)\b", t):
            return "Share the exact error line and I will fix it step by step."
        if re.search(r"\b(task|plan|workflow|steps|delegate)\b", t):
            return "I can take this step by step."
        if re.search(r"\b(project|repo|codebase|architecture)\b", t):
            return "I can review that and suggest the next best step."
        return "Done."

    def _fetch_recent_runtime_context(self, text: str, *, limit: int = 3, include_rag: bool = True) -> dict:
        out = {
            "recent_chat": [],
            "rag_context": [],
            "last_task_outcome": "",
            "last_task_correlation_id": "",
            "error_fix_hints": [],
            "learning_hints": [],
            "cached_best_response": "",
        }
        try:
            # Fast path: keep trivial chat requests independent from DB health.
            # This prevents avoidable latency/timeouts when Mongo is slow/unavailable.
            try:
                qtype_fast = self._knowledge_query_type(text)
            except Exception:
                qtype_fast = "general"
            if (not include_rag) and qtype_fast in {"general", "project"}:
                if self.learning_engine is not None:
                    out["learning_hints"] = self.learning_engine.get_learning_hints(text, limit=2)
                    out["cached_best_response"] = self.learning_engine.get_cached_best_response(text) or ""
                return out

            db._ensure_connected()
            if db.db is None:
                return out

            for row in db.db["chat_logs"].find({}, {"message": 1, "timestamp": 1}).sort("timestamp", -1).limit(max(1, int(limit))):
                msg = str((row or {}).get("message") or "").strip()
                if msg:
                    out["recent_chat"].append(msg)

            qtype = self._knowledge_query_type(text)
            rag_collections: tuple[str, ...] = ()
            if include_rag and qtype == "project":
                rag_collections = ("training_events", "agent_logs", "chat_logs")
            elif include_rag and qtype == "debug":
                rag_collections = ("error_logs", "task_logs", "agent_logs")
            elif include_rag and qtype == "system_behavior":
                rag_collections = ("agent_logs", "task_logs", "training_events")
            elif include_rag and qtype == "task_history":
                rag_collections = ("task_logs", "agent_logs", "chat_logs")

            q_words = [w for w in re.findall(r"[a-z0-9]{3,}", str(text or "").lower())[:8]]
            candidates: list[dict] = []
            if rag_collections and q_words:
                pattern = "|".join(re.escape(w) for w in q_words)
                regex = {"$regex": pattern, "$options": "i"}
                for cname in rag_collections:
                    rows = db.db[cname].find(
                        {"message": regex},
                        {"message": 1, "timestamp": 1, "correlation_id": 1, "lifecycle_state": 1, "type": 1, "source": 1},
                    ).sort("timestamp", -1).limit(14)
                    for row in rows:
                        d = dict(row or {})
                        d["collection"] = cname
                        candidates.append(d)

            task_row = db.db["task_logs"].find_one(
                {"lifecycle_state": {"$in": ["completed", "failed", "stopped"]}},
                sort=[("timestamp", -1)],
            )
            if isinstance(task_row, dict):
                st = str(task_row.get("result_status") or task_row.get("lifecycle_state") or "recorded").strip()
                msg = str(task_row.get("message") or "").strip()
                corr = str(task_row.get("correlation_id") or "").strip()
                if corr:
                    out["last_task_correlation_id"] = corr
                if msg:
                    out["last_task_outcome"] = f"{st}: {msg}"

            if candidates:
                ranked = self._rank_rag_candidates(
                    str(text or ""),
                    candidates,
                    task_correlation_id=str(out.get("last_task_correlation_id") or ""),
                )
                total_chars = 0
                for row in ranked[:4]:
                    m = str((row or {}).get("message") or "").strip()
                    if not m:
                        continue
                    if len(m) < 14 or re.search(r"\b(ok|done|ack|noted|recorded|yes|no)\b", m.strip().lower()):
                        continue
                    source = str((row or {}).get("source") or (row or {}).get("collection") or "ctx").strip()
                    life = str((row or {}).get("lifecycle_state") or "recorded").strip()
                    line = f"[{source}|{life}] {m[:220]}"
                    if total_chars + len(line) > 760:
                        break
                    out["rag_context"].append(line)
                    total_chars += len(line)

            out["error_fix_hints"] = self._build_error_intelligence_hints(text, limit=3)
            if self.learning_engine is not None:
                out["learning_hints"] = self.learning_engine.get_learning_hints(text, limit=3)
                out["cached_best_response"] = self.learning_engine.get_cached_best_response(text) or ""
        except Exception:
            return out
        return out

    @staticmethod
    def _build_reasoning_hint(text: str) -> str:
        t = str(text or "").strip().lower()
        if not t:
            return "none"
        system_action = bool(re.search(r"\b(open|close|run|execute|click|type|settings|volume|brightness|wifi|bluetooth|screenshot|task|automation)\b", t))
        if system_action:
            return "detect intent -> plan steps -> route/delegate safely -> execute -> respond with result and next step"
        return "detect intent -> plan concise answer -> respond with only relevant details"

    @staticmethod
    def _first_sentences(text: str, *, max_sentences: int = 2) -> str:
        s = str(text or "").strip()
        if not s:
            return s
        parts = re.split(r"(?<=[.!?])\s+", s)
        return " ".join([p.strip() for p in parts[:max_sentences] if p.strip()]).strip()

    @staticmethod
    def _clean_conversational_noise(text: str) -> str:
        """Remove operational/log artifacts that should not be spoken as chat text."""
        txt = str(text or "").strip()
        if not txt:
            return ""

        patterns = [
            r"(?im)^\s*capturing\s+command\.{0,3}\s*$",
            r"(?im)^\s*pc\s+actions?\s+delegated\s*\([^)]*\)\.?\s*$",
            r"(?im)^\s*pc\s+action\s+completed\b[^\n]*$",
            r"(?i)\bresults\s*:\s*\d+\s*ok\s*,\s*\d+\s*error\s*,\s*\d+\s*forbidden\b",
            r"(?i)\bsource\s*:\s*delegated\s+work\b",
        ]
        for pat in patterns:
            txt = re.sub(pat, " ", txt)

        txt = re.sub(
            r"(?i)\bi\s+can\s+handle\s+deterministic\s+local\s+actions\s+and\s+concise\s+fallback\s+answers\s+right\s+now\.?",
            "I can still help with local actions right now.",
            txt,
        )
        txt = re.sub(
            r"(?i)\bwhat'?s\s+specific\s+task\s+would\s+you\s+like\s+to\s+focus\s+on[\s\S]*?let\s+me\s+know\b",
            "What would you like to do next?",
            txt,
        )
        txt = re.sub(r"(?i)\bdone\.\s*next\s*step\s*:\s*execution\s+queued\.[^\n]*", "", txt)
        txt = re.sub(r"\s{2,}", " ", txt).strip(" \n\t.-")
        if not txt:
            return ""

        # Deduplicate back-to-back repeated sentences.
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", txt) if p.strip()]
        if not parts:
            return txt
        deduped: list[str] = []
        for p in parts:
            if deduped and p.lower() == deduped[-1].lower():
                continue
            deduped.append(p)
        return " ".join(deduped).strip()

    def _naturalize_response_text(self, user_text: str, text: str, *, actions: list[dict] | None = None) -> str:
        txt = self._clean_conversational_noise(text)
        if not txt:
            return txt

        # Remove stiff lead-ins that make replies feel robotic.
        txt = re.sub(
            r"(?i)^\s*(certainly|of course|sure(?: thing)?|absolutely|definitely|understood|noted)\s*[:,\-]\s*",
            "",
            txt,
        ).strip()

        txt = re.sub(r"\s{2,}", " ", txt).strip()
        complexity = self._estimate_complexity(user_text, "chat")
        qtype = self._knowledge_query_type(user_text)
        has_actions = bool(isinstance(actions, list) and actions)

        # Keep simple conversational queries short and direct.
        if complexity == 0 and not has_actions and qtype == "general":
            txt = self._first_sentences(txt, max_sentences=2)
            if len(txt) > 220:
                txt = txt[:220].rstrip(" ,.;:") + "."

        # Optional follow-up suggestion for longer informational replies.
        if complexity >= 1 and not has_actions and qtype in {"general", "project", "task_history"}:
            low = txt.lower()
            if "if you want" not in low and "want me to" not in low:
                txt = txt.rstrip()
                if txt and txt[-1] not in ".!?":
                    txt += "."
                txt += " If you want, I can give a short next-step plan."

        return txt

    @staticmethod
    def _resolve_voice_personality(user_prefs: dict | None) -> str:
        try:
            if isinstance(user_prefs, dict):
                vp = str(user_prefs.get("voice_personality") or user_prefs.get("voice_style") or "").strip().lower()
                if vp in {"siri", "gemini", "warm", "professional", "natural"}:
                    return vp
                persona = str(user_prefs.get("persona") or "").strip().lower()
                if persona in {"analyst", "formal-gentle", "friendly"}:
                    return {
                        "analyst": "professional",
                        "formal-gentle": "siri",
                        "friendly": "warm",
                    }.get(persona, "natural")
        except Exception:
            pass
        return "natural"

    def _apply_voice_reply_style(
        self,
        *,
        text: str,
        user_text: str,
        actions: list[dict] | None,
        user_prefs: dict | None,
        response_mode: str,
    ) -> str:
        if str(response_mode or "").strip().lower() != "voice":
            return str(text or "").strip()

        # Wake-word UX: keep a full friendly readiness line.
        wake_word = str(user_text or "").strip().lower()
        if re.match(r"^(jarvis|hey\s+jarvis|ok\s+jarvis|okay\s+jarvis|wake\s+up(?:\s+jarvis)?|jarvis\s+wake\s+up)[\s!?.]*$", wake_word):
            return "Hello, I'm here. How can I help you?"

        txt = self._clean_conversational_noise(text)
        if not txt:
            return txt

        acts = actions if isinstance(actions, list) else []
        low = txt.lower()
        if re.search(r"\b(permission|forbidden|denied)\b", low):
            return "I hit a permission block. Want me to try a safe alternative?"
        if re.search(r"\b(offline|not connected|unavailable|queued_for_agent|awaiting_agent)\b", low):
            return "Your device looks offline right now. I can queue this and run it when it reconnects."
        if re.search(r"\b(timeout|timed out)\b", low):
            return "That timed out. Want me to retry with a smaller step?"
        if re.search(r"\b(fail|failed|error|unable|cannot|can't|could not)\b", low):
            return "I could not finish that. Want me to retry now?"

        voice_personality = self._resolve_voice_personality(user_prefs)

        def _voice_contract(s: str) -> str:
            out = str(s or "").strip()
            if not out:
                return out
            repl = [
                (r"\bI am\b", "I'm"),
                (r"\bI will\b", "I'll"),
                (r"\bI have\b", "I've"),
                (r"\bdo not\b", "don't"),
                (r"\bcannot\b", "can't"),
                (r"\bcan not\b", "can't"),
                (r"\bwill not\b", "won't"),
            ]
            for pat, rep in repl:
                out = re.sub(pat, rep, out, flags=re.IGNORECASE)
            return re.sub(r"\s{2,}", " ", out).strip()

        def _pick(options: list[str], seed: str) -> str:
            if not options:
                return ""
            idx = sum(ord(c) for c in str(seed or "")) % len(options)
            return options[idx]

        if acts:
            first = acts[0] if acts and isinstance(acts[0], dict) else {}
            at = str(first.get("type") or "").strip().lower()
            lead = self._action_text_from_first_action(acts)
            lead = self._first_sentences(lead, max_sentences=1)
            lead = re.sub(r"(?i)^opening\s+that\.?$", "Opening that now.", lead)
            lead = re.sub(r"(?i)^searching\s+for\s+that\.?$", "Searching that now.", lead)
            lead = re.sub(r"(?i)^applying\s+that\s+now\.?$", "Applying that now.", lead)
            lead = re.sub(r"\s{2,}", " ", lead).strip()
            lead = _voice_contract(lead)
            if lead and not lead.endswith((".", "!", "?")):
                lead += "."

            natural_tail = ""
            if at in {"open_app", "open_url", "switch_app", "close_app"}:
                natural_tail = _pick([
                    "I'll stay with you for the next step.",
                    "Tell me the next step when you're ready.",
                    "Want me to continue after this opens?",
                ], user_text)
            elif at in {"device_action", "execute_command"}:
                natural_tail = _pick([
                    "I'll confirm once it finishes.",
                    "I'll keep this moving step by step.",
                    "Want me to continue automatically after this?",
                ], user_text)
            elif at == "web_search":
                natural_tail = _pick([
                    "I'll give you a short summary right after.",
                    "I'll pull the key points for you.",
                    "I'll keep it concise.",
                ], user_text)

            if voice_personality == "siri":
                return lead if lead.endswith(".") else (lead + ".")
            if voice_personality == "professional":
                return (lead if lead.endswith(".") else (lead + ".")) + " " + _pick([
                    "I'll confirm once it's complete.",
                    "I'll share the result right away.",
                ], user_text)
            if voice_personality == "warm":
                return (lead if lead.endswith(".") else (lead + ".")) + " " + _pick([
                    "I'm here if you want the next step.",
                    "Want me to keep going?",
                ], user_text)
            if voice_personality == "gemini":
                return (lead if lead.endswith(".") else (lead + ".")) + " " + _pick([
                    "I'll keep you updated.",
                    "I'll continue as soon as you say go.",
                ], user_text)

            if natural_tail:
                return f"{lead} {natural_tail}".strip()
            return lead if lead.endswith(".") else (lead + ".")

        txt = re.sub(r"(?i)\bif you want,? i can give a short next-step plan\.?", "", txt)
        txt = re.sub(r"(?i)\bwhen you're ready,\s*", "", txt)
        txt = re.sub(r"(?i)\bnext step:\s*", "Then ", txt)
        txt = re.sub(r"(?i)\bauto mode:\s*", "", txt)
        txt = txt.replace("\n", " ").strip()
        txt = _voice_contract(txt)
        max_sentences = 2 if self._classify_primary_intent(user_text) == "informational_intent" else 1
        txt = self._first_sentences(txt, max_sentences=max_sentences)
        txt = re.sub(r"\s{2,}", " ", txt).strip(" \t\n")
        if len(txt) > 190:
            txt = txt[:190].rstrip(" ,.;:") + "."
        if txt and not txt.endswith((".", "?", "!")):
            txt += "."

        if voice_personality == "siri":
            return txt
        if voice_personality == "professional":
            return txt
        if voice_personality == "warm":
            if not txt.endswith((".", "?", "!")):
                txt += "."
            return txt
        if voice_personality == "gemini":
            if not txt.endswith((".", "?", "!")):
                txt += "."
            return txt
        return txt

    def _sanitize_output_text(self, user_text: str, parsed: dict) -> dict:
        out = dict(parsed or {})
        txt = str(out.get("text") or "").strip()
        if not txt:
            return out

        actions = out.get("actions") if isinstance(out.get("actions"), list) else []
        has_url_action = any(
            isinstance(a, dict) and str(a.get("type") or "").strip().lower() in {"open_url", "fetch_url", "web_search"}
            for a in actions
        )
        user_wants_url = bool(re.search(r"\b(url|link|website|site|open\s+http|https?://)\b", str(user_text or "").lower()))

        # Remove unsolicited URLs/markdown links from free-text replies to keep responses relevant.
        if not has_url_action and not user_wants_url:
            txt = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", txt)
            txt = re.sub(r"https?://\S+", "", txt).strip()
            txt = re.sub(r"\s{2,}", " ", txt).strip()

        primary_intent = self._classify_primary_intent(user_text)
        profile = self._classify_intent_profile(user_text)
        if str(profile.get("intent_type") or "") == "informational":
            explicit_execute = bool(re.search(r"\b(open|run|execute|launch|start|go\s+to|visit)\b", str(user_text or "").lower()))
            explicit_research = bool(
                re.search(
                    r"\b(research|look\s+up|search\s+(for|online)?|latest|current|today|sources?|citations?|links?)\b",
                    str(user_text or "").lower(),
                )
            )
            if not explicit_execute:
                filtered_actions = []
                for a in actions:
                    if not isinstance(a, dict):
                        continue
                    at = str(a.get("type") or "").strip().lower()
                    if at in {"open_app", "open_url", "execute_command", "device_action", "switch_app", "close_app"}:
                        continue
                    if (not explicit_research) and at in {"web_search", "fetch_url", "search"}:
                        continue
                    filtered_actions.append(a)
                if filtered_actions != actions:
                    actions = filtered_actions
                    out["actions"] = filtered_actions

        # Safety guard: for plain chat/greeting/informational prompts, do not emit
        # typing-style execution actions unless user explicitly asked to type/write.
        try:
            intent_type = str(profile.get("intent_type") or "").strip().lower()
            explicit_type_request = bool(
                re.search(r"\b(type|write|paste|enter|fill|input|insert|replace\s+text)\b", str(user_text or "").lower())
            )
            if intent_type in {"informational", "ambiguous"} and not explicit_type_request:
                filtered_actions = []
                for a in actions:
                    if not isinstance(a, dict):
                        continue
                    at = str(a.get("type") or "").strip().lower()
                    if at in {"type_text", "press_key", "hotkey"}:
                        continue
                    filtered_actions.append(a)
                if filtered_actions != actions:
                    actions = filtered_actions
                    out["actions"] = filtered_actions
        except Exception:
            pass

        if primary_intent != "informational_intent":
            txt = re.sub(r"(?im)^\s*I\s+found\s+this\s*:?\s*", "", txt).strip()
            txt = re.sub(r"(?im)^\s*Risks\s*/\s*assumptions\s*:?\s*", "", txt).strip()
            txt = re.sub(r"(?im)^\s*Decision\s*/\s*next\s+steps\s*:?\s*", "", txt).strip()
            txt = re.sub(r"(?im)^\s*Source\s+URLs\s*:?\s*", "", txt).strip()

        qtype = self._knowledge_query_type(user_text)
        if qtype in {"debug", "system_behavior"}:
            low = txt.lower()
            if "cause:" not in low or "fix:" not in low or "next step:" not in low:
                clean = txt.strip() or "Issue detected."
                txt = (
                    "Cause: " + clean + "\n"
                    "Fix: Apply the smallest safe correction and verify with one retry.\n"
                    "Next step: Share the exact failing line/output for a targeted patch."
                )
        else:
            txt = self._naturalize_response_text(user_text, txt, actions=actions)

        if str(profile.get("intent_type") or "") == "informational" and (not actions):
            txt = self._first_sentences(txt, max_sentences=2)
            if len(txt) > 320:
                txt = txt[:320].rstrip(" ,.;:") + "."

        out["text"] = txt
        return out

    async def _ensure_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
    
    async def _ensure_decision_maker(self):
        """Initialize decision maker on first use"""
        if DECISION_MAKER_AVAILABLE and self.decision_maker is None:
            try:
                self.decision_maker = ContextAwareDecisionMaker()
                await self.decision_maker.initialize()
            except Exception as e:
                print(f"[LLMAdapter] Decision maker init failed: {e}")
        return self.decision_maker

    async def close(self):
        """Close the underlying HTTP session (best-effort)."""
        try:
            if self.session:
                await self.session.close()
        finally:
            self.session = None

    async def _call_openai(
        self,
        messages,
        *,
        max_tokens: int,
        temperature: float,
        model: str | None = None,
        endpoint: str | None = None,
        api_key: str | None = None,
    ):
        """Call an OpenAI-compatible Chat Completions endpoint.

        NOTE: Tests monkeypatch this method to simulate model outages. Keep this as
        the single choke point for model calls.
        """
        return await self._call_openai_with_model(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
            endpoint=endpoint,
            api_key=api_key,
        )

    async def _call_openai_with_model(
        self,
        messages,
        *,
        max_tokens: int,
        temperature: float,
        model: str | None,
        endpoint: str | None,
        api_key: str | None,
    ):
        await self._ensure_session()
        resolved_key = api_key if api_key is not None else self.primary_key
        resolved_endpoint = endpoint if endpoint is not None else self.primary_endpoint
        if not resolved_key:
            raise Exception(
                "Missing API key. Set OPENAI_API_KEY/PRIMARY_API_KEY for primary, "
                "and GROQ_API_KEY/BACKUP_API_KEY for fallback."
            )
        headers = {
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": (model or self.primary_model),
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        async with self.session.post(resolved_endpoint, json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"OpenAI API error: {await r.text()}")
            return await r.json()

    @staticmethod
    def _is_transient_provider_error(err: Exception) -> bool:
        msg = str(err or "").strip().lower()
        if not msg:
            return False
        transient_markers = (
            "timeout",
            "timed out",
            "connection",
            "temporarily",
            "temporary",
            "too many requests",
            "rate limit",
            "429",
            "500",
            "502",
            "503",
            "504",
            "server error",
        )
        return any(m in msg for m in transient_markers)

    def _choose_model_for_request(self, text: str, mode: str) -> str:
        """Route to a stronger model for complex tasks when configured."""
        try:
            complexity = self._estimate_complexity(text, mode)
            if self.fast_model and complexity == 0 and (mode or "").lower() != "voice":
                return self.fast_model
            if not self.smart_model:
                return self.primary_model
            if complexity >= self.smart_model_min_complexity:
                return self.smart_model
        except Exception:
            pass
        return self.primary_model

    @staticmethod
    def _map_model_id_to_runtime_model(model_id: str, *, primary_model: str, smart_model: str, backup_model: str) -> str:
        mid = str(model_id or "").strip().lower()
        mapping = {
            "ollama_llama3_1_8b": "llama3.1:8b",
            "ollama_qwen2_5_7b": "qwen2.5:7b",
            "openai_compatible_primary": primary_model or smart_model,
            "openai_compatible_backup": backup_model or primary_model,
            "local_tiny_fallback": "local-fast-path",
        }
        resolved = mapping.get(mid)
        if resolved:
            return resolved
        return model_id or primary_model

    def _resolve_model_ops_route(self, text: str, mode: str) -> dict | None:
        if not self.model_ops_routing_enabled:
            return None
        if not callable(model_ops_resolve_route):
            return None
        try:
            route = model_ops_resolve_route(text=text, mode=mode)
            if isinstance(route, dict):
                self._last_model_ops_route = route
                return route
        except Exception:
            return None
        return None

    @staticmethod
    def _classify_primary_intent(user_text: str) -> str:
        """Classify top-level intent with strict routing priority.

        Priority:
        1) action_intent
        2) generation_intent
        3) clarification_intent
        4) informational_intent
        """
        tl = (user_text or "").strip().lower()
        if not tl:
            return "clarification_intent"

        action_intent = bool(
            re.search(
                r"\b(open|launch|start|run|execute|close|quit|exit|switch\s+to|focus|go\s+to|"
                r"take\s+(a\s+)?screenshot|capture\s+screen|set\s+(volume|brightness)|"
                r"turn\s+on|turn\s+off|enable|disable|lock\s+screen|restart|shutdown|"
                r"open\s+settings|create\s+folder|make\s+folder|delete\s+file|read\s+file|list\s+files)\b",
                tl,
            )
        )

        generation_intent = bool(
            re.search(
                r"\b(write|draft|compose|generate|create|make)\b.*\b(email|mail|message|reply|"
                r"post|content|letter|proposal|summary|bio|description|code|snippet|text)\b",
                tl,
            )
            or re.search(r"\b(email|mail)\b.*\b(write|draft|compose|generate|create|send)\b", tl)
        )

        informational_intent = bool(
            re.search(r"\b(what|why|how|when|where|which|who)\b", tl)
            or re.search(r"\b(explain|define|meaning|research|documentation|docs|guide|tutorial)\b", tl)
        )

        clarification_intent = bool(
            re.fullmatch(r"(do it|do this|do that|same as before|like before|continue|go ahead)\.?", tl)
            or re.search(r"\b(this|that|it)\b", tl)
        )

        if action_intent:
            return "action_intent"
        if generation_intent:
            return "generation_intent"
        if clarification_intent:
            return "clarification_intent"
        if informational_intent:
            return "informational_intent"
        return "clarification_intent"

    @staticmethod
    def _classify_intent_profile(user_text: str) -> dict:
        tl = (user_text or "").strip().lower()
        wc = len(re.findall(r"\w+", tl))
        if not tl:
            return {
                "intent_type": "ambiguous",
                "intent_depth": "low",
                "response_strategy": "clarify",
            }

        primary_intent = LLMAdapter._classify_primary_intent(user_text)
        direct_action = primary_intent == "action_intent"
        generation_intent = primary_intent == "generation_intent"
        informational = primary_intent == "informational_intent"

        goal_oriented = bool(
            re.search(
                r"\b(i\s+want\s+to|help\s+me\s+to|learn|improve|fix|achieve|master|"
                r"become|build\s+a\s+habit|get\s+better|plan\s+for|roadmap)\b",
                tl,
            )
        )
        ambiguous = bool(primary_intent == "clarification_intent")
        if not ambiguous:
            ambiguous = bool(
                re.fullmatch(r"(do it|do this|do that|same|continue|go ahead|it|that|this)\.?", tl)
                or (wc <= 2 and not direct_action and not goal_oriented and not informational)
            )

        if direct_action:
            intent_type = "direct_action"
            strategy = "execute_immediately"
        elif generation_intent:
            intent_type = "goal_oriented" if goal_oriented else "informational"
            strategy = "explain_plus_plan_plus_optional_execution" if goal_oriented else "concise_explanation_only"
        elif goal_oriented:
            intent_type = "goal_oriented"
            strategy = "explain_plus_plan_plus_optional_execution"
        elif informational:
            intent_type = "informational"
            strategy = "concise_explanation_only"
        elif ambiguous:
            intent_type = "ambiguous"
            strategy = "clarify"
        else:
            intent_type = "ambiguous"
            strategy = "clarify"

        depth = "low"
        if wc >= 11 or re.search(r"\b(and|then|after|before|because|so\s+that)\b", tl):
            depth = "medium"
        if wc >= 22 or re.search(r"\b(plan|roadmap|step\s+by\s+step|long\s+term|deep)\b", tl):
            depth = "high"

        return {
            "intent_type": intent_type,
            "intent_depth": depth,
            "response_strategy": strategy,
        }

    @staticmethod
    def _intent_confidence_from_profile(intent_profile: dict, user_text: str, context_state: dict | None) -> float:
        profile = intent_profile if isinstance(intent_profile, dict) else {}
        intent_type = str(profile.get("intent_type") or "ambiguous").strip().lower()
        depth = str(profile.get("intent_depth") or "low").strip().lower()
        text = str(user_text or "").strip()
        tl = text.lower()

        base = 0.55
        if intent_type == "direct_action":
            base = 0.86
        elif intent_type == "goal_oriented":
            base = 0.74
        elif intent_type == "informational":
            base = 0.78
        elif intent_type == "ambiguous":
            base = 0.42

        if depth == "medium":
            base += 0.03
        elif depth == "high":
            base -= 0.04

        if LLMAdapter._looks_like_continuation_request(tl) and isinstance(context_state, dict):
            if bool(context_state.get("task_active")):
                base += 0.08
            if str(context_state.get("last_entity") or "").strip() or str(context_state.get("active_app") or "").strip():
                base += 0.04

        if not text:
            base = 0.15
        return max(0.05, min(0.98, float(base)))

    def _build_unified_intelligence_state(self, *, user_text: str, context_state: dict | None, user_prefs: dict | None) -> dict:
        profile = self._classify_intent_profile(user_text)
        proactive_mode = self._resolve_proactive_mode(user_prefs, context_state)
        continuation = self._looks_like_continuation_request(user_text)
        interrupt = self._is_interrupt_instruction(user_text)
        new_task = self._looks_like_new_task(user_text, context_state)
        confidence = self._intent_confidence_from_profile(profile, user_text, context_state)

        return {
            "intent_type": str(profile.get("intent_type") or "ambiguous"),
            "intent_depth": str(profile.get("intent_depth") or "low"),
            "response_strategy": str(profile.get("response_strategy") or "clarify"),
            "primary_intent": self._classify_primary_intent(user_text),
            "confidence": float(confidence),
            "proactive_mode": proactive_mode,
            "continuation": bool(continuation),
            "interrupt": bool(interrupt),
            "new_task": bool(new_task),
        }

    @staticmethod
    def _default_runtime_task_context() -> dict:
        return {
            "active_app": "",
            "last_entity": "",
            "last_action": "",
            "last_result": "",
            "last_result_status": "",
            "task_chain": [],
            "task_active": False,
            "task_goal": "",
            "task_step": 0,
            "task_status": "",
            "next_possible_actions": [],
            "goal_progress": 0.0,
            "proactive_mode": "assist",
            "pending_clarification_kind": "",
            "pending_clarification_question": "",
            "pending_clarification_original_text": "",
            "expires_at": "",
            "updated_at": "",
            "last_resolved_input": "",
        }

    @staticmethod
    def _has_pending_clarification(context_state: dict | None) -> bool:
        if not isinstance(context_state, dict):
            return False
        return bool(str(context_state.get("pending_clarification_original_text") or "").strip())

    @staticmethod
    def _clear_pending_clarification(context_state: dict) -> dict:
        st = deepcopy(context_state if isinstance(context_state, dict) else {})
        st["pending_clarification_kind"] = ""
        st["pending_clarification_question"] = ""
        st["pending_clarification_original_text"] = ""
        return st

    @staticmethod
    def _resolve_proactive_mode(user_prefs: dict | None, context_state: dict | None) -> str:
        valid = {"manual", "assist", "auto"}
        if isinstance(user_prefs, dict):
            cand = str(user_prefs.get("proactive_mode") or user_prefs.get("assistant_mode") or "").strip().lower()
            if cand in valid:
                return cand
        if isinstance(context_state, dict):
            cand = str(context_state.get("proactive_mode") or "").strip().lower()
            if cand in valid:
                return cand
        return "assist"

    @staticmethod
    def _is_explicit_execution_confirmation(user_text: str) -> bool:
        tl = str(user_text or "").strip().lower()
        if not tl:
            return False
        return bool(re.search(r"\b(go ahead|continue|execute|run it|proceed|yes do it|confirm)\b", tl))

    @staticmethod
    def _is_interrupt_instruction(user_text: str) -> bool:
        tl = str(user_text or "").strip().lower()
        if not tl:
            return False
        return bool(re.search(r"\b(stop|cancel|pause|hold|abort|never mind|forget that|forget it)\b", tl))

    def _looks_like_new_task(self, user_text: str, context_state: dict | None) -> bool:
        tl = str(user_text or "").strip().lower()
        if not tl:
            return False
        if not isinstance(context_state, dict) or not bool(context_state.get("task_active")):
            return False
        if self._looks_like_continuation_request(tl) or self._is_explicit_execution_confirmation(tl):
            return False
        if self._is_interrupt_instruction(tl):
            return False

        active_app = str(context_state.get("active_app") or "").strip().lower()
        new_action_intent = self._classify_primary_intent(tl) == "action_intent"
        mentions_active_app = bool(active_app and re.search(rf"\b{re.escape(active_app)}\b", tl))
        return bool(new_action_intent and not mentions_active_app)

    def _override_active_task_context(self, *, context_state: dict, reason: str, user_prefs: dict | None) -> dict:
        st = deepcopy(context_state if isinstance(context_state, dict) else self._default_runtime_task_context())
        now = datetime.now(timezone.utc)
        st["task_active"] = False
        st["task_status"] = "paused" if reason == "interrupt" else "replaced"
        st["task_goal"] = ""
        st["task_step"] = 0
        st["next_possible_actions"] = []
        st["updated_at"] = now.isoformat()
        st["expires_at"] = (now + timedelta(seconds=self._runtime_context_ttl_seconds())).isoformat()
        self._set_runtime_task_context(user_prefs, st)
        return st

    @staticmethod
    def _extract_url_from_result_payload(payload: dict | None) -> str:
        if not isinstance(payload, dict):
            return ""
        url = str(payload.get("url") or "").strip()
        if url:
            return url
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if isinstance(data, dict):
            url = str(data.get("url") or "").strip()
            if url:
                return url
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
        for item in results[:3]:
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                if url:
                    return url
        return ""

    def _derive_auto_next_action(self, context_state: dict | None) -> dict | None:
        if not isinstance(context_state, dict):
            return None
        if not bool(context_state.get("task_active")):
            return None

        next_actions = context_state.get("next_possible_actions") if isinstance(context_state.get("next_possible_actions"), list) else []
        hint = str(next_actions[0] if next_actions else "").strip().lower()
        active_app = str(context_state.get("active_app") or "").strip()
        last_result = context_state.get("last_result") if isinstance(context_state.get("last_result"), dict) else {}

        if "open best result" in hint:
            url = self._extract_url_from_result_payload(last_result)
            if url:
                return {"type": "open_url", "url": url}

        if "verify system result" in hint and active_app:
            return {"type": "switch_app", "app_name": active_app}

        if "continue to next step" in hint and active_app:
            return {"type": "switch_app", "app_name": active_app}

        return None

    @staticmethod
    def _runtime_context_ttl_seconds() -> int:
        try:
            ttl = int(getattr(rd, "RUNTIME_TASK_CONTEXT_TTL_SECONDS", 1800) or 1800)
            return max(120, ttl)
        except Exception:
            return 1800

    @staticmethod
    def _is_runtime_context_expired(context_state: dict | None) -> bool:
        if not isinstance(context_state, dict):
            return True
        raw = str(context_state.get("expires_at") or "").strip()
        if not raw:
            return False
        try:
            expires_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc)
        except Exception:
            return False

    def _runtime_task_context_scope(self, user_prefs: dict | None) -> str:
        try:
            user_key = self._extract_user_scope_key(user_prefs)
            if user_key:
                return f"user:{user_key}"
        except Exception:
            pass
        return "global"

    def _get_runtime_task_context(self, user_prefs: dict | None) -> dict:
        key = self._runtime_task_context_scope(user_prefs)
        state = self._runtime_task_contexts.get(key)
        if (not isinstance(state, dict)) or self._is_runtime_context_expired(state):
            state = self._default_runtime_task_context()
            self._runtime_task_contexts[key] = state
        return state

    def _set_runtime_task_context(self, user_prefs: dict | None, context_state: dict) -> None:
        key = self._runtime_task_context_scope(user_prefs)
        self._runtime_task_contexts[key] = context_state if isinstance(context_state, dict) else self._default_runtime_task_context()

    @staticmethod
    def _extract_context_entity(user_text: str) -> str:
        t = str(user_text or "").strip()
        if not t:
            return ""
        stop_tokens = {"it", "this", "that", "app", "application", "site", "website", "browser"}

        def _clean(v: str) -> str:
            cand = re.sub(r"\s+", " ", str(v or "").strip()).strip(" .,;:!?\t\n\r")
            low = cand.lower()
            if (not cand) or (low in stop_tokens):
                return ""
            return cand

        for pat in (
            r'"([^\"]{2,80})"',
            r"'([^']{2,80})'",
            r"\bto\s+([a-zA-Z0-9._@-]{2,80})\b",
            r"\bfor\s+([a-zA-Z0-9._@-]{2,80})\b",
        ):
            m = re.search(pat, t, flags=re.IGNORECASE)
            if m:
                out = _clean(str(m.group(1) or ""))
                if out:
                    return out

        # Also capture direct app-like command targets: "open gemini", "switch to chrome".
        m = re.search(
            r"\b(?:open|launch|start|switch\s+to|switch|close|quit|exit)\b\s+(.+)$",
            t,
            flags=re.IGNORECASE,
        )
        if m:
            raw = str(m.group(1) or "")
            raw = re.split(r"\b(?:and\s+then|then|so|because|but|and|after\s+that)\b", raw, maxsplit=1, flags=re.IGNORECASE)[0]
            raw = raw.strip()
            # Keep only the leading token-ish phrase to avoid absorbing full sentence tails.
            raw = re.sub(r"[^a-zA-Z0-9._@\-\s].*$", "", raw).strip()
            out = _clean(raw)
            if out:
                return out
        return ""

    @staticmethod
    def _looks_like_continuation_request(user_text: str) -> bool:
        tl = str(user_text or "").strip().lower()
        if not tl:
            return False
        if re.search(r"\b(this|that|same|continue|it|again)\b", tl):
            return True
        return bool(re.fullmatch(r"(do it|do this|do that|same|continue|go ahead)\.?", tl))

    def _resolve_contextual_user_text(self, user_text: str, context_state: dict | None) -> str:
        text = str(user_text or "").strip()
        if not text or not isinstance(context_state, dict):
            return text

        active_app = str(context_state.get("active_app") or "").strip()
        last_entity = str(context_state.get("last_entity") or "").strip()
        last_action = str(context_state.get("last_action") or "").strip()

        fallback_entity = ""
        if not last_entity and last_action:
            m = re.match(r"^(?:open_app|switch_app|close_app):(.+)$", last_action, flags=re.IGNORECASE)
            if m:
                fallback_entity = str(m.group(1) or "").strip()

        resolved = text
        if last_entity:
            resolved = re.sub(r"\b(this\s+person|this\s+contact|that\s+person|that\s+contact)\b", last_entity, resolved, flags=re.IGNORECASE)

        pronoun_target = last_entity or fallback_entity or active_app
        if pronoun_target and self._looks_like_continuation_request(text):
            # Convert continuation pronouns into explicit commands using prior task entity.
            pronoun = r"(?:it|that|this|the\s+app|app)"
            resolved = re.sub(rf"\bopen\s+{pronoun}\b", f"open {pronoun_target}", resolved, flags=re.IGNORECASE)
            resolved = re.sub(rf"\blaunch\s+{pronoun}\b", f"launch {pronoun_target}", resolved, flags=re.IGNORECASE)
            resolved = re.sub(rf"\bstart\s+{pronoun}\b", f"start {pronoun_target}", resolved, flags=re.IGNORECASE)
            resolved = re.sub(rf"\bswitch\s+to\s+{pronoun}\b", f"switch to {pronoun_target}", resolved, flags=re.IGNORECASE)
            resolved = re.sub(rf"\bclose\s+{pronoun}\b", f"close {pronoun_target}", resolved, flags=re.IGNORECASE)

            # Failure/retry phrasing should resolve into a concrete retry action target.
            if re.search(r"\b(try\s+again|retry|again|did(?:\s+not|n't)\s+open|not\s+open(?:ed)?|failed\s+to\s+open)\b", resolved, flags=re.IGNORECASE):
                has_explicit_open = bool(
                    re.search(r"\b(open|launch|start)\s+" + re.escape(pronoun_target) + r"\b", resolved, flags=re.IGNORECASE)
                )
                if not has_explicit_open:
                    resolved = f"open {pronoun_target}"

        if self._looks_like_continuation_request(text):
            hints: list[str] = []
            if active_app:
                hints.append(f"active_app={active_app}")
            if last_entity:
                hints.append(f"last_entity={last_entity}")
            if last_action:
                hints.append(f"last_action={last_action}")
            if hints:
                resolved = f"{resolved}\nContext hints: {'; '.join(hints)}"

        return resolved

    @staticmethod
    def _runtime_context_prompt_block(context_state: dict | None) -> str:
        if not isinstance(context_state, dict):
            return "(none)"
        chain = context_state.get("task_chain") if isinstance(context_state.get("task_chain"), list) else []
        recent_chain = chain[-3:]
        chain_lines = []
        for item in recent_chain:
            if not isinstance(item, dict):
                continue
            chain_lines.append(
                f"- input={str(item.get('input') or '').strip()} | actions={str(item.get('actions') or '').strip()} | result={str(item.get('result') or '').strip()}"
            )
        if not chain_lines:
            chain_lines = ["- (none)"]
        return (
            f"active_app: {str(context_state.get('active_app') or '(none)')}\n"
            f"last_entity: {str(context_state.get('last_entity') or '(none)')}\n"
            f"last_action: {str(context_state.get('last_action') or '(none)')}\n"
            f"last_result: {str(context_state.get('last_result') or '(none)')}\n"
            f"last_result_status: {str(context_state.get('last_result_status') or '(none)')}\n"
            f"task_active: {str(bool(context_state.get('task_active'))).lower()}\n"
            f"task_goal: {str(context_state.get('task_goal') or '(none)')}\n"
            f"task_step: {int(context_state.get('task_step') or 0)}\n"
            f"task_status: {str(context_state.get('task_status') or '(none)')}\n"
            f"goal_progress: {float(context_state.get('goal_progress') or 0.0):.2f}\n"
            f"next_possible_actions: {', '.join([str(x) for x in (context_state.get('next_possible_actions') or [])[:2]]) or '(none)'}\n"
            "task_chain:\n"
            + "\n".join(chain_lines)
        )

    @staticmethod
    def _infer_task_goal(user_text: str, planning_text: str) -> str:
        base = str(user_text or "").strip() or str(planning_text or "").strip()
        if not base:
            return ""
        goal = re.sub(r"\s+", " ", base).strip()
        goal = re.sub(r"(?i)\b(please|jarvis|can you|could you|would you)\b", "", goal).strip()
        if len(goal) > 140:
            goal = goal[:140].rstrip(" ,.;:")
        return goal

    @staticmethod
    def _infer_high_level_goal(user_text: str, actions: list[dict] | None = None) -> str:
        text = str(user_text or "").strip().lower()
        acts = actions if isinstance(actions, list) else []
        action_types = {
            str(a.get("type") or "").strip().lower()
            for a in acts
            if isinstance(a, dict)
        }

        if re.search(r"\b(message|chat|email|mail|reply|call|contact)\b", text):
            return "communicate"
        if re.search(r"\b(search|research|find|look\s+up|analyz|compare|docs|documentation)\b", text):
            return "research"
        if re.search(r"\b(write|draft|compose|format|rewrite|summarize|create\s+content)\b", text):
            return "create_content"
        if re.search(r"\b(file|folder|read|write|edit|delete|move|copy|path)\b", text):
            return "file_management"
        if re.search(r"\b(open|launch|switch|set|turn\s+on|turn\s+off|settings|device|system)\b", text):
            return "system_control"

        if "web_search" in action_types or "fetch_url" in action_types:
            return "research"
        if "type_text" in action_types or "generate_email" in action_types:
            return "create_content"
        if {"read", "write", "edit", "delete", "move", "copy"} & action_types:
            return "file_management"
        if {"open_app", "switch_app", "device_action", "open_url"} & action_types:
            return "system_control"

        return "assist"

    @staticmethod
    def _estimate_goal_progress(*, task_status: str, task_step: int, prev_progress: float) -> float:
        status = str(task_status or "").strip().lower()
        if status == "completed":
            return 1.0
        if status == "failed":
            return max(0.05, min(0.9, float(prev_progress or 0.0)))
        if task_step <= 0:
            return max(0.0, min(0.2, float(prev_progress or 0.0)))
        inferred = min(0.9, 0.2 + (0.2 * max(0, int(task_step) - 1)))
        return max(float(prev_progress or 0.0), inferred)

    @staticmethod
    def _compute_next_possible_actions(*, goal: str, last_action: str, status: str) -> list[str]:
        s = str(status or "").strip().lower()
        if s == "failed":
            return [
                "retry with safer fallback",
                "switch to a simpler step",
            ]

        g = str(goal or "").strip().lower()
        la = str(last_action or "").strip().lower()

        if "web_search" in la or "fetch_url" in la or g == "research":
            return ["open best result", "refine query"]
        if "open_app" in la or "switch_app" in la or g == "communicate":
            return ["send a message", "start a call"]
        if g == "create_content" or "type_text" in la:
            return ["refine draft", "send or save output"]
        if g == "file_management" or any(x in la for x in ["read", "write", "edit", "list", "delete"]):
            return ["apply next file change", "verify file state"]
        if g == "system_control" or "device_action" in la:
            return ["apply next setting", "verify system result"]
        return ["continue to next step", "review result"]

    @staticmethod
    def _execution_result_success(item: dict) -> bool | None:
        if not isinstance(item, dict):
            return None
        if isinstance(item.get("success"), bool):
            return bool(item.get("success"))
        st = str(item.get("status") or "").strip().lower()
        if st in {"success", "completed", "done", "opened", "sent", "ok"}:
            return True
        if st in {"failed", "error", "denied", "timeout", "forbidden"}:
            return False
        return None

    @staticmethod
    def _execution_result_message(item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        msg = (
            str(item.get("message") or "").strip()
            or str(item.get("error") or "").strip()
            or str(item.get("detail") or "").strip()
        )
        return msg

    @staticmethod
    def _derive_recovery_actions_from_execution_result(item: dict | None) -> list[dict]:
        if not isinstance(item, dict):
            return []
        action_type = str(item.get("action_type") or item.get("action") or "").strip().lower()
        message = str(item.get("message") or item.get("error") or "").strip().lower()

        if action_type in {"open_url"}:
            url = str(item.get("url") or "").strip()
            if url:
                return [{"type": "open_url", "url": url}]

        if action_type in {"web_search", "search"}:
            query = str(item.get("query") or "").strip()
            if query:
                return [{"type": "web_search", "query": query, "num_results": 5}]

        if action_type in {"open_app", "switch_app", "close_app"}:
            app_name = str(item.get("app_name") or item.get("app") or "").strip()
            if app_name and action_type in {"open_app", "switch_app"}:
                return [{"type": "open_app", "app_name": app_name, "args": []}]

        if action_type in {"device_action"}:
            name = str(item.get("name") or item.get("device_action") or "").strip().lower()
            if name:
                # Keep retries conservative: re-issue exact final-effect action once.
                return [{"type": "device_action", "name": name, "args": {}}]

        if "timeout" in message and action_type in {"fetch_url"}:
            url = str(item.get("url") or "").strip()
            if url:
                return [{"type": "fetch_url", "url": url}]

        return []

    def _derive_autonomous_followup_actions(
        self,
        *,
        context_state: dict | None,
        execution_results: list[dict] | None,
        proactive_mode: str,
        confidence: float,
    ) -> list[dict]:
        if not isinstance(context_state, dict):
            return []
        mode = str(proactive_mode or "assist").strip().lower()
        conf = max(0.0, min(1.0, float(confidence or 0.0)))
        results = execution_results if isinstance(execution_results, list) else []
        if not results or not bool(context_state.get("task_active")):
            return []

        # Auto mode: continue safe obvious step when confidence is high.
        if mode == "auto" and conf >= 0.78:
            nxt = self._derive_auto_next_action(context_state)
            if isinstance(nxt, dict) and nxt.get("type"):
                return [nxt]

        # Failure branch: propose one conservative recovery action in assist/auto.
        fail = None
        for r in results:
            ok = self._execution_result_success(r if isinstance(r, dict) else {})
            if ok is False:
                fail = r if isinstance(r, dict) else None
                break
        if fail is not None and mode in {"assist", "auto"}:
            return self._derive_recovery_actions_from_execution_result(fail)

        return []

    @staticmethod
    def _looks_generic_completion_text(text: str) -> bool:
        tl = str(text or "").strip().lower()
        if not tl:
            return True
        return tl in {
            "done",
            "done.",
            "opening it.",
            "opening it",
            "applying that setting.",
            "i am on it.",
        }

    def ingest_execution_feedback(
        self,
        *,
        user_text: str,
        response: dict,
        execution_results: list[dict] | None,
        user_prefs: dict | None = None,
    ) -> dict:
        """Ingest real ActionExecutor results to refine context and final user response.

        Integration point: call after executor completes and before final response return.
        """
        out = dict(response or {})
        results = execution_results if isinstance(execution_results, list) else []
        if not results:
            return out

        success_count = 0
        fail_count = 0
        first_success = None
        first_fail = None
        for r in results:
            if not isinstance(r, dict):
                continue
            ok = self._execution_result_success(r)
            if ok is True:
                success_count += 1
                if first_success is None:
                    first_success = r
            elif ok is False:
                fail_count += 1
                if first_fail is None:
                    first_fail = r

        primary = first_fail if first_fail is not None else first_success
        primary_message = self._execution_result_message(primary or {})

        followup_actions: list[dict] = []

        # Update runtime context with actual execution outcomes.
        try:
            ctx = deepcopy(self._get_runtime_task_context(user_prefs))
            proactive_mode = self._resolve_proactive_mode(user_prefs, ctx)
            status = "failed" if fail_count > 0 and success_count == 0 else "success"
            if fail_count > 0 and success_count > 0:
                status = "failed"

            ctx["updated_at"] = datetime.now(timezone.utc).isoformat()
            ctx["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=self._runtime_context_ttl_seconds())).isoformat()
            ctx["last_result"] = primary if isinstance(primary, dict) else {"message": primary_message or "execution finished"}
            ctx["last_result_status"] = status
            if bool(ctx.get("task_active")):
                ctx["task_status"] = "failed" if status == "failed" else "in_progress"
            if status == "failed":
                ctx["next_possible_actions"] = self._compute_next_possible_actions(
                    goal=str(ctx.get("task_goal") or ""),
                    last_action=str(ctx.get("last_action") or ""),
                    status="failed",
                )[:2]
                ctx["goal_progress"] = max(0.05, float(ctx.get("goal_progress") or 0.0) - 0.1)
            else:
                ctx["next_possible_actions"] = self._compute_next_possible_actions(
                    goal=str(ctx.get("task_goal") or ""),
                    last_action=str(ctx.get("last_action") or ""),
                    status="in_progress",
                )[:2]
                ctx["goal_progress"] = min(1.0, float(ctx.get("goal_progress") or 0.0) + 0.2)

            ctx["next_possible_actions"] = self._personalize_next_possible_actions(
                base_actions=ctx.get("next_possible_actions") if isinstance(ctx.get("next_possible_actions"), list) else [],
                user_prefs=user_prefs,
                context_state=ctx,
            )[:2]

            feedback_conf = 0.9 if status == "success" else 0.7
            followup_actions = self._derive_autonomous_followup_actions(
                context_state=ctx,
                execution_results=results,
                proactive_mode=proactive_mode,
                confidence=feedback_conf,
            )

            if followup_actions:
                ctx["task_status"] = "in_progress"

            exec_actions = out.get("actions") if isinstance(out.get("actions"), list) else []
            self._update_user_habits_from_step(
                user_prefs=user_prefs,
                user_text=user_text,
                actions=exec_actions,
                success=(status == "success"),
            )

            self._set_runtime_task_context(user_prefs, ctx)
        except Exception:
            pass

        # Improve final user-facing text with real execution feedback.
        try:
            current_text = str(out.get("text") or "").strip()
            if primary_message:
                if fail_count > 0 and success_count == 0:
                    out["text"] = f"That did not work: {primary_message}. I can try a safer next step."
                elif self._looks_generic_completion_text(current_text):
                    out["text"] = primary_message
            elif fail_count > 0 and success_count == 0 and self._looks_generic_completion_text(current_text):
                out["text"] = "That step failed. I can recover with a safer path."

            if followup_actions:
                existing_actions = out.get("actions") if isinstance(out.get("actions"), list) else []
                if not existing_actions:
                    out["actions"] = followup_actions
                tl = str(out.get("text") or "").strip().lower()
                if fail_count > 0 and "safer" not in tl:
                    out["text"] = (str(out.get("text") or "").strip() + " I can retry a safer method.").strip()
                elif success_count > 0 and fail_count == 0 and "next step" not in tl:
                    out["text"] = (str(out.get("text") or "").strip() + " I can continue with the next step.").strip()
        except Exception:
            pass

        return out

    @staticmethod
    def _infer_result_status(text_result: str, has_actions: bool) -> str:
        tl = str(text_result or "").strip().lower()
        if re.search(r"\b(fail|failed|error|denied|unable|cannot|can't|could not|timeout)\b", tl):
            return "failed"
        # Do not infer success merely because actions were planned; actual execution
        # feedback arrives later via ingest_execution_feedback.
        if re.search(r"\b(done|completed|finished|sent|opened|applied|success|successful)\b", tl):
            return "success"
        return ""

    @staticmethod
    def _is_task_completion_text(text_result: str) -> bool:
        tl = str(text_result or "").strip().lower()
        if not tl:
            return False
        return bool(re.search(r"\b(completed|finished|all set|done|resolved|successfully)\b", tl))

    @staticmethod
    def _suggest_next_task_step(context_state: dict, parsed: dict) -> str:
        if not isinstance(context_state, dict):
            return ""
        status = str(context_state.get("task_status") or "").strip().lower()
        step = int(context_state.get("task_step") or 0)
        active = bool(context_state.get("task_active"))
        next_actions = context_state.get("next_possible_actions") if isinstance(context_state.get("next_possible_actions"), list) else []
        actions = parsed.get("actions") if isinstance(parsed, dict) else []

        if status in {"queued", "in_progress", "executing", "delegated"} and isinstance(actions, list) and actions:
            return "I queued that action and will share the result as soon as the device responds."

        if status == "failed":
            options = ", ".join([str(x) for x in next_actions[:2]]) if next_actions else "retry with safer fallback"
            return f"I can recover automatically: {options}. Would you like me to continue?"
        if not active:
            return ""
        if next_actions:
            if len(next_actions) > 1:
                return f"Based on your usual flow, the best next options are {next_actions[0]} or {next_actions[1]}."
            return f"Based on your usual flow, the best next option is {next_actions[0]}."
        if isinstance(actions, list) and actions:
            return f"Step {step} is queued. I can continue to step {step + 1} next."
        return "I can continue with the next step when you say continue."

    @staticmethod
    def _apply_assistant_personality_tone(text: str, *, task_active: bool, mode: str) -> str:
        t = str(text or "").strip()
        if not t:
            return t
        if not task_active:
            return t
        if mode not in {"assist", "auto"}:
            return t
        tl = t.lower().strip()
        if tl in {"done", "done.", "completed", "completed."}:
            return "Done. I can keep this flow going for you."
        if tl in {"opening it.", "opening it"}:
            return "Opening it now. I can handle the next step too."
        if tl in {"applying that setting.", "applying that setting"}:
            return "Applying that setting now."
        return t

    def _apply_confidence_execution_control(
        self,
        *,
        user_text: str,
        parsed: dict,
        context_state: dict | None,
        planning_confidence: float | None,
    ) -> dict:
        if not isinstance(parsed, dict):
            return parsed
        actions = parsed.get("actions")
        if not isinstance(actions, list) or not actions:
            return parsed

        confidence = planning_confidence if isinstance(planning_confidence, (int, float)) else None
        if confidence is None:
            confidence = 0.7

        try:
            threshold = float(getattr(rd, "CONTEXT_EXECUTION_MIN_CONFIDENCE", 0.35) or 0.35)
        except Exception:
            threshold = 0.35

        risky = False
        risky_device_names = {"shutdown", "restart", "sleep", "hibernate", "logoff"}
        for a in actions:
            if not isinstance(a, dict):
                continue
            at = str(a.get("type") or "").strip().lower()
            if at in {"execute_command", "delete"}:
                risky = True
                break
            if at == "device_action":
                name = str(a.get("name") or "").strip().lower()
                if name in risky_device_names:
                    risky = True
                    break

        if risky and float(confidence) < threshold:
            parsed["actions"] = []
            parsed["text"] = (
                "I understood the task, but confidence is low for a safe execution. "
                "Please confirm and I will continue with a guarded next step."
            )
            parsed["task_status_hint"] = "low_confidence_hold"
        return parsed

    @staticmethod
    def _action_label_for_context(action: dict) -> str:
        if not isinstance(action, dict):
            return ""
        t = str(action.get("type") or "").strip().lower()
        if not t:
            return ""
        if t in {"open_app", "switch_app", "close_app"}:
            name = str(action.get("app_name") or "").strip()
            return f"{t}:{name}" if name else t
        if t == "device_action":
            name = str(action.get("name") or "").strip()
            return f"device_action:{name}" if name else "device_action"
        if t in {"web_search", "fetch_url", "open_url", "read", "write", "edit", "list", "delete"}:
            arg = str(action.get("query") or action.get("url") or action.get("path") or "").strip()
            return f"{t}:{arg[:60]}" if arg else t
        return t

    def _apply_context_to_actions(self, user_text: str, parsed: dict, context_state: dict | None) -> dict:
        if not isinstance(parsed, dict):
            return parsed
        actions = parsed.get("actions")
        if not isinstance(actions, list) or not isinstance(context_state, dict):
            return parsed

        active_app = str(context_state.get("active_app") or "").strip()
        continuation = self._looks_like_continuation_request(user_text)
        patched_actions = [dict(a) if isinstance(a, dict) else a for a in actions]

        for act in patched_actions:
            if not isinstance(act, dict):
                continue
            t = str(act.get("type") or "").strip().lower()
            if t in {"switch_app", "close_app"} and not str(act.get("app_name") or "").strip() and active_app:
                act["app_name"] = active_app

        if continuation and active_app:
            has_focus_action = any(
                isinstance(a, dict) and str(a.get("type") or "").strip().lower() in {"open_app", "switch_app"}
                for a in patched_actions
            )
            has_interaction = any(
                isinstance(a, dict) and str(a.get("type") or "").strip().lower() in {"type_text", "press_key", "hotkey", "device_action"}
                for a in patched_actions
            )
            if has_interaction and not has_focus_action:
                patched_actions.insert(0, {"type": "switch_app", "app_name": active_app})

        parsed["actions"] = patched_actions
        return parsed

    def _update_runtime_task_context(self, *, user_text: str, planning_text: str, parsed: dict, user_prefs: dict | None) -> None:
        context_state = deepcopy(self._get_runtime_task_context(user_prefs))
        actions = parsed.get("actions") if isinstance(parsed, dict) else []
        if not isinstance(actions, list):
            actions = []

        now = datetime.now(timezone.utc)
        context_state["updated_at"] = now.isoformat()
        context_state["expires_at"] = (now + timedelta(seconds=self._runtime_context_ttl_seconds())).isoformat()
        context_state["last_resolved_input"] = str(planning_text or user_text or "").strip()

        entity = self._extract_context_entity(user_text)
        if not entity and planning_text and planning_text != user_text:
            entity = self._extract_context_entity(planning_text)
        if entity:
            context_state["last_entity"] = entity

        for act in actions:
            if not isinstance(act, dict):
                continue
            t = str(act.get("type") or "").strip().lower()
            if t in {"open_app", "switch_app"}:
                app_name = str(act.get("app_name") or "").strip()
                if app_name:
                    context_state["active_app"] = app_name
            elif t == "open_url":
                context_state["active_app"] = "browser"

        if actions:
            labels = [self._action_label_for_context(a) for a in actions if isinstance(a, dict)]
            labels = [x for x in labels if x]
            if labels:
                context_state["last_action"] = labels[-1]
                if not str(context_state.get("last_entity") or ""):
                    m = re.match(r"^(?:open_app|switch_app|close_app):(.+)$", str(labels[-1]), flags=re.IGNORECASE)
                    if m:
                        target = str(m.group(1) or "").strip()
                        if target and target.lower() not in {"it", "this", "that", "app", "application"}:
                            context_state["last_entity"] = target
            chain = context_state.get("task_chain") if isinstance(context_state.get("task_chain"), list) else []
            chain.append(
                {
                    "ts": context_state["updated_at"],
                    "input": str(user_text or "").strip()[:220],
                    "actions": labels[:6],
                    "result": str((parsed or {}).get("text") or "").strip()[:220],
                }
            )
            context_state["task_chain"] = chain[-8:]

        has_actions = bool(actions)
        result_text = str((parsed or {}).get("text") or "").strip()
        result_status = self._infer_result_status(result_text, has_actions)
        task_status_hint = str((parsed or {}).get("task_status_hint") or "").strip().lower()
        clarification = (parsed or {}).get("clarification") if isinstance((parsed or {}).get("clarification"), dict) else {}

        if clarification and not has_actions:
            context_state["pending_clarification_kind"] = str(clarification.get("kind") or "").strip()
            context_state["pending_clarification_question"] = str(clarification.get("question") or "").strip()
            original_for_resume = str(clarification.get("original_user_text") or user_text or planning_text or "").strip()
            context_state["pending_clarification_original_text"] = original_for_resume
            context_state["task_status"] = "waiting_for_details"
        elif has_actions:
            context_state = self._clear_pending_clarification(context_state)

        if task_status_hint == "provider_unavailable" and not has_actions:
            # Provider fallback with no executable action should not keep stale task state active.
            context_state["task_active"] = False
            context_state["task_goal"] = ""
            context_state["task_step"] = 0
            context_state["task_status"] = "waiting_for_input"
            context_state["goal_progress"] = 0.0
            context_state["next_possible_actions"] = []
            context_state["last_result_status"] = ""
            if result_text:
                context_state["last_result"] = self._first_sentences(result_text, max_sentences=1)[:220]
            self._set_runtime_task_context(user_prefs, context_state)
            return

        if result_status:
            context_state["last_result_status"] = result_status

        task_goal = str(context_state.get("task_goal") or "").strip()
        task_active = bool(context_state.get("task_active"))
        task_step = int(context_state.get("task_step") or 0)

        if has_actions and not task_goal:
            task_goal = self._infer_high_level_goal(user_text=user_text, actions=actions)
            if not task_goal:
                task_goal = self._infer_task_goal(user_text=user_text, planning_text=planning_text)

        if has_actions:
            task_active = True
            task_step = max(1, task_step + 1)
            if result_status == "failed":
                context_state["task_status"] = "failed"
            elif result_status == "success":
                context_state["task_status"] = "in_progress"
            else:
                context_state["task_status"] = "queued"
        else:
            if task_active and result_status == "failed":
                context_state["task_status"] = "failed"
            elif task_active and self._is_task_completion_text(result_text):
                context_state["task_status"] = "completed"
                task_active = False

        context_state["task_active"] = bool(task_active)
        context_state["task_goal"] = task_goal
        context_state["task_step"] = int(task_step)
        context_state["goal_progress"] = self._estimate_goal_progress(
            task_status=str(context_state.get("task_status") or ""),
            task_step=int(context_state.get("task_step") or 0),
            prev_progress=float(context_state.get("goal_progress") or 0.0),
        )
        base_next = self._compute_next_possible_actions(
            goal=str(context_state.get("task_goal") or ""),
            last_action=str(context_state.get("last_action") or ""),
            status=str(context_state.get("task_status") or ""),
        )
        context_state["next_possible_actions"] = self._personalize_next_possible_actions(
            base_actions=base_next,
            user_prefs=user_prefs,
            context_state=context_state,
        )[:2]

        text_result = str((parsed or {}).get("text") or "").strip()
        if text_result:
            context_state["last_result"] = self._first_sentences(text_result, max_sentences=1)[:220]

        self._set_runtime_task_context(user_prefs, context_state)

        self._update_user_habits_from_step(
            user_prefs=user_prefs,
            user_text=user_text,
            actions=actions,
            success=(str(context_state.get("last_result_status") or "").strip().lower() != "failed"),
        )

    def _finalize_response_with_context(
        self,
        *,
        user_text: str,
        planning_text: str,
        parsed: dict,
        user_prefs: dict | None,
    ) -> dict:
        out = parsed if isinstance(parsed, dict) else {"text": str(parsed or ""), "actions": []}
        if not isinstance(out.get("actions"), list):
            out["actions"] = []
        if "text" not in out:
            out["text"] = ""

        planning_confidence = out.pop("_planning_confidence", None)
        proactive_mode = out.pop("_proactive_mode", None)
        response_mode = str(out.pop("_response_mode", "") or "").strip().lower()

        try:
            runtime_ctx = self._get_runtime_task_context(user_prefs)
            out = self._apply_context_to_actions(user_text=user_text, parsed=out, context_state=runtime_ctx)
        except Exception:
            pass

        try:
            runtime_ctx = self._get_runtime_task_context(user_prefs)
            mode = str(proactive_mode or self._resolve_proactive_mode(user_prefs, runtime_ctx)).strip().lower()
            runtime_ctx["proactive_mode"] = mode if mode in {"manual", "assist", "auto"} else "assist"
            self._set_runtime_task_context(user_prefs, runtime_ctx)

            # manual mode: require explicit user confirmation before each executable step.
            if mode == "manual":
                acts = out.get("actions") if isinstance(out.get("actions"), list) else []
                if acts and not self._is_explicit_execution_confirmation(user_text):
                    out["actions"] = []
                    out["text"] = "I am in manual mode. Confirm and I will execute the next step."

            # auto mode: continue only when task is active, confidence high, and next step is obvious/safe.
            if mode == "auto":
                acts = out.get("actions") if isinstance(out.get("actions"), list) else []
                conf = float(planning_confidence if isinstance(planning_confidence, (int, float)) else 0.7)
                threshold = float(getattr(rd, "PROACTIVE_AUTO_MIN_CONFIDENCE", 0.75) or 0.75)
                if (not acts) and bool(runtime_ctx.get("task_active")) and conf >= threshold:
                    nxt = self._derive_auto_next_action(runtime_ctx)
                    if isinstance(nxt, dict) and nxt.get("type"):
                        out["actions"] = [nxt]
                        base = str(out.get("text") or "").strip()
                        out["text"] = (base + "\n\nAuto mode: continuing with the next step.").strip() if base else "Auto mode: continuing with the next step."

            out = self._apply_confidence_execution_control(
                user_text=user_text,
                parsed=out,
                context_state=runtime_ctx,
                planning_confidence=planning_confidence,
            )
        except Exception:
            pass

        try:
            self._update_runtime_task_context(
                user_text=user_text,
                planning_text=planning_text,
                parsed=out,
                user_prefs=user_prefs,
            )
        except Exception:
            pass

        try:
            runtime_ctx = self._get_runtime_task_context(user_prefs)
            suggestion = self._suggest_next_task_step(runtime_ctx, out)
            txt = str(out.get("text") or "").strip()
            low = txt.lower()
            out_actions = out.get("actions") if isinstance(out.get("actions"), list) else []
            intent = self._classify_primary_intent(user_text)
            continuation_like = self._looks_like_continuation_request(user_text)
            should_append = bool(out_actions) or continuation_like or intent in {"action_intent", "clarification_intent"}
            if bool(runtime_ctx.get("task_active")) and suggestion and should_append and ("next step" not in low) and ("want me to continue" not in low):
                out["text"] = (txt + "\n\nNext step: " + suggestion).strip()
        except Exception:
            pass

        try:
            runtime_ctx = self._get_runtime_task_context(user_prefs)
            mode = str(runtime_ctx.get("proactive_mode") or "assist").strip().lower()
            out["text"] = self._apply_assistant_personality_tone(
                str(out.get("text") or ""),
                task_active=bool(runtime_ctx.get("task_active")),
                mode=mode,
            )
        except Exception:
            pass

        try:
            out_actions = out.get("actions") if isinstance(out.get("actions"), list) else []
            out["text"] = self._apply_voice_reply_style(
                text=str(out.get("text") or ""),
                user_text=str(user_text or ""),
                actions=out_actions,
                user_prefs=user_prefs,
                response_mode=response_mode,
            )
        except Exception:
            pass

        return out

    @staticmethod
    def _build_goal_oriented_plan_response(user_text: str, user_prefs: dict | None = None) -> dict | None:
        tl = (user_text or "").strip().lower()
        if not tl:
            return None

        profile = LLMAdapter._classify_intent_profile(user_text)
        if profile.get("intent_type") != "goal_oriented":
            return None

        topic = "your goal"
        m = re.search(r"\b(?:learn|improve|fix|achieve|master|become)\s+(.+)$", str(user_text or ""), flags=re.IGNORECASE)
        if m:
            topic = str(m.group(1) or "").strip(" .,!?") or topic

        query = topic
        if re.search(r"\blearn\b", tl):
            query = f"beginner {topic} tutorial"
        elif re.search(r"\bimprove\b", tl):
            query = f"how to improve {topic}"
        elif re.search(r"\bfix\b", tl):
            query = f"how to fix {topic}"

        prefers_execution = bool(isinstance(user_prefs, dict) and user_prefs.get("prefers_execution") is True)

        text = (
            f"Great goal. Here is a practical plan to make progress on {topic}:\n"
            "1. Open browser\n"
            f"2. Search: {query}\n"
            "3. Follow a curated short step list and track daily progress\n\n"
            "Do you want me to execute step 1 and step 2 now?"
        )

        actions = []
        if prefers_execution:
            actions = [
                {"type": "open_url", "url": f"https://www.google.com/search?q={quote_plus(query)}"},
            ]
            text = (
                f"Great goal. I am starting with a focused resource search for {topic}.\n"
                f"Search query: {query}\n\n"
                "After this opens, I can suggest a curated day-by-day plan."
            )

        return {
            "text": text,
            "actions": actions,
            "source": "deterministic-goal-plan",
            "intent_type": profile.get("intent_type"),
            "intent_depth": profile.get("intent_depth"),
            "response_strategy": profile.get("response_strategy"),
            "proactive_followup_added": True,
            "user_preference_influenced": bool(prefers_execution),
        }

    @staticmethod
    def _is_mixed_action_generation_intent(user_text: str) -> bool:
        tl = (user_text or "").strip().lower()
        if not tl:
            return False

        has_action = bool(
            re.search(
                r"\b(open|launch|start|run|execute|close|switch\s+to|focus|go\s+to|"
                r"take\s+(a\s+)?screenshot|capture\s+screen|set\s+(volume|brightness)|"
                r"turn\s+on|turn\s+off|enable|disable|open\s+settings)\b",
                tl,
            )
        )
        has_generation = bool(
            re.search(
                r"\b(write|draft|compose|generate|create|make)\b.*\b(email|mail|message|reply|content|code|text)\b",
                tl,
            )
            or re.search(r"\b(email|mail)\b.*\b(write|draft|compose|generate|create|send)\b", tl)
        )
        has_joiner = bool(re.search(r"\b(and|then|also|plus)\b", tl))
        return has_action and has_generation and has_joiner

    @staticmethod
    def _build_mixed_intent_generation_first_response(user_text: str) -> dict | None:
        if not LLMAdapter._is_mixed_action_generation_intent(user_text):
            return None

        draft = LLMAdapter._build_reasonable_draft(user_text)
        if not draft:
            return None

        app = LLMAdapter._maybe_map_local_app_name(user_text)
        if not app:
            app = "Notepad"

        app_label = str(app or "Notepad").strip()
        return {
            "text": f"{draft}\n\nDo you want me to open {app_label} and paste this?",
            "actions": [],
            "source": "deterministic-mixed-intent",
        }

    @staticmethod
    def _looks_uncertain(reply_text: str) -> bool:
        tl = (reply_text or "").strip().lower()
        if not tl:
            return False
        markers = (
            "i don't know",
            "i do not know",
            "i'm not sure",
            "im not sure",
            "not sure",
            "can't say",
            "cannot say",
            "uncertain",
            "i might be wrong",
            "i may be wrong",
            "i could be wrong",
        )
        return any(m in tl for m in markers)

    @staticmethod
    def _is_informational_question(user_text: str) -> bool:
        return LLMAdapter._classify_primary_intent(user_text) == "informational_intent"

    @staticmethod
    def _build_direct_generation_response(user_text: str) -> dict | None:
        """Deterministic generation fast-path for content requests.

        This path intentionally avoids web/RAG for generation tasks.
        """
        tl = (user_text or "").strip().lower()
        if not tl:
            return None

        if re.search(r"\b(email|mail)\b", tl) and re.search(r"\b(write|draft|compose|generate|create|make|send)\b", tl):
            draft = LLMAdapter._build_reasonable_draft(user_text)
            if not draft:
                return None
            return {
                "text": f"{draft}\n\nDo you want me to open Outlook and create a draft?",
                "actions": [],
                "source": "deterministic-generation",
            }

        if re.search(r"\b(write|draft|compose|generate|create|make)\b", tl) and re.search(
            r"\b(message|reply|post|content|summary|bio|description|code|snippet|text)\b", tl
        ):
            draft = LLMAdapter._build_reasonable_draft(user_text)
            if not draft:
                return None
            return {
                "text": draft,
                "actions": [],
                "source": "deterministic-generation",
            }

        return None

    @staticmethod
    def _quick_local_chat_reply(user_text: str) -> dict | None:
        """Fast-path replies for common basic prompts.

        This avoids provider round-trips for trivial/local-safe chat prompts.
        """
        tl = (user_text or "").strip().lower()
        if not tl:
            return None

        if re.match(r"^(hi|hello|hey|howdy|greetings|good\s+(morning|afternoon|evening|day))(\s+jarvis)?([\s!?.]*)$", tl):
            return {
                "text": "Hey, I am here. I can help with chat, research, and connected PC actions.",
                "actions": [],
                "source": "deterministic-local-chat",
            }

        if re.match(r"^(jarvis|hey\s+jarvis|ok\s+jarvis|okay\s+jarvis|wake\s+up(?:\s+jarvis)?|jarvis\s+wake\s+up)([\s!?.]*)$", tl):
            return {
                "text": "Hello, I am here. How can I help you?",
                "actions": [],
                "source": "deterministic-local-chat",
            }

        if "what can you do" in tl or "capabilit" in tl:
            return {
                "text": "I can answer questions, do quick research, and run connected PC actions like opening apps, URLs, screenshots, and safe automations when permissions allow.",
                "actions": [],
                "source": "deterministic-local-chat",
            }

        # Deterministic one-line definitions for simple prompts.
        m = re.match(r"^\s*what\s+is\s+(.+?)(?:\s+in\s+one\s+line|\s+in\s+a\s+line)?[\s?.!]*$", tl)
        if m:
            topic = str(m.group(1) or "").strip(" .,!?:;")
            if topic:
                definitions = {
                    "python": "Python is a high-level programming language known for readability and versatility.",
                    "api": "An API is a defined interface that lets software systems communicate with each other.",
                    "ai": "AI is software that performs tasks that normally require human-like intelligence.",
                    "machine learning": "Machine learning is a method where models learn patterns from data to make predictions.",
                    "tcp": "TCP is a reliable transport protocol that delivers ordered data between networked systems.",
                    "http": "HTTP is the protocol used for requesting and transferring web resources.",
                    "dns": "DNS translates domain names into IP addresses so clients can reach internet services.",
                }
                line = definitions.get(topic)
                if not line:
                    topic_label = topic.upper() if len(topic) <= 5 and topic.isalpha() else topic.capitalize()
                    line = f"{topic_label} is the concept or system referred to by that term."
                return {
                    "text": line,
                    "actions": [],
                    "source": "deterministic-local-chat",
                }

        if re.search(r"\b(self\s*[-_]?update)\b", tl) and re.search(r"\b(explain|what\s+is|how|command|usage|mean|means)\b", tl):
            return {
                "text": "The self-update command is an admin-only maintenance flow that applies controlled code changes with audit history and optional rollback. Use it only for trusted update requests, and always review the generated diff before deployment.",
                "actions": [],
                "source": "deterministic-local-chat",
            }

        return None

    def _estimate_complexity(self, text: str, mode: str) -> int:
        """Rough heuristic to scale response budget for harder tasks.

        0 = simple, 1 = medium, 2 = complex
        """
        t = (text or "").strip()
        tl = t.lower()
        wc = len(re.findall(r"\w+", tl))

        complex_markers = (
            "research", "compare", "analyze", "analysis", "summarize", "plan", "architecture",
            "debug", "fix", "refactor", "optimize", "security", "performance", "design",
            "step by step", "end-to-end", "proposal", "strategy",
            "roadmap", "tradeoff", "trade-offs", "pros and cons", "recommend", "evaluation",
            "market", "trend", "outlook", "forecast", "sentiment", "scenario", "thesis",
        )
        needs_internet = bool(
            re.search(
                r"\b(latest|today|current|202\d|news|price|release|docs|documentation|look\s+up|online|sources?|cite|citation|"
                r"crypto|cryptocurrency|bitcoin|ethereum|btc|eth|altcoin|market\s+cap|dominance|funding\s+rate|open\s+interest|on-?chain|token\s+unlock|etf)\b",
                tl,
            )
        )

        score = 0
        if wc >= 14:
            score = 1
        if wc >= 30 or any(m in tl for m in complex_markers):
            score = 2
        if needs_internet and score < 2:
            score = max(score, 1)
        if (mode or "").lower() == "voice" and score > 0:
            # Voice mode should stay concise; don't over-expand.
            score = min(score, 1)
        return score

    def _choose_generation_params(self, text: str, mode: str) -> tuple[int, float]:
        complexity = self._estimate_complexity(text, mode)
        base = max(200, self.default_max_tokens)
        if complexity == 0:
            return min(base, self.max_max_tokens), 0.6
        if complexity == 1:
            return min(max(base, 600), self.max_max_tokens), 0.55
        return min(max(base, 800), self.max_max_tokens), 0.5

    @staticmethod
    def _action_text_from_first_action(actions: list[dict]) -> str:
        if not isinstance(actions, list) or not actions:
            return "Done."
        first = actions[0] if isinstance(actions[0], dict) else {}
        at = str(first.get("type") or "").strip().lower()
        if at == "open_app":
            name = str(first.get("app_name") or "").strip() or "the app"
            return f"Opening {name}."
        if at == "close_app":
            name = str(first.get("app_name") or "").strip() or "the app"
            return f"Closing {name}."
        if at == "switch_app":
            name = str(first.get("app_name") or "").strip() or "the app"
            return f"Switching to {name}."
        if at == "open_url":
            return "Opening that."
        if at == "web_search":
            return "Searching for that."
        if at == "device_action":
            return "Applying that now."
        return "Done."

    def _postprocess_proactive_followup(self, user_text: str, parsed: dict) -> dict:
        """Add concise assistant-style confirmations and optional follow-up suggestions."""
        if not isinstance(parsed, dict):
            return parsed

        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []
        txt = str(parsed.get("text") or "").strip()
        intent = self._classify_primary_intent(user_text)
        followup_added = False

        if actions:
            first = actions[0] if actions and isinstance(actions[0], dict) else {}
            at = str(first.get("type") or "").strip().lower()
            important = {"open_app", "switch_app", "close_app", "execute_command", "device_action"}
            if at in important:
                confirm = self._action_text_from_first_action(actions).strip()
                if confirm.endswith("."):
                    confirm = confirm[:-1] + " now."
                if (not txt) or ("opening" not in txt.lower() and "switching" not in txt.lower() and "closing" not in txt.lower() and "applying" not in txt.lower()):
                    parsed["text"] = confirm
            try:
                profile = self._classify_intent_profile(user_text)
                parsed["intent_type"] = profile.get("intent_type")
                parsed["intent_depth"] = profile.get("intent_depth")
                parsed["response_strategy"] = profile.get("response_strategy")
                parsed["intent"] = parsed.get("intent") or profile.get("intent_type") or "chat"
                parsed["proactive_followup_added"] = bool(parsed.get("proactive_followup_added") or followup_added)
                parsed["user_preference_influenced"] = bool(parsed.get("user_preference_influenced"))
            except Exception:
                pass
            return parsed

        # No actions: add optional proactive suggestion for generation/helpful tasks.
        suggestion = ""
        tl = (user_text or "").strip().lower()
        if intent == "generation_intent":
            if re.search(r"\b(email|mail)\b", tl):
                suggestion = "Do you want me to open Outlook and create this draft?"
            elif re.search(r"\b(fix|debug|error|issue)\b", tl):
                suggestion = "Want me to fix this automatically?"
            else:
                suggestion = "I can run this for you if you want."

        if suggestion and suggestion.lower() not in txt.lower():
            parsed["text"] = (txt + "\n\n" + suggestion).strip() if txt else suggestion
            followup_added = True

        # Goal/informational intent proactive nudges.
        try:
            profile = self._classify_intent_profile(user_text)
            it = str(profile.get("intent_type") or "")
            low = str(parsed.get("text") or "").strip().lower()
            if it == "goal_oriented" and "do you want me" not in low:
                parsed["text"] = (str(parsed.get("text") or "").rstrip() + "\n\nDo you want me to execute the first step now?").strip()
                followup_added = True
            elif it == "informational" and "want a quick example" not in low:
                parsed["text"] = (str(parsed.get("text") or "").rstrip() + "\n\nWant a quick example?").strip()
                followup_added = True
        except Exception:
            pass

        # Attach intent metadata for downstream decision layers.
        try:
            profile = self._classify_intent_profile(user_text)
            parsed["intent_type"] = profile.get("intent_type")
            parsed["intent_depth"] = profile.get("intent_depth")
            parsed["response_strategy"] = profile.get("response_strategy")
            parsed["intent"] = parsed.get("intent") or profile.get("intent_type") or "chat"
            parsed["proactive_followup_added"] = bool(parsed.get("proactive_followup_added") or followup_added)
            parsed["user_preference_influenced"] = bool(parsed.get("user_preference_influenced"))
        except Exception:
            pass

        return parsed

    @staticmethod
    def _detect_compound_intent(user_text: str) -> bool:
        tl = str(user_text or "").strip().lower()
        if not tl:
            return False
        connectors = re.findall(r"\b(?:and\s+then|then|after|and|with|in)\b", tl)
        verbs = re.findall(r"\b(?:open|launch|start|switch|close|search|look\s+up|find|type|send|play|press|set|turn\s+on|turn\s+off|enable|disable|go\s+to|visit)\b", tl)
        return (len(connectors) >= 1) and (len(verbs) >= 2)

    @staticmethod
    def _split_compound_intent_steps(user_text: str) -> list[str]:
        raw = str(user_text or "").strip()
        if not raw:
            return []
        parts = re.split(r"\b(?:and\s+then|then|and|after)\b", raw, flags=re.IGNORECASE)
        out: list[str] = []
        for p in parts:
            s = re.sub(r"\s+", " ", str(p or "")).strip(" ,.!?\t")
            if s:
                out.append(s)
        return out

    @staticmethod
    def _build_compound_response_text(actions: list[dict], fallback_text: str = "") -> str:
        acts = [a for a in (actions or []) if isinstance(a, dict)]
        if not acts:
            return str(fallback_text or "").strip()
        labels: list[str] = []
        for a in acts[:3]:
            t = str(a.get("type") or "").strip().lower()
            if t in {"open_app", "open_url"}:
                labels.append("opening")
            elif t == "switch_app":
                labels.append("switching")
            elif t == "web_search":
                labels.append("searching")
            elif t == "type_text":
                labels.append("typing")
            elif t == "press_key":
                labels.append("confirming")
            else:
                labels.append("working")
        dedup: list[str] = []
        for x in labels:
            if x not in dedup:
                dedup.append(x)
        if not dedup:
            return str(fallback_text or "").strip()
        if len(dedup) == 1:
            return dedup[0].capitalize() + "..."
        if len(dedup) == 2:
            return f"{dedup[0].capitalize()} and {dedup[1]}..."
        return f"{dedup[0].capitalize()}, {dedup[1]}, and {dedup[2]}..."

    def _is_local_reasoner_candidate(self, text: str, mode: str, decision_hint: dict | None) -> bool:
        # Universal LLM-only mode: disable local/deterministic reasoner.
        return False
        if not bool(rd.LOCAL_REASONER_ENABLED):
            return False
        if (mode or "").lower() == "voice":
            return True
        if not bool(rd.LOCAL_REASONER_CHAT_ENABLED):
            return False

        tl = (text or "").strip().lower()
        if not tl:
            return False

        actionable = bool(
            re.search(
                r"\b(open|launch|start|close|quit|exit|switch\s+to|go\s+to|visit|browse|navigate|"
                r"search|look\s+up|research|fetch|open\s+settings|settings|turn\s+on|turn\s+off|enable|disable|"
                r"set|increase|decrease|write|type|draft|compose|format|rewrite|fix)\b",
                tl,
            )
        )
        if actionable:
            return True

        if isinstance(decision_hint, dict):
            conf = float(decision_hint.get("confidence") or 0.0)
            rec = decision_hint.get("recommended_action")
            if conf >= float(rd.LOCAL_REASONER_MIN_CONFIDENCE) and isinstance(rec, dict) and rec.get("type"):
                return True

        return False

    def _build_local_reasoned_response(
        self,
        *,
        text: str,
        context: str,
        mode: str,
        decision_hint: dict | None,
    ) -> dict | None:
        parsed: dict = {"text": "", "actions": []}

        try:
            learned = self._predict_action_from_learned_alias(text)
            if isinstance(learned, dict) and isinstance(learned.get("actions"), list) and learned.get("actions"):
                learned["source"] = "local-reasoner-learned"
                return learned
        except Exception:
            pass

        # Seed with high-confidence decision-maker action when available.
        try:
            if isinstance(decision_hint, dict):
                conf = float(decision_hint.get("confidence") or 0.0)
                rec = decision_hint.get("recommended_action")
                if conf >= float(rd.LOCAL_REASONER_MIN_CONFIDENCE) and isinstance(rec, dict) and rec.get("type"):
                    parsed["actions"] = [rec]
        except Exception:
            pass

        # Build/normalize a deterministic action plan.
        try:
            parsed = self._postprocess_open_url_actions(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_pc_settings_actions(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_write_actions(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_email_clarification_actions(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_ambiguous_type_text_actions(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_missing_value_actions(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_file_action_clarification(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_research_clarification(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_generic_clarification(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_followup_edit_actions(user_text=text, context=context, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_web_lookup_policy(user_text=text, parsed=parsed)
        except Exception:
            pass
        try:
            parsed = self._postprocess_system_safety(user_text=text, parsed=parsed)
        except Exception:
            pass

        actions = parsed.get("actions") or []
        if not (isinstance(actions, list) and actions):
            return None

        try:
            parsed["actions"] = self._dedupe_actions(actions)
        except Exception:
            pass

        if not (parsed.get("text") or "").strip():
            parsed["text"] = self._action_text_from_first_action(parsed.get("actions") or [])
        parsed["source"] = "local-reasoner"
        self._learn_from_actions(text, parsed.get("actions") or [])
        return parsed

    def _apply_preference_overrides(self, max_tokens: int, temperature: float, mode: str, user_prefs: dict | None) -> tuple[int, float]:
        """Adjust generation parameters based on stored user preferences."""
        if not isinstance(user_prefs, dict) or not user_prefs:
            return max_tokens, temperature

        v = (user_prefs.get("verbosity") or "").strip().lower()
        # Voice mode should remain concise regardless.
        if (mode or "").lower() == "voice":
            if v in {"high"}:
                # Allow a bit more, but still keep tight.
                return min(max_tokens, 450), temperature
            return min(max_tokens, 320), temperature

        if v in {"low", "short", "brief", "concise"}:
            max_tokens = min(max_tokens, 400)
            temperature = min(temperature, 0.55)
        elif v in {"high", "detailed", "long"}:
            max_tokens = min(max(self.default_max_tokens, max_tokens, 750), self.max_max_tokens)
            temperature = max(0.45, min(temperature, 0.6))

        return max_tokens, temperature

    @staticmethod
    def _normalize_phrase(s: str) -> str:
        s = (s or "").strip().lower()
        s = re.sub(r"[\"'`]+", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def _maybe_map_local_app_name(phrase: str) -> str:
        """Return a canonical local app name if phrase clearly refers to a local app.

        Conservative mapping: only well-known apps + common speech-to-text variants.
        """
        p = LLMAdapter._normalize_phrase(phrase)
        if not p:
            return ""

        # Drop leading filler.
        p = re.sub(r"^(the|a|an)\s+", "", p).strip()
        # Remove trailing politeness/filler.
        p = re.sub(r"\b(please|pls|now|quickly)\b", "", p).strip()
        p = re.sub(r"\s+", " ", p).strip()

        # Common synonyms/aliases (include STT variants like "note pad").
        alias_map = {
            "notepad": "notepad",
            "note pad": "notepad",
            "wordpad": "wordpad",
            "word pad": "wordpad",
            "textedit": "textedit",
            "text edit": "textedit",
            "calculator": "calculator",
            "calc": "calculator",
            "paint": "paint",
            "ms paint": "paint",
            "mspaint": "paint",
            "cmd": "cmd",
            "command prompt": "cmd",
            "powershell": "powershell",
            "power shell": "powershell",
            "windows powershell": "powershell",
            "file explorer": "explorer",
            "files": "explorer",
            "explorer": "explorer",
            "task manager": "taskmgr",
            "taskmanager": "taskmgr",
            "taskmgr": "taskmgr",
            "vs code": "vscode",
            "vscode": "vscode",
            "visual studio code": "vscode",
            "chrome": "chrome",
            "google chrome": "chrome",
            "firefox": "firefox",
            "edge": "edge",
            "microsoft edge": "edge",
            "word": "word",
            "microsoft word": "word",
            "excel": "excel",
            "microsoft excel": "excel",
            "powerpoint": "powerpoint",
            "microsoft powerpoint": "powerpoint",
            "outlook": "outlook",
            "microsoft outlook": "outlook",
        }

        if p in alias_map:
            return alias_map[p]

        # Handle patterns like "notepad and type ...".
        for k, v in alias_map.items():
            if p.startswith(k + " "):
                return v

        # If it matches a known local app key from AppManager, treat it as local.
        try:
            from src.utils.app_manager import app_manager as _app_mgr

            app_paths = getattr(_app_mgr, "app_paths", {}) or {}
            if p in app_paths:
                return p
            for k in app_paths.keys():
                k2 = str(k or "").strip().lower()
                if k2 and (p == k2 or p.startswith(k2 + " ")):
                    return k2
        except Exception:
            pass
        return ""

    @staticmethod
    def _preparse_deterministic_voice_actions(user_text: str) -> dict | None:
        """Deterministic intent parser for voice mode.

        Goal: when the user says a simple PC command, do the *obvious* thing without
        relying on the LLM (which may be unavailable/rate-limited in production).

        Keep this conservative to avoid breaking complex requests.
        """
        t = (user_text or "").strip()
        if not t:
            return None

        t = LLMAdapter._normalize_voice_control_text(t)
        tl = t.lower().strip()

        # Skill invocation: "run skill X" / "use X skill" / "skill X"

        # Fast-path: greetings and simple small-talk → instant response, no LLM needed.
        _GREETING_PATTERNS = {
            r"^(hi|hello|hey|howdy|greetings|good\s+(morning|afternoon|evening|day)|what'?s\s+up|sup|yo)[\s!?.,]*$": "Hey! How can I help you?",
            r"^(jarvis|hey\s+jarvis|ok\s+jarvis|okay\s+jarvis|wake\s+up(?:\s+jarvis)?|jarvis\s+wake\s+up)[\s!?.,]*$": "Hello, I am here. How can I help you?",
            r"^how\s+are\s+you[\s!?.,]*$": "I'm running great! What can I do for you?",
            r"^(what'?s\s+your\s+name|who\s+are\s+you)[\s!?.,]*$": "I'm Jarvis, your AI assistant.",
            r"^(thanks|thank\s+you|ty|cheers|thx)[\s!?.,]*$": "You're welcome! Anything else?",
            r"^(bye|goodbye|see\s+you|cya|take\s+care)[\s!?.,]*$": "Goodbye! Have a great day.",
            r"^(ok|okay|got\s+it|sure|alright|fine|noted)[\s!?.,]*$": "Got it!",
            r"^(what\s+can\s+you\s+do|what\s+are\s+your\s+capabilities|help|show\s+(me\s+)?commands?)[\s!?.,]*$":
                "I can open apps, search the web, send WhatsApp messages, take screenshots, set volume, do research, learn from you, and much more. Just ask!",
        }
        try:
            for pattern, reply in _GREETING_PATTERNS.items():
                if re.match(pattern, tl, re.IGNORECASE):
                    return {"text": reply, "actions": [], "source": "deterministic-greeting"}
        except Exception:
            pass
        try:
            skill = LLMAdapter._match_skill_command(tl)
            if skill:
                return {
                    "text": f"Running {skill.get('name')}.",
                    "actions": [{
                        "type": "n8n_webhook",
                        "path": skill.get("path"),
                        "method": "POST",
                        "payload": {
                            "skill": skill.get("name"),
                            "query": t,
                        },
                    }],
                    "source": "deterministic-voice",
                }
        except Exception:
            pass

        # Voice-first developer operations:
        # - update/improve Jarvis internals
        # - add new feature/module/component
        # - find code/files in project
        try:
            dev_keywords = r"\b(update|modify|improve|fix|refactor|patch|edit|add|create|make|build)\b"
            target_keywords = r"\b(jarvis|assistant|llm|voice|prompt|orchestrator|brain|executor|flow|logic|code|feature|module|component|project|repo|repository)\b"
            wants_dev_update = bool(re.search(dev_keywords, tl) and re.search(target_keywords, tl))

            if wants_dev_update:
                parsed_cmd = None
                try:
                    from src.utils.self_update import parse_voice_command

                    parsed_cmd = parse_voice_command(t)
                except Exception:
                    parsed_cmd = None

                def _map_target_path(target: str, raw: str) -> str:
                    tx = ((target or "") + " " + (raw or "")).lower()
                    mapping = [
                        ("llm", "src/core/llm_adapter.py"),
                        ("prompt", "src/core/llm_adapter.py"),
                        ("voice", "src/core/llm_adapter.py"),
                        ("orchestrator", "src/core/chat_orchestrator.py"),
                        ("brain", "src/core/jarvis_brain.py"),
                        ("executor", "src/core/executor.py"),
                        ("app.py", "apps/web/app.py"),
                        ("web app", "apps/web/app.py"),
                        ("frontend", "frontend/src/App.js"),
                        ("ui", "frontend/src/App.js"),
                    ]
                    for k, p in mapping:
                        if k in tx:
                            return p

                    # If user already named a file, pass it through.
                    if re.search(r"\.[a-z0-9]{1,6}$", (target or "").strip().lower()):
                        return (target or "").strip()
                    return "src/core/llm_adapter.py"

                if isinstance(parsed_cmd, dict) and parsed_cmd.get("action") in {"update", "edit"}:
                    target = str(parsed_cmd.get("target") or "").strip()
                    description = str(parsed_cmd.get("description") or t).strip() or t
                    file_path = _map_target_path(target, t)
                    return {
                        "text": f"Preparing update in {file_path}.",
                        "actions": [{
                            "type": "self_update",
                            "description": description,
                            "file_path": file_path,
                        }],
                        "source": "deterministic-voice-self-update",
                    }

                if isinstance(parsed_cmd, dict) and parsed_cmd.get("action") == "add":
                    feature_type = str(parsed_cmd.get("feature_type") or "module").strip().lower()
                    if feature_type not in {"module", "component", "feature", "file"}:
                        feature_type = "module"
                    description = str(parsed_cmd.get("description") or t).strip() or t
                    return {
                        "text": f"Preparing to add a new {feature_type}.",
                        "actions": [{
                            "type": "self_add",
                            "description": description,
                            "feature_type": feature_type,
                        }],
                        "source": "deterministic-voice-self-update",
                    }

                # Fallback for broad update phrasing without precise parse.
                if re.search(r"\b(add|create|build|make)\b", tl):
                    return {
                        "text": "Preparing to add the requested feature.",
                        "actions": [{
                            "type": "self_add",
                            "description": t,
                            "feature_type": "module",
                        }],
                        "source": "deterministic-voice-self-update",
                    }

                return {
                    "text": "Preparing to update assistant logic.",
                    "actions": [{
                        "type": "self_update",
                        "description": t,
                        "file_path": _map_target_path("", t),
                    }],
                    "source": "deterministic-voice-self-update",
                }

            # Project file/code discovery in voice mode.
            wants_project_find = bool(
                re.search(r"\b(find|search|locate|look\s+for|where\s+is)\b", tl)
                and re.search(r"\b(file|code|project|repo|repository|folder|path|class|function)\b", tl)
            )
            if wants_project_find:
                q = re.sub(r"(?i)\b(find|search|locate|look\s+for|where\s+is|file|code|project|repo|repository|folder|path|class|function|in|the|for|please|jarvis)\b", " ", t)
                q = re.sub(r"\s+", " ", q).strip(" .")
                if len(re.findall(r"[a-z0-9_\-\.]+", q.lower())) >= 1:
                    return {
                        "text": "Searching the project for matches.",
                        "actions": [{
                            "type": "find_files",
                            "query": q,
                            "path": "src",
                            "in_content": True,
                            "max_results": 20,
                        }],
                        "source": "deterministic-voice-find",
                    }
        except Exception:
            pass

        # Intent-based skill routing (research/scrape) when skills exist
        try:
            skills = LLMAdapter._get_skills_catalog() or []
            skill_names = {str(s.get("name") or "").strip().lower(): s for s in skills if isinstance(s, dict)}

            if re.search(r"\b(research|market research|analyze market|analysis|report)\b", tl):
                target = skill_names.get("market_research") or skill_names.get("research")
                if target:
                    return {
                        "text": "Starting research.",
                        "actions": [{
                            "type": "n8n_webhook",
                            "path": target.get("path"),
                            "method": "POST",
                            "payload": {
                                "skill": target.get("name"),
                                "query": t,
                            },
                        }],
                        "source": "deterministic-voice",
                    }

            if re.search(r"\b(scrape|extract|crawl)\b", tl):
                target = skill_names.get("web_scrape") or skill_names.get("scrape")
                if target:
                    url_match = re.search(r"https?://\S+", t)
                    return {
                        "text": "Starting web scrape.",
                        "actions": [{
                            "type": "n8n_webhook",
                            "path": target.get("path"),
                            "method": "POST",
                            "payload": {
                                "skill": target.get("name"),
                                "url": url_match.group(0) if url_match else None,
                                "query": t,
                            },
                        }],
                        "source": "deterministic-voice",
                    }
        except Exception:
            pass

        # Deterministic: screenshot / screen capture
        # "Take a screenshot", "capture my screen", "screenshot"
        try:
            if re.search(r"\b(take|capture|grab|snap)\b", tl) and re.search(r"\b(screenshot|screen\s*shot|screen)\b", tl) \
                    or tl.strip() in {"screenshot", "take screenshot", "grab screenshot"}:
                return {
                    "text": "Taking a screenshot.",
                    "actions": [{"type": "capture_screen"}],
                    "source": "deterministic-voice-screenshot",
                }
        except Exception:
            pass

        # High-confidence WhatsApp messaging intent: deterministic parsing
        # Supports: phone number → "Open WhatsApp send message to +918460217965 message is 'Hello'"
        #           contact name → "Send WhatsApp to John saying Meeting at 5pm"
        # Architecture: Intent → Mode Selection (app vs web) → Plan Actions → Execute
        try:
            wants_whatsapp = bool(re.search(r"\b(watsapp|whatsapp)\b", tl))
            wants_send = bool(re.search(r"\b(send|message|msg)\b", tl))
            if wants_whatsapp and wants_send:
                # Extract phone number from the text
                phone_match = re.search(r"(?<!\w)(\+?\d[\d\s\-]{7,18}\d)(?!\w)", t)
                phone_raw = (phone_match.group(1).strip() if phone_match else "")
                phone_digits = re.sub(r"[^0-9+]", "", phone_raw)
                if phone_digits.startswith("+"):
                    phone_digits = phone_digits[1:]

                # Extract contact name when no phone number: "to John", "to My Friend"
                contact_name = ""
                if not phone_digits:
                    name_match = re.search(
                        r"\bto\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b",
                        t,
                    )
                    contact_name = (name_match.group(1).strip() if name_match else "")

                # Extract message: "message/msg is ...", "saying ..."
                msg_match = re.search(
                    r"(?:l?message|msg)\s*(?:is|:)?\s*[\"'\u201c\u201d]?\s*([^\"'\u201c\u201d]+?)(?:[\"'\u201c\u201d]\s*)?$",
                    t,
                    flags=re.IGNORECASE,
                ) or re.search(
                    r"\bsaying\s+[\"'\u201c\u201d]?(.+?)[\"'\u201c\u201d]?\s*$",
                    t,
                    flags=re.IGNORECASE,
                )
                message_text = (msg_match.group(1).strip() if msg_match else "")
                message_text = re.sub(r"\s+", " ", message_text).strip()

                # Choose recipient label for TTS
                recipient = phone_raw or contact_name or "the contact"
                # Choose search term to type in WhatsApp: phone takes priority, else name
                search_term = phone_raw if phone_digits else contact_name

                if (phone_digits or contact_name) and message_text:
                    # Desktop app first; agent falls back automatically if not installed.
                    out_actions = [
                        {"type": "open_app", "app_name": "WhatsApp"},
                        {"type": "type_text", "text": search_term, "before_ms": 1500},
                        {"type": "press_key", "key": "enter", "presses": 1, "before_ms": 500},
                        {"type": "type_text", "text": message_text, "before_ms": 800},
                        {"type": "press_key", "key": "enter", "presses": 1, "before_ms": 200},
                    ]
                    return {
                        "text": f"Opening WhatsApp and sending message to {recipient}.",
                        "actions": out_actions,
                        "source": "deterministic-voice-whatsapp",
                    }
                elif phone_digits:
                    # Phone only, no message: open WhatsApp web for the contact
                    wa_url = f"https://web.whatsapp.com/send?phone={phone_digits}"
                    return {
                        "text": f"Opening WhatsApp for {phone_raw}.",
                        "actions": [{"type": "open_url", "url": wa_url}],
                        "source": "deterministic-voice-whatsapp-web-fallback",
                    }
                elif contact_name:
                    # Name only, no message: open WhatsApp and search contact
                    out_actions = [
                        {"type": "open_app", "app_name": "WhatsApp"},
                        {"type": "type_text", "text": contact_name, "before_ms": 1500},
                        {"type": "press_key", "key": "enter", "presses": 1, "before_ms": 500},
                    ]
                    return {
                        "text": f"Opening WhatsApp and searching for {contact_name}.",
                        "actions": out_actions,
                        "source": "deterministic-voice-whatsapp",
                    }
        except Exception:
            pass

        # Avoid hijacking internet/research tasks.
        if re.search(r"\b(latest|today|current|news|download|install|update|documentation|docs|how\s+to)\b", tl):
            return None
        if re.search(r"\b(look\s+up|search\s+online|from\s+the\s+internet|on\s+the\s+internet|with\s+sources?|citations?|links?)\b", tl):
            return None

        # If the user explicitly provided a URL/domain, let normal URL tooling handle it.
        if re.search(r"\bhttps?://|\bwww\.|\b[a-z0-9\-]+\.(com|org|net|io|dev)\b", tl):
            # Unless they also clearly mention a local app (rare but possible).
            if not LLMAdapter._maybe_map_local_app_name(tl):
                return None

        words = re.findall(r"[a-z0-9']+", tl)
        # Allow more words because voice often includes filler.
        if len(words) > 24:
            return None

        parsed: dict = {"text": "", "actions": []}

        # Deterministic settings route: handle "set/turn/increase/decrease ..."
        # even when no open/switch/close verb is present.
        if re.search(r"\b(set|turn\s+on|turn\s+off|enable|disable|increase|decrease|raise|lower)\b", tl):
            try:
                settings_parsed = LLMAdapter._postprocess_pc_settings_actions(user_text=t, parsed={"text": "", "actions": []})
                settings_actions = settings_parsed.get("actions") if isinstance(settings_parsed.get("actions"), list) else []
                if settings_actions:
                    settings_parsed["source"] = "deterministic-voice"
                    if not str(settings_parsed.get("text") or "").strip():
                        settings_parsed["text"] = LLMAdapter._action_text_from_first_action(settings_actions)
                    return settings_parsed
            except Exception:
                pass

        # Accept natural phrasing, not just "<verb> <target>".
        # Examples:
        # - jarvis please open notepad
        # - can you open notepad and type hello
        # - i want to open calculator
        # - please close the notepad
        # - switch to chrome
        open_verb = re.search(r"\b(open|start|launch)\b", tl)
        close_verb = re.search(r"\b(close|quit|exit)\b", tl)
        switch_verb = re.search(r"\b(switch\s+to|switch|focus|go\s+to)\b", tl)

        if close_verb:
            target = LLMAdapter._maybe_map_local_app_name(tl)
            if not target:
                # Heuristic: take words after the close verb.
                m = re.search(r"\b(?:close|quit|exit)\b\s+(.*)$", tl)
                target = (m.group(1).strip() if m else "")
                target = LLMAdapter._maybe_map_local_app_name(target) or target
            if target:
                parsed["actions"] = [{"type": "close_app", "app_name": target}]
                parsed["text"] = f"Closing {target}."
                parsed["source"] = "deterministic-voice"
                return parsed
            return None

        if switch_verb:
            target = LLMAdapter._maybe_map_local_app_name(tl)
            if not target:
                m = re.search(r"\b(?:switch\s+to|switch|focus|go\s+to)\b\s+(.*)$", tl)
                target = (m.group(1).strip() if m else "")
                target = LLMAdapter._maybe_map_local_app_name(target) or target
            if target:
                parsed["actions"] = [{"type": "switch_app", "app_name": target}]
                parsed["text"] = f"Switching to {target}."
                parsed["source"] = "deterministic-voice"
                return parsed
            return None

        if open_verb:
            # If the user says "open X and type Y", keep the whole string so existing
            # postprocessors can add type_text. We don't require a strict format; we
            # just attempt the deterministic postprocessing pipeline.
            parsed = {"text": "", "actions": []}
            try:
                parsed = LLMAdapter._postprocess_open_url_actions(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_pc_settings_actions(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_write_actions(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_email_clarification_actions(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_ambiguous_type_text_actions(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_missing_value_actions(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_file_action_clarification(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_research_clarification(user_text=t, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_system_safety(user_text=t, parsed=parsed)
            except Exception:
                pass

            actions = parsed.get("actions") or []
            if isinstance(actions, list) and actions:
                # Provide a short deterministic user-facing line.
                first = actions[0] if isinstance(actions[0], dict) else {}
                at = (first.get("type") or "").strip()
                if not str(parsed.get("text") or "").strip():
                    if at == "open_app":
                        name = str(first.get("app_name") or "").strip() or "the app"
                        parsed["text"] = f"Opening {name}."
                    elif at == "open_url":
                        parsed["text"] = "Opening it."
                    elif at == "web_search":
                        parsed["text"] = "Looking it up online."
                    else:
                        parsed["text"] = "Done."
                parsed["source"] = "deterministic-voice"
                return parsed

        return None

    @staticmethod
    def _match_skill_command(tl: str) -> dict | None:
        try:
            skills = LLMAdapter._get_skills_catalog()
            if not skills:
                root = Path(__file__).resolve().parents[2]
                skills_path = root / "data" / "skills.json"
                if not skills_path.exists():
                    return None
                data = json.loads(skills_path.read_text(encoding="utf-8"))
                skills = [s for s in (data or []) if isinstance(s, dict) and s.get("enabled", True)]
            if not skills:
                return None

            # Normalize skill names for matching
            names = {str(s.get("name") or "").strip().lower(): s for s in skills}
            if not names:
                return None

            patterns = [
                r"\b(run|use|execute|start)\s+skill\s+([a-z0-9_\- ]{2,50})$",
                r"\b(skill)\s+([a-z0-9_\- ]{2,50})$",
                r"\b(run|use|execute|start)\s+([a-z0-9_\- ]{2,50})\s+skill\b",
            ]
            target = None
            for p in patterns:
                m = re.search(p, tl)
                if m:
                    target = (m.group(2) or "").strip().lower()
                    break
            if not target:
                return None

            if target in names:
                return names[target]

            # Fuzzy match by inclusion
            for k, v in names.items():
                if k and k in target:
                    return v

        except Exception:
            return None
        return None

    @staticmethod
    def _is_high_level_analysis_task(user_text: str) -> bool:
        """Return True if the user is asking for high-level informational synthesis.

        Used to optionally bypass the LLM when running in offline mode.
        """
        t = (user_text or "").strip().lower()
        if not t:
            return False
        return bool(
            re.search(
                r"\b(analyze|analysis|research|compare|strategy|roadmap|tradeoff|trade\-offs|pros\s+and\s+cons|"
                r"evaluation|outlook|forecast|sentiment|scenario|thesis|market|markets|trend)\b",
                t,
            )
        )

    @staticmethod
    def _spoken_number_to_int(phrase: str) -> int | None:
        p = str(phrase or "").strip().lower()
        if not p:
            return None
        units = {
            "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
            "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
            "seventeen": 17, "eighteen": 18, "nineteen": 19,
        }
        tens = {
            "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
        }
        if p in units:
            return units[p]
        if p in tens:
            return tens[p]
        m = re.fullmatch(
            r"(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[ -](one|two|three|four|five|six|seven|eight|nine)",
            p,
        )
        if m:
            return int(tens[m.group(1)] + units[m.group(2)])
        if p == "hundred" or p == "one hundred":
            return 100
        return None

    @classmethod
    def _normalize_voice_control_text(cls, text: str) -> str:
        t = str(text or "").strip().lower()
        if not t:
            return ""

        # Normalize common ASR spelling variants for control intents.
        replacements = [
            (r"\bblue\s*tooth\b", "bluetooth"),
            (r"\bblu\s*tooth\b", "bluetooth"),
            (r"\bblutooth\b", "bluetooth"),
            (r"\bbightness\b", "brightness"),
            (r"\bbrighness\b", "brightness"),
            (r"\bbright\s*ness\b", "brightness"),
            (r"\bbrite\s*ness\b", "brightness"),
            (r"\bwi\s*-?\s*fi\b", "wifi"),
            (r"\bturn\s+of\b", "turn off"),
        ]
        for pat, rep in replacements:
            t = re.sub(pat, rep, t, flags=re.IGNORECASE)

        # Convert spoken percentages, e.g. "twenty percent" -> "20%".
        number_word_pattern = (
            r"(zero|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
            r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
            r"eighty|ninety|hundred|one\s+hundred)(?:[ -](one|two|three|"
            r"four|five|six|seven|eight|nine))?"
        )

        def _to_percent(m: re.Match) -> str:
            phrase = str(m.group(1) or "")
            suffix = str(m.group(2) or "")
            full = (phrase + (f" {suffix}" if suffix else "")).strip()
            n = cls._spoken_number_to_int(full)
            if n is None:
                return str(m.group(0) or "")
            n = max(0, min(100, int(n)))
            return f"{n}%"

        t = re.sub(rf"\b{number_word_pattern}\s*percent\b", _to_percent, t, flags=re.IGNORECASE)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    @staticmethod
    def _postprocess_pc_settings_actions(user_text: str, parsed: dict) -> dict:
        """Best-effort helper: open PC Settings pages safely.

        We do NOT directly change OS/security-critical configuration.
        For low-risk settings (e.g., brightness / power plan), we may emit a
        dedicated device action so the PC agent can apply the change.
        Otherwise we only open the relevant Settings page (primarily Windows via
        ms-settings:) and keep the rest as user-guided steps.
        """
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        def _wrap_device_action(name: str, args: dict | None = None) -> dict:
            """Universal device action envelope.

            This keeps the LLM-facing action surface small. The PC agent unwraps
            this and dispatches to the appropriate implementation.
            """
            return {
                "type": "device_action",
                "name": str(name or "").strip(),
                "args": (args if isinstance(args, dict) else {}),
            }

        # If the model already emitted a Settings opener, don't add duplicates.
        for a in actions:
            if not isinstance(a, dict):
                continue
            if a.get("type") == "execute_command" and "ms-settings:" in str(a.get("command") or ""):
                parsed["actions"] = actions
                return parsed
            if a.get("type") == "open_app" and "setting" in str(a.get("app_name") or "").lower():
                parsed["actions"] = actions
                return parsed

        t = LLMAdapter._normalize_voice_control_text(user_text)
        if not t:
            parsed["actions"] = actions
            return parsed

        # Keep detection broad; mapping below decides whether we can safely auto-open a page.
        wants_settings = bool(
            re.search(
                r"\b(settings|configuration|configure|setup|set up|wifi|wi-?fi|bluetooth|sound|audio|volume|mute|unmute|display|screen|notification|notifications|do\s+not\s+disturb|dnd|focus\s+assist|quiet\s+hours|brightness|night\s*light|time|date|language|keyboard|mouse|touchpad|printer|storage|battery|power|privacy|camera|microphone|default apps|apps|energy\s*saver|battery\s*saver)\b",
                t,
            )
        )
        if not wants_settings:
            parsed["actions"] = actions
            return parsed

        # Energy Saver: prefer an explicit power-plan action over opening Settings.
        if re.search(r"\b(energy\s*saver|battery\s*saver)\b", t):
            wants_on = bool(re.search(r"\b(turn\s+on|enable|activate)\b", t))
            wants_off = bool(re.search(r"\b(turn\s+off|disable|deactivate)\b", t))
            if wants_on or wants_off:
                plan = "power saver" if wants_on else "balanced"
                filtered = []
                for a in actions:
                    if not isinstance(a, dict):
                        continue
                    if a.get("type") == "execute_command" and "ms-settings:" in str(a.get("command") or "").lower():
                        continue
                    filtered.append(a)
                filtered.append(_wrap_device_action("set_power_plan", {"plan": plan}))
                parsed["actions"] = filtered
                base_text = (parsed.get("text") or "").strip()
                msg = "Turning on Energy Saver (Power saver plan)." if wants_on else "Turning off Energy Saver (Balanced plan)."
                parsed["text"] = base_text or msg
                return parsed

        # Only auto-open common *low-risk* settings pages.
        # We intentionally avoid updates, recovery, security, firewall, registry, disk, etc.
        # NOTE: ms-settings URIs vary across Windows versions; if a URI is unsupported,
        # Windows will typically fall back to the Settings home.
        settings_catalog = [
            (r"\b(display|resolution|scale|scaling|brightness|screen|night\s*light|hdr|orientation|multiple\s+displays)\b", "ms-settings:display"),
            (r"\b(sound|audio|volume|mute|unmute|speaker|microphone|mic|input|output|headphones)\b", "ms-settings:sound"),
            (r"\b(notification|notifications|do\s+not\s+disturb|focus|focus\s+assist)\b", "ms-settings:notifications"),
            (r"\b(bluetooth|bt|pair\s+device|pairing)\b", "ms-settings:bluetooth"),
            (r"\b(wi-?fi|wifi|wireless)\b", "ms-settings:network-wifi"),
            (r"\b(network|ethernet|ip\s+address|dns|proxy)\b", "ms-settings:network"),
            (r"\b(time\s*zone|date\s+and\s+time|date|time)\b", "ms-settings:dateandtime"),
            (r"\b(language|region|keyboard\s+layout|input\s+language)\b", "ms-settings:regionlanguage"),
            (r"\b(storage|storage\s+sense|disk\s+space|free\s+space)\b", "ms-settings:storagesense"),
            (r"\b(battery|power|sleep|lid\s+close)\b", "ms-settings:powersleep"),
            (r"\b(default\s+apps?|file\s+associations?)\b", "ms-settings:defaultapps"),
            (r"\b(apps?\s+and\s+features|uninstall|installed\s+apps?)\b", "ms-settings:appsfeatures"),
            (r"\b(privacy\b.*\bcamera\b|camera\s+permission)\b", "ms-settings:privacy-webcam"),
            (r"\b(privacy\b.*\bmicrophone\b|microphone\s+permission)\b", "ms-settings:privacy-microphone"),
            (r"\b(accessibility|ease\s+of\s+access)\b", "ms-settings:easeofaccess"),
            (r"\b(mouse|touchpad|trackpad)\b", "ms-settings:mousetouchpad"),
            (r"\b(printer|printers|printing|scanner)\b", "ms-settings:printers"),
        ]

        ms_uri = None
        for pattern, uri in settings_catalog:
            if re.search(pattern, t):
                ms_uri = uri
                break

        if ms_uri:
            # Special-case: brightness and energy saver can be applied automatically
            # via explicit device actions.
            if ms_uri == "ms-settings:display" and re.search(r"\bbrightness\b", t):
                # Try to infer an absolute target (0-100) or a small delta.
                target = None
                delta = None
                m = re.search(r"\bbrightness\b[^\d]{0,15}(\d{1,3})\s*%?\b", t)
                if m:
                    try:
                        target = int(m.group(1))
                    except Exception:
                        target = None
                if target is None:
                    m2 = re.search(r"\b(\d{1,3})\s*%?\b[^\n]{0,20}\bbrightness\b", t)
                    if m2:
                        try:
                            target = int(m2.group(1))
                        except Exception:
                            target = None
                if target is not None:
                    target = max(0, min(100, target))
                else:
                    if re.search(r"\b(increase|raise|turn\s+up|brighter|up)\b", t):
                        delta = 10
                    elif re.search(r"\b(decrease|lower|turn\s+down|dimmer|down)\b", t):
                        delta = -10

                if target is not None or delta is not None:
                    # Remove any prior settings openers / brightness shell commands.
                    filtered = []
                    for a in actions:
                        if not isinstance(a, dict):
                            continue
                        if a.get("type") != "execute_command":
                            filtered.append(a)
                            continue
                        cmd = str(a.get("command") or "").strip().lower()
                        if "ms-settings:" in cmd:
                            continue
                        if "brightness" in cmd or "nircmd" in cmd:
                            continue
                        filtered.append(a)
                    actions = filtered

                    if target is not None:
                        actions.append(_wrap_device_action("set_brightness", {"value": target}))
                        parsed["text"] = (parsed.get("text") or "" ).strip() or f"Setting brightness to {target}%."
                    else:
                        actions.append(_wrap_device_action("set_brightness", {"delta": int(delta)}))
                        parsed["text"] = (parsed.get("text") or "" ).strip() or ("Increasing brightness." if delta > 0 else "Decreasing brightness.")

                    parsed["actions"] = actions
                    return parsed

            if ms_uri == "ms-settings:sound" and re.search(r"\b(volume|mute|unmute|sound)\b", t):
                # Volume/mute are low-risk quality-of-life controls; emit explicit device actions.
                wants_unmute = bool(re.search(r"\b(unmute|sound\s+on|turn\s+on\s+sound)\b", t))
                wants_mute = bool(re.search(r"\b(mute|silence|turn\s+off\s+sound)\b", t)) and not wants_unmute

                target = None
                delta = None
                m = re.search(r"\bvolume\b[^\d]{0,15}(\d{1,3})\s*%?\b", t)
                if m:
                    try:
                        target = int(m.group(1))
                    except Exception:
                        target = None
                if target is None:
                    m2 = re.search(r"\b(\d{1,3})\s*%?\b[^\n]{0,20}\bvolume\b", t)
                    if m2:
                        try:
                            target = int(m2.group(1))
                        except Exception:
                            target = None

                if target is not None:
                    target = max(0, min(100, target))
                else:
                    if re.search(r"\b(increase|raise|turn\s+up|louder|up)\b", t):
                        delta = 10
                    elif re.search(r"\b(decrease|lower|turn\s+down|quieter|down)\b", t):
                        delta = -10

                if wants_mute or wants_unmute or target is not None or delta is not None:
                    filtered = []
                    for a in actions:
                        if not isinstance(a, dict):
                            continue
                        if a.get("type") != "execute_command":
                            filtered.append(a)
                            continue
                        cmd = str(a.get("command") or "").strip().lower()
                        if "ms-settings:" in cmd:
                            continue
                        if "volume" in cmd or "nircmd" in cmd:
                            continue
                        filtered.append(a)
                    actions = filtered

                    if wants_mute or wants_unmute:
                        actions.append(_wrap_device_action("set_mute", {"muted": bool(wants_mute)}))
                        base_text = (parsed.get("text") or "").strip()
                        if not base_text:
                            parsed["text"] = "Muting system volume." if wants_mute else "Unmuting system volume."

                    if target is not None:
                        actions.append(_wrap_device_action("set_volume", {"value": target}))
                        parsed["text"] = (parsed.get("text") or "").strip() or f"Setting volume to {target}%."
                    elif delta is not None:
                        actions.append(_wrap_device_action("set_volume", {"delta": int(delta)}))
                        parsed["text"] = (parsed.get("text") or "").strip() or ("Increasing volume." if delta > 0 else "Decreasing volume.")

                    parsed["actions"] = actions
                    return parsed

            if re.search(r"\b(energy\s*saver|battery\s*saver)\b", t):
                wants_on = bool(re.search(r"\b(turn\s+on|enable|activate)\b", t))
                wants_off = bool(re.search(r"\b(turn\s+off|disable|deactivate)\b", t))
                if wants_on or wants_off:
                    plan = "power saver" if wants_on else "balanced"
                    filtered = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "execute_command" and "ms-settings:" in str(a.get("command") or "").lower())]
                    filtered.append(_wrap_device_action("set_power_plan", {"plan": plan}))
                    parsed["actions"] = filtered
                    base_text = (parsed.get("text") or "").strip()
                    msg = "Turning on Energy Saver (Power saver plan)." if wants_on else "Turning off Energy Saver (Balanced plan)."
                    parsed["text"] = base_text or msg
                    return parsed

            # Wi-Fi toggle (best-effort on Windows; agent may fall back to opening Settings).
            if ms_uri == "ms-settings:network-wifi" and re.search(r"\b(wi-?fi|wifi|wireless)\b", t):
                wants_on = bool(re.search(r"\b(turn\s+on|enable|connect)\b", t))
                wants_off = bool(re.search(r"\b(turn\s+off|disable|disconnect)\b", t))
                if wants_on or wants_off:
                    filtered = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "execute_command" and "ms-settings:" in str(a.get("command") or "").lower())]
                    filtered.append(_wrap_device_action("set_wifi", {"enabled": bool(wants_on)}))
                    parsed["actions"] = filtered
                    base_text = (parsed.get("text") or "").strip()
                    parsed["text"] = base_text or ("Turning Wi-Fi on." if wants_on else "Turning Wi-Fi off.")
                    return parsed

            # Bluetooth toggle (best-effort on Windows; agent may fall back to opening Settings).
            if ms_uri == "ms-settings:bluetooth" and re.search(r"\b(bluetooth|bt)\b", t):
                wants_on = bool(re.search(r"\b(turn\s+on|enable)\b", t))
                wants_off = bool(re.search(r"\b(turn\s+off|disable)\b", t))
                if wants_on or wants_off:
                    filtered = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "execute_command" and "ms-settings:" in str(a.get("command") or "").lower())]
                    filtered.append(_wrap_device_action("set_bluetooth", {"enabled": bool(wants_on)}))
                    parsed["actions"] = filtered
                    base_text = (parsed.get("text") or "").strip()
                    parsed["text"] = base_text or ("Turning Bluetooth on." if wants_on else "Turning Bluetooth off.")
                    return parsed

            # Night Light: Windows doesn't provide a stable official CLI toggle.
            # We open Display settings (safe) and can optionally open quick settings for a faster toggle.
            if ms_uri == "ms-settings:display" and re.search(r"\bnight\s*light\b", t):
                wants_on = bool(re.search(r"\b(turn\s+on|enable)\b", t))
                wants_off = bool(re.search(r"\b(turn\s+off|disable)\b", t))
                if wants_on or wants_off:
                    filtered = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "execute_command" and "ms-settings:" in str(a.get("command") or "").lower())]
                    filtered.append(_wrap_device_action("open_settings", {"uri": "ms-settings:display"}))
                    # Also try opening quick settings (Win+A) to make toggling faster.
                    filtered.append(_wrap_device_action("open_quick_settings", {}))
                    parsed["actions"] = filtered
                    base_text = (parsed.get("text") or "").strip()
                    parsed["text"] = base_text or "Opening settings to toggle Night light."
                    return parsed

            # Do Not Disturb / Focus Assist: no stable official CLI toggle across Windows versions.
            # Open Notifications settings and (optionally) quick settings.
            if ms_uri == "ms-settings:notifications" and re.search(r"\b(do\s+not\s+disturb|dnd|focus\s+assist|quiet\s+hours)\b", t):
                wants_on = bool(re.search(r"\b(turn\s+on|enable)\b", t))
                wants_off = bool(re.search(r"\b(turn\s+off|disable)\b", t))
                if wants_on or wants_off:
                    filtered = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "execute_command" and "ms-settings:" in str(a.get("command") or "").lower())]
                    filtered.append(_wrap_device_action("open_settings", {"uri": "ms-settings:notifications"}))
                    filtered.append(_wrap_device_action("open_quick_settings", {}))
                    parsed["actions"] = filtered
                    base_text = (parsed.get("text") or "").strip()
                    parsed["text"] = base_text or "Opening settings to toggle Do Not Disturb / Focus Assist."
                    return parsed

            # Default behavior: open the relevant Settings page.
            # Using cmd's start with an empty title is the most reliable form.
            actions.append({"type": "execute_command", "command": f'start "" "{ms_uri}"', "wait": False})

            parsed["actions"] = actions

            base_text = (parsed.get("text") or "").strip()

            # Keep the assistant honest: opening Settings isn't the same as changing the value.
            wants_toggle = bool(re.search(r"\b(turn\s+on|turn\s+off|enable|disable)\b", t))
            if ms_uri == "ms-settings:display" and re.search(r"\bbrightness\b", t):
                msg = "Opening Display settings."
            elif wants_toggle:
                msg = "Opening Settings for that change. Tell me what you see and I’ll guide the safest steps."
            else:
                msg = "Opening the relevant Settings page. Tell me what you want to change and I’ll guide the safest steps."

            if not base_text:
                parsed["text"] = msg
            else:
                # If the model claimed it already changed something, append a correction.
                if re.search(r"\b(set|adjust|changed)\b", base_text.lower()) and msg.lower() not in base_text.lower():
                    parsed["text"] = (base_text + "\n\n" + msg).strip()
                elif msg not in base_text:
                    parsed["text"] = (base_text + "\n\n" + msg).strip()
            return parsed

        # Generic settings request: open Settings app (best effort) without changing anything.
        if re.search(r"\bsettings\b", t):
            actions.append({"type": "execute_command", "command": 'start "" "ms-settings:"', "wait": False})
            parsed["actions"] = actions
        else:
            parsed["actions"] = actions
        return parsed

    @staticmethod
    def _is_project_relative_path(path: str) -> bool:
        """Return True if `path` looks like a safe project-relative path.

        We intentionally disallow absolute paths and parent traversal so the model
        cannot target OS/system locations.
        """
        p = (path or "").strip()
        if not p:
            return False
        # Absolute paths (Windows drive, UNC, or POSIX root)
        if re.match(r"^[A-Za-z]:\\", p) or p.startswith("\\\\") or p.startswith("/") or p.startswith("\\"):
            return False
        # Home shortcuts
        if p.startswith("~"):
            return False
        # Parent traversal
        parts = re.split(r"[\\/]+", p)
        if any(part == ".." for part in parts):
            return False
        return True

    @staticmethod
    def _is_dangerous_command(command: str) -> bool:
        """Block destructive OS/system commands.

        This is a safety backstop to prevent accidental OS damage (format, disk ops,
        deleting system folders, boot/registry edits, etc.).
        """
        c = (command or "").strip()
        if not c:
            return False
        cl = c.lower()

        # High-risk utilities / operations
        high_risk_patterns = [
            r"\bformat\b",  # format c:
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

        # Classic "rm -rf /"
        if re.search(r"\brm\b\s+.*\s-\s*rf\s+/(?:\s|$)", cl):
            return True
        if "--no-preserve-root" in cl and "rm" in cl and "/" in cl:
            return True

        # Deleting system locations
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

    @staticmethod
    def _postprocess_system_safety(user_text: str, parsed: dict) -> dict:
        """Remove actions that could modify OS/system files or perform destructive commands."""
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        blocked = []
        kept = []

        for a in actions:
            if not isinstance(a, dict):
                continue
            t = (a.get("type") or "").strip()

            # Avoid accidental screenshots unless the user explicitly requested it.
            # This covers universal device actions like: {"type":"device_action","name":"save_screenshot"}.
            if t == "device_action":
                nm = str((a.get("name") or a.get("action") or "")).strip().lower()
                if nm in ("save_screenshot", "screenshot"):
                    ut = (user_text or "").strip().lower()
                    explicit = bool(re.search(r"\b(screenshot|screen\s*shot|capture\s+screen|take\s+(a\s+)?screenshot)\b", ut))
                    if not explicit:
                        blocked.append({"type": t, "reason": "screen_capture_not_explicit"})
                        continue

            # Never allow destructive commands.
            if t == "execute_command":
                cmd = a.get("command") or ""
                if LLMAdapter._is_dangerous_command(str(cmd)):
                    blocked.append({"type": t, "reason": "dangerous_command"})
                    continue

            # File ops must stay project-relative (no absolute paths; no traversal).
            if t in ("read", "list", "mkdir", "write", "edit", "delete"):
                p = a.get("path")
                if not LLMAdapter._is_project_relative_path(str(p or "")):
                    blocked.append({"type": t, "reason": "unsafe_path"})
                    continue

            if t == "move":
                p1 = a.get("path")
                p2 = a.get("dest")
                if (not LLMAdapter._is_project_relative_path(str(p1 or ""))) or (not LLMAdapter._is_project_relative_path(str(p2 or ""))):
                    blocked.append({"type": t, "reason": "unsafe_path"})
                    continue

            if t == "copy":
                p1 = a.get("source")
                p2 = a.get("destination")
                if (not LLMAdapter._is_project_relative_path(str(p1 or ""))) or (not LLMAdapter._is_project_relative_path(str(p2 or ""))):
                    blocked.append({"type": t, "reason": "unsafe_path"})
                    continue

            kept.append(a)

        parsed["actions"] = kept
        if blocked:
            base_text = (parsed.get("text") or "").strip()
            safety_note = (
                "Safety: I won’t run actions that modify OS/system files or destructive commands. "
                "If you need help, I can suggest safer steps instead."
            )
            if safety_note not in base_text:
                parsed["text"] = (base_text + "\n\n" + safety_note).strip() if base_text else safety_note

        return parsed

    @staticmethod
    def _is_research_status_question(user_text: str) -> bool:
        """Return True when the user is asking about *our* research task status.

        Examples:
        - "did you finish the research"
        - "do you completed research" (common voice grammar)
        - "what's the research status"
        """
        tl = (user_text or "").strip().lower()
        if not tl:
            return False

        # Avoid accidental web searches for internal state questions.
        return bool(
            re.search(
                r"\b(?:did|do|have|has|are|is)\s+(?:you\s+)?(?:already\s+)?"
                r"(?:complete|completed|finish|finished|done)\s+(?:the\s+)?research\b",
                tl,
            )
            or re.search(r"\b(?:research)\s+(?:status|progress|update)\b", tl)
        )

    @staticmethod
    def _should_use_web_lookup(user_text: str) -> bool:
        """Heuristic policy: only use web tools when truly needed.

        Goal: keep simple PC tasks fast (no web), but allow dynamic learning for unknown
        app steps, documentation, troubleshooting, and "latest" questions.
        """
        t = (user_text or "").strip().lower()
        if not t:
            return False

        # If the user is asking whether *we* completed a research task, this is internal state,
        # not an internet lookup.
        if LLMAdapter._is_research_status_question(user_text):
            return False

        # Internal/self-management requests should not be routed to web lookup.
        self_system_markers = (
            "update yourself",
            "improve yourself",
            "fix yourself",
            "about yourself",
            "your architecture",
            "your system",
            "my assistant",
            "this assistant",
            "my jarvis",
        )
        if any(m in t for m in self_system_markers):
            return False

        primary_intent = LLMAdapter._classify_primary_intent(user_text)
        is_high_level_analysis = False
        try:
            is_high_level_analysis = LLMAdapter._is_high_level_analysis_task(user_text)
        except Exception:
            is_high_level_analysis = False

        if primary_intent in {"action_intent", "generation_intent"}:
            return False
        if primary_intent == "clarification_intent" and not is_high_level_analysis:
            return False

        # If global factual mode is enabled, default to web-first for non-local informational queries.
        # This applies across ALL topics (not domain-specific).
        local_action_markers = (
            "open ",
            "launch ",
            "start ",
            "close ",
            "switch ",
            "write ",
            "type ",
            "format ",
            "rewrite ",
            "make it ",
            "increase ",
            "decrease ",
            "turn on ",
            "turn off ",
            "enable ",
            "disable ",
            "set ",
            "adjust ",
        )
        if bool(getattr(rd, "GLOBAL_FACTUAL_MODE", False)):
            # Internal orchestration/system prompts must not trigger new web loops.
            if not (t.startswith("you are ") or ("provided web context" in t)):
                # Keep simple definition/explanation prompts local for speed unless
                # the user explicitly asks for current/sourced data.
                simple_definition = bool(
                    re.search(r"\b(what is|define|explain|meaning of)\b", t)
                    and len(re.findall(r"[a-z0-9]+", t)) <= 12
                )
                explicit_source_or_fresh = bool(
                    re.search(r"\b(latest|today|current|source|sources|citation|cite|link|links|online|internet)\b", t)
                )
                if simple_definition and not explicit_source_or_fresh:
                    return False
                if not any(t.startswith(m) for m in local_action_markers):
                    return True

        # High-level analysis doesn't always need web (many prompts are conceptual).
        # Only trigger web for analysis when the user explicitly requests research/sources
        # or when the topic is time-sensitive.
        try:
            if is_high_level_analysis:
                analysis_web_triggers = (
                    "research",
                    "do research",
                    "make research",
                    "with sources",
                    "with citations",
                    "with links",
                    "sources",
                    "citations",
                    "as of",
                    "today",
                    "current",
                    "latest",
                    "market",
                    "price",
                    "outlook",
                    "forecast",
                    "2025",
                    "2026",
                    "documentation",
                    "docs",
                    "official",
                )
                if any(s in t for s in analysis_web_triggers):
                    return True
        except Exception:
            pass

        # Strong signals that web lookup is useful/required.
        strong = (
            "latest",
            "today",
            "current",
            "release",
            "version",
            "price",
            "look up",
            "lookup",
            "online",
            "from the internet",
            "source",
            "sources",
            "citation",
            "cite",
            "link",
            "links",
            # Crypto / markets (tends to be time-sensitive)
            "crypto",
            "cryptocurrency",
            "bitcoin",
            "ethereum",
            "btc",
            "eth",
            "altcoin",
            "market cap",
            "dominance",
            "funding rate",
            "open interest",
            "spot etf",
            "etf",
            "exchange inflow",
            "on-chain",
            "token unlock",
            "macro",
            "documentation",
            "docs",
            "official",
            "api reference",
            "how to",
            "steps",
            "tutorial",
            "troubleshoot",
            "error",
            "exception",
            "stack trace",
            "fix this error",
            "why is",

            # Knowledge domains where the user explicitly wants internet-backed answers
            # (e.g., psychology, history, crises) or specific reference sites.
            "psychology",
            "psychological",
            "human psychology",
            "history",
            "historical",
            "earth crisis",
            "climate",
            "climate change",
            "global warming",
            "pandemic",
            "earthquake",
            "volcano",
            "war",
            "conflict",
            "wikipedia",
            "w3schools",
        )
        if any(s in t for s in strong):
            return True

        # If the user is just asking to DO something locally, default to no web.
        if any(t.startswith(m) for m in local_action_markers):
            return False

        # Information-only questions that are not time-sensitive can often be answered without web.
        if re.search(r"\b(what is|define|explain|meaning of)\b", t):
            return False

        # Default: no web unless clear need.
        return False

    @staticmethod
    def _postprocess_web_lookup_policy(user_text: str, parsed: dict) -> dict:
        """Reduce unnecessary web tool usage.

        Rules:
        - If web lookup isn't needed, drop web_search/fetch_url/search actions.
        - If web lookup is needed, keep ONLY the web actions (2-pass pipeline will continue).
        """
        actions = parsed.get("actions") or []
        if not isinstance(actions, list) or not actions:
            parsed["actions"] = actions if isinstance(actions, list) else []
            return parsed

        web_types = {"web_search", "fetch_url", "search"}
        has_web = any(isinstance(a, dict) and (a.get("type") in web_types) for a in actions)
        if not has_web:
            parsed["actions"] = actions
            return parsed

        if not LLMAdapter._should_use_web_lookup(user_text):
            # Drop web actions to keep latency low.
            kept = [a for a in actions if not (isinstance(a, dict) and (a.get("type") in web_types))]
            parsed["actions"] = kept
            return parsed

        # Web lookup is allowed/needed: ensure the plan is strictly "lookup first".
        kept_web = [a for a in actions if isinstance(a, dict) and (a.get("type") in web_types)]
        parsed["actions"] = kept_web
        return parsed

    @staticmethod
    def _postprocess_force_web_lookup(user_text: str, parsed: dict) -> dict:
        """Force a web_search when the request clearly requires online lookup.

        The model sometimes answers "latest/current" questions from memory without emitting web_search.
        This backstop ensures the 2-pass web lookup pipeline runs and avoids hallucinated facts.

        We only force when:
        - Our heuristic says web lookup is needed, AND
        - The model did not already request web_search/fetch_url/search, AND
        - The model did not propose any non-web actions (we don't want to override PC tasks).
        """
        try:
            t = (user_text or "").strip()
            tl = t.lower()

            # Never web-search for internal research task status.
            if LLMAdapter._is_research_status_question(user_text):
                return parsed

            # This adapter is used both for user prompts and internal orchestration prompts.
            # Never force a new web_search for internal "use provided web context" prompts.
            if tl.startswith("you are ") or ("provided web context" in tl):
                return parsed

            primary_intent = LLMAdapter._classify_primary_intent(user_text)
            is_high_level_analysis = False
            try:
                is_high_level_analysis = LLMAdapter._is_high_level_analysis_task(user_text)
            except Exception:
                is_high_level_analysis = False

            if primary_intent != "informational_intent" and not is_high_level_analysis:
                return parsed

            if not LLMAdapter._should_use_web_lookup(user_text):
                return parsed

            actions = parsed.get("actions") or []
            if not isinstance(actions, list):
                actions = []

            web_types = {"web_search", "fetch_url", "search"}
            if any(isinstance(a, dict) and (a.get("type") in web_types) for a in actions):
                return parsed

            # If the model already planned any non-web actions, don't override.
            if any(isinstance(a, dict) and (a.get("type") not in web_types) for a in actions):
                return parsed

            query = t
            if query:
                # If the user explicitly asked to "research X", extract X (avoid searching for the word "research").
                try:
                    m = re.search(
                        r"(?i)\b(?:do\s+research|make\s+research|perform\s+research|deep\s+research|research)\b\s*(?:about|on|regarding)?\s*(.+)$",
                        query,
                    )
                    if m and (m.group(1) or "").strip():
                        query = (m.group(1) or "").strip()
                except Exception:
                    pass

                # Remove common "must look it up online" directives and similar boilerplate.
                query = re.sub(r"(?i)\byou\s+must\b[\s\S]*$", "", query).strip()
                query = re.sub(
                    r"(?i)\b(look\s+(it\s+)?up|search)\b[\s\S]{0,24}?\b(online|on\s+the\s+internet|from\s+the\s+internet)\b",
                    "",
                    query,
                ).strip()
                query = re.sub(r"(?i)\b(as\s+of\s+today|as\s+of\s+now|today)\b[:,]?", "", query).strip()

                # Remove leading question scaffolding.
                query = re.sub(r"(?i)^(what\s+is|what\s+are|tell\s+me|give\s+me|find|search\s+for|look\s+up)\s+", "", query).strip()

                # Remove common "citation/links" suffixes to keep the query clean.
                query = re.sub(
                    r"(?i)\b(include|provide|add|with)\b[\s\S]{0,80}?\b(source|sources|links?|citations?)\b[\s\S]*$",
                    "",
                    query,
                ).strip()
                query = re.sub(r"(?i)\b(and|please|kindly)\s*$", "", query).strip(" .-\t")

                # Strip trailing punctuation and over-long prose.
                query = re.sub(r"[\?\"\u201c\u201d]", "", query).strip()
                query = re.sub(r"\s+", " ", query).strip()
                # Convert to keyword-style query for better search reliability.
                stop = {
                    "the","a","an","and","or","to","of","for","in","on","with","this","that","today","now","as","is","are","was","were",
                    "must","please","include","provide","sources","source","links","link","look","up","online","from","internet","summarize","summary",
                    "analyze","analysis","scenarios","scenario","bull","bear","base","current","trend","drivers","risks","assumptions",
                    # Research-request boilerplate (avoid biasing searches toward "research methodology")
                    "research","researching","perform","performing","make","making","do","doing","talking","mean","update","yourself","myself","better","version","proper","results","result","notes",
                    "about","regarding",
                }
                toks = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t and t not in stop]
                if toks:
                    query = " ".join(toks[:10]).strip()
                if len(query) > 120:
                    query = " ".join(query.split()[:12]).strip()
            if not query:
                query = (user_text or "").strip()

            parsed["text"] = (parsed.get("text") or "Looking it up online.").strip() or "Looking it up online."
            parsed["actions"] = [{"type": "web_search", "query": query, "num_results": 5}]
            return parsed
        except Exception:
            return parsed

    async def generate_response(self, text: str, context: str = "", mode="chat", capabilities=None, user_prefs: dict | None = None):
        original_text = str(text or "")
        pending_note = ""
        try:
            self._ensure_local_reasoner_state_scope(user_prefs)
        except Exception:
            pass

        runtime_task_context = self._get_runtime_task_context(user_prefs)
        habits_block = self._user_habits_prompt_block(user_prefs)
        intelligence_state = self._build_unified_intelligence_state(
            user_text=original_text,
            context_state=runtime_task_context,
            user_prefs=user_prefs,
        )
        proactive_mode = str(intelligence_state.get("proactive_mode") or "assist").strip().lower()
        runtime_task_context["proactive_mode"] = proactive_mode
        self._set_runtime_task_context(user_prefs, runtime_task_context)

        # Smart waiting: with no input, continue only when safe in auto mode; otherwise wait.
        if not str(original_text or "").strip():
            if proactive_mode == "auto" and bool(runtime_task_context.get("task_active")):
                nxt = self._derive_auto_next_action(runtime_task_context)
                if isinstance(nxt, dict) and nxt.get("type"):
                    return self._finalize_response_with_context(
                        user_text="",
                        planning_text="",
                        parsed={
                            "text": "",
                            "actions": [nxt],
                            "source": "proactive-auto-wait",
                            "_planning_confidence": 0.9,
                            "_proactive_mode": proactive_mode,
                        },
                        user_prefs=user_prefs,
                    )
            return self._finalize_response_with_context(
                user_text="",
                planning_text="",
                parsed={
                    "text": "",
                    "actions": [],
                    "source": "proactive-wait",
                    "_planning_confidence": 1.0,
                    "_proactive_mode": proactive_mode,
                },
                user_prefs=user_prefs,
            )

        # Interrupt handling + multi-task control.
        if bool(runtime_task_context.get("task_active")):
            if bool(intelligence_state.get("interrupt")):
                runtime_task_context = self._override_active_task_context(
                    context_state=runtime_task_context,
                    reason="interrupt",
                    user_prefs=user_prefs,
                )
            elif bool(intelligence_state.get("new_task")):
                runtime_task_context = self._override_active_task_context(
                    context_state=runtime_task_context,
                    reason="replace",
                    user_prefs=user_prefs,
                )

        pending_task_original = str(runtime_task_context.get("pending_clarification_original_text") or "").strip() if isinstance(runtime_task_context, dict) else ""
        pending_question = str(runtime_task_context.get("pending_clarification_question") or "").strip() if isinstance(runtime_task_context, dict) else ""
        side_question_with_pending = False

        if pending_task_original and str(original_text or "").strip():
            now_intent = self._classify_primary_intent(original_text)
            if now_intent == "informational_intent" and not self._looks_like_continuation_request(original_text):
                side_question_with_pending = True
                if pending_question:
                    pending_note = f"When you're ready, {pending_question}"
            else:
                merged = f"{pending_task_original}. Additional details: {str(original_text or '').strip()}"
                original_text = merged
                runtime_task_context = self._clear_pending_clarification(runtime_task_context)
                self._set_runtime_task_context(user_prefs, runtime_task_context)

        planning_text = self._resolve_contextual_user_text(original_text, runtime_task_context)
        text = planning_text or original_text

        if runtime_task_context:
            context_block = self._runtime_context_prompt_block(runtime_task_context)
            context = (
                f"{(context or '').strip()}\n\n"
                "[RUNTIME_TASK_CONTEXT]\n"
                f"{context_block}\n\n"
                "[USER_HABITS_MEMORY]\n"
                f"{habits_block}"
            ).strip()

        def _finalize(parsed: dict) -> dict:
            payload = parsed if isinstance(parsed, dict) else {"text": str(parsed or ""), "actions": []}
            if side_question_with_pending and isinstance(payload, dict):
                acts = payload.get("actions") if isinstance(payload.get("actions"), list) else []
                if not acts and pending_note:
                    base = str(payload.get("text") or "").strip()
                    if pending_note.lower() not in base.lower():
                        payload["text"] = (base + "\n\n" + pending_note).strip() if base else pending_note
            try:
                if isinstance(payload, dict):
                    payload["_proactive_mode"] = proactive_mode
                    payload["_response_mode"] = str(mode or "chat")
            except Exception:
                pass
            return self._finalize_response_with_context(
                user_text=str(text or original_text or ""),
                planning_text=text,
                parsed=payload,
                user_prefs=user_prefs,
            )

        try:
            mixed = self._build_mixed_intent_generation_first_response(text)
            if isinstance(mixed, dict):
                mixed["routing"] = {
                    "provider": "deterministic",
                    "model": "local-fast-path",
                    "fallback_used": True,
                }
                try:
                    mixed["emotion"] = mixed.get("emotion") or self._infer_emotion(mixed.get("text") or text)
                except Exception:
                    pass
                return _finalize(mixed)
        except Exception:
            pass

        intelligence_state = self._build_unified_intelligence_state(
            user_text=text,
            context_state=runtime_task_context,
            user_prefs=user_prefs,
        )
        primary_intent = str(intelligence_state.get("primary_intent") or self._classify_primary_intent(text))
        intent_profile = {
            "intent_type": intelligence_state.get("intent_type"),
            "intent_depth": intelligence_state.get("intent_depth"),
            "response_strategy": intelligence_state.get("response_strategy"),
        }

        try:
            if intent_profile.get("intent_type") == "goal_oriented":
                goal_plan = self._build_goal_oriented_plan_response(text, user_prefs=user_prefs)
                if isinstance(goal_plan, dict):
                    goal_plan["routing"] = {
                        "provider": "deterministic",
                        "model": "local-fast-path",
                        "fallback_used": True,
                    }
                    try:
                        goal_plan["emotion"] = goal_plan.get("emotion") or self._infer_emotion(goal_plan.get("text") or text)
                    except Exception:
                        pass
                    return _finalize(goal_plan)
        except Exception:
            pass

        try:
            repeated = self._get_cached_response(original_text, max_age_s=240)
            if isinstance(repeated, dict) and not (repeated.get("actions") or []):
                repeated_text = str(repeated.get("text") or "").strip()
                if repeated_text and primary_intent in {"generation_intent", "action_intent", "clarification_intent"}:
                    return _finalize({
                        "text": self._compact_repeat_reply(text, repeated_text),
                        "actions": [],
                        "source": "repeat-aware-cache",
                        "routing": {
                            "provider": "deterministic",
                            "model": "local-fast-path",
                            "fallback_used": True,
                        },
                    })
        except Exception:
            pass

        if primary_intent == "action_intent":
            try:
                deterministic_action = self._preparse_deterministic_voice_actions(text)
                if isinstance(deterministic_action, dict) and isinstance(deterministic_action.get("actions"), list) and deterministic_action.get("actions"):
                    try:
                        deterministic_action = self._postprocess_multi_step_chain_actions(user_text=text, parsed=deterministic_action)
                    except Exception:
                        pass
                    deterministic_action["routing"] = {
                        "provider": "deterministic",
                        "model": "local-fast-path",
                        "fallback_used": True,
                    }
                    try:
                        deterministic_action["emotion"] = deterministic_action.get("emotion") or self._infer_emotion(deterministic_action.get("text") or text)
                    except Exception:
                        pass
                    return _finalize(deterministic_action)
            except Exception:
                pass

        if primary_intent == "generation_intent":
            try:
                generated = self._build_direct_generation_response(text)
                if isinstance(generated, dict):
                    generated = self._postprocess_proactive_followup(text, generated)
                    generated["routing"] = {
                        "provider": "deterministic",
                        "model": "local-fast-path",
                        "fallback_used": True,
                    }
                    try:
                        generated["emotion"] = generated.get("emotion") or self._infer_emotion(generated.get("text") or text)
                    except Exception:
                        pass
                    return _finalize(generated)
            except Exception:
                pass

        quick = self._quick_local_chat_reply(text)
        if isinstance(quick, dict):
            quick["routing"] = {
                "provider": "deterministic",
                "model": "local-fast-path",
                "fallback_used": True,
            }
            try:
                quick["emotion"] = quick.get("emotion") or self._infer_emotion(quick.get("text") or text)
            except Exception:
                pass
            logger.info(
                "[llm.fast_path] mode=%s source=%s text_preview=%s",
                str(mode or "chat"),
                str(quick.get("source") or "deterministic-local-chat"),
                (text or "")[:80].replace("\n", " "),
            )
            return _finalize(quick)

        """
        Generate a rich, humanlike structured response.
        
        Architecture: Goal → Plan → Execute → Evaluate → Improve
        This method handles: Goal (detect intent) + Plan (generate actions)
        Execution, evaluation, and improvement are handled by callers after agent runs.
        """
        # Voice + web-worthy requests: do web_search first (better UX, fewer hallucinations).
        # We only do this when we did NOT match a deterministic PC command above.
        try:
            if (mode or "").lower() == "voice":
                is_analysis = self._is_high_level_analysis_task(text)
                if (primary_intent == "informational_intent" or is_analysis) and self._should_use_web_lookup(text):
                    parsed = {"text": "Looking it up online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "voice-pre-web"
                        return _finalize(parsed)
        except Exception:
            pass
        # Optional offline mode: reduce dependency on OpenAI for high-level analysis.
        # If enabled, we skip the LLM call and trigger web_search directly (2-pass pipeline
        # will still run, and backend will synthesize if continuation fails).
        try:
            tl = (text or "").strip().lower()
            offline_analysis = bool(rd.OFFLINE_ANALYSIS)
            offline_only = bool(rd.OFFLINE_ONLY)
            offline_web_only = bool(rd.OFFLINE_WEB_ONLY)

            is_internal = tl.startswith("you are ") or ("provided web context" in tl)
            if not is_internal:
                if primary_intent == "informational_intent" and (offline_only or offline_web_only or (offline_analysis and self._is_high_level_analysis_task(text))) and self._should_use_web_lookup(text):
                    parsed = {"text": "Looking it up online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    # Ensure we actually returned a web_search action.
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "offline-web"
                        return _finalize(parsed)

                # Also bypass OpenAI when there's no key configured, but only for
                # web-required high-level questions (keeps local automation usable).
                if primary_intent == "informational_intent" and (not self.primary_key and not self.backup_key) and self._should_use_web_lookup(text) and self._is_high_level_analysis_task(text):
                    parsed = {"text": "Looking it up online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "offline-web"
                        return _finalize(parsed)
        except Exception:
            # Never break response generation due to offline toggle logic.
            pass

        # Chat-mode research: when the user explicitly requests research/sources or time-sensitive
        # info, force the 2-pass web pipeline immediately (avoids answering from memory).
        try:
            tl = (text or "").strip().lower()
            is_internal = tl.startswith("you are ") or ("provided web context" in tl)
            if not is_internal and (mode or "").lower() != "voice":
                explicit_research = bool(
                    re.search(
                        r"\b(research|do\s+research|make\s+research|with\s+sources|with\s+citations|with\s+links|"
                        r"sources?|citations?|cite|links?|look\s+up|lookup|online|from\s+the\s+internet|"
                        r"latest|today|current|as\s+of)\b",
                        tl,
                    )
                )
                if primary_intent == "informational_intent" and explicit_research and self._should_use_web_lookup(text):
                    parsed = {"text": "Researching online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "pre-web"
                        return _finalize(parsed)
        except Exception:
            pass

        # Persona may be overridden per-user.
        persona_key = self.persona
        try:
            if isinstance(user_prefs, dict):
                pk = (user_prefs.get("persona") or "").strip()
                if pk in self.personality:
                    persona_key = pk
        except Exception:
            pass

        persona = self.personality.get(persona_key, self.personality["formal-gentle"])
        prefix = persona["prefix"]
        tone = persona["tone"]

        max_tokens, temperature = self._choose_generation_params(text=text, mode=mode)
        max_tokens, temperature = self._apply_preference_overrides(max_tokens, temperature, mode=mode, user_prefs=user_prefs)
        caps = capabilities or []
        caps_str = ", ".join([str(c) for c in caps if c]) if isinstance(caps, (list, tuple)) else str(caps)

        preferred_language = None
        preferred_language_code = None
        try:
            if isinstance(user_prefs, dict):
                preferred_language = (user_prefs.get("language") or user_prefs.get("language_name") or "").strip()
                preferred_language_code = (user_prefs.get("language_code") or "").strip()
        except Exception:
            preferred_language = None
            preferred_language_code = None

        if not preferred_language_code and preferred_language:
            if re.fullmatch(r"[a-zA-Z]{2}(-[a-zA-Z]{2})?", preferred_language):
                preferred_language_code = preferred_language

        skills_block = ""
        try:
            skills_block = self._skills_prompt_block()
        except Exception:
            skills_block = ""

        # Optional: context-aware decision hints (and fast-path for high-confidence voice commands).
        decision_hint = None
        try:
            dm = await self._ensure_decision_maker()
            if dm is not None:
                decision = await dm.decide_action(text, context=context or "")
                if isinstance(decision, dict):
                    # Keep the hint small; do not leak full PC profile into the prompt.
                    parsed_intent = (decision.get("parsed_intent") or {}) if isinstance(decision.get("parsed_intent"), dict) else {}
                    recommended = decision.get("recommended_action") if isinstance(decision.get("recommended_action"), dict) else None
                    decision_hint = {
                        "intent": parsed_intent.get("intent"),
                        "target": parsed_intent.get("target"),
                        "confidence": float(decision.get("confidence") or 0.0),
                        "recommended_action": recommended,
                    }

        except Exception:
            decision_hint = None

        base_conf = float((decision_hint or {}).get("confidence") or 0.0) if isinstance(decision_hint, dict) else 0.0
        unified_conf = float(intelligence_state.get("confidence") or 0.7)
        planning_confidence = base_conf if base_conf > 0 else unified_conf

        # If the assistant likely lacks knowledge (unknown/low-confidence intent), auto-search the web.
        # This triggers the existing 2-pass web pipeline (search -> continue with web context).
        try:
            auto_unknown = bool(rd.AUTO_WEB_ON_UNKNOWN)
            if auto_unknown and primary_intent == "informational_intent" and self._is_informational_question(text):
                conf = 0.0
                intent = None
                if isinstance(decision_hint, dict):
                    conf = float(decision_hint.get("confidence") or 0.0)
                    intent = (decision_hint.get("intent") or "").strip().lower() if decision_hint.get("intent") else None
                if (intent in {None, "", "unknown"} or conf < 0.35) and not self._should_use_web_lookup(text):
                    parsed = {"text": "Looking it up online.", "actions": []}
                    parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    if parsed.get("actions"):
                        parsed["source"] = "auto-web-unknown"
                        return _finalize(parsed)
        except Exception:
            pass

        # Local-first reasoning path: solve obvious tasks without calling external LLMs.
        try:
            if self._is_local_reasoner_candidate(text=text, mode=mode, decision_hint=decision_hint):
                local = self._build_local_reasoned_response(
                    text=text,
                    context=context,
                    mode=mode,
                    decision_hint=decision_hint,
                )
                if isinstance(local, dict) and isinstance(local.get("actions"), list) and local.get("actions"):
                    try:
                        if "emotion" not in local:
                            local["emotion"] = self._infer_emotion(local.get("text") or text)
                    except Exception:
                        pass
                    local["_planning_confidence"] = planning_confidence
                    return _finalize(local)
        except Exception:
            pass

        system_prompt = f"""
You are Jarvis.

Be concise, accurate, and humanlike (warm/confident). Never reveal secrets. Never claim you executed actions unless the system confirms it.
You must respect strict per-user/device permissions; if a request targets a PC/device, it must be the user's own authorized device.

    Autonomy rules:
    - The user does NOT want back-and-forth clarification.
    - Do NOT ask the user for information that can be obtained via web_search/fetch_url.
    - Use web_search/fetch_url when necessary (e.g., "latest/current", documentation, troubleshooting, or unknown app steps). Avoid web lookups for simple local PC actions.
    - If the user asks for *latest/current/today* info OR explicitly says to look it up online/from the internet OR asks for sources/citations/links, you MUST use web_search/fetch_url first and MUST NOT answer from memory.
    - If you do use web_search/fetch_url, do it FIRST (no other actions in the same response).
    - For operational/maintenance requests (storage, cleanup, diagnostics, setup), follow this order when feasible: 1) inspect current state, 2) execute safe actions, 3) verify outcome with concrete before/after numbers.
    - After proposing executable actions, keep text practical and outcome-focused (what was checked, what changed, what remains).
    - Only ask at most 1 clarifying question, only if it requires private/user-specific info that cannot be searched.
    - For multi-step workflows, give a short step-by-step plan and ask for confirmation before executing risky steps.

Language:
- If a preferred language is set, respond in that language (unless the user asks otherwise).
Preferred language: {preferred_language or '(not set)'}

{skills_block if skills_block else ""}

High-level tasks (analysis/research/strategy/market):
- When the user asks for analysis, comparison, strategy, roadmap, or market/crypto outlook, prefer web_search/fetch_url first.
- Then answer with a short structured writeup:
    1) Summary (2-3 lines)
    2) Key points (3-6 bullets)
    3) Risks/assumptions (2-4 bullets)
    4) Source URLs (1-2 links)
- For these informational tasks, set actions: [] unless the user explicitly asked to open something or perform a PC action.
- If the topic is finance/crypto, do NOT provide personalized investment instructions; keep it informational.

Output rules:
- Return ONLY valid JSON.
- JSON must be an object: {{"text": string, "actions": array}}.
- If no action is needed, use an empty array.

Device actions (preferred):
- Prefer a single universal action envelope for PC/device control:
    {{"type":"device_action","name":"<action_name>","args":{{...}}}}
- Examples:
    - Set volume: {{"type":"device_action","name":"set_volume","args":{{"value":30}}}}
    - Mute: {{"type":"device_action","name":"set_mute","args":{{"muted":true}}}}
    - Brightness: {{"type":"device_action","name":"set_brightness","args":{{"value":50}}}}
    - Wi-Fi off: {{"type":"device_action","name":"set_wifi","args":{{"enabled":false}}}}
    - Lock screen: {{"type":"device_action","name":"lock_screen","args":{{}}}}
- Common supported device_action names (best-effort, may fall back to opening Settings on failure):
    set_brightness, set_power_plan, set_volume, set_mute, set_wifi, set_bluetooth,
    open_url, open_path, get_clipboard, set_clipboard, list_processes, kill_process,
    show_desktop, open_task_manager, open_run_dialog, open_start_menu,
    open_quick_settings, open_notification_center,
    window_snap_left, window_snap_right, window_maximize, window_minimize,
    media_play_pause, media_next_track, media_prev_track, media_stop,
    open_settings, lock_screen, alt_tab, find_files, save_screenshot.
- Destructive power actions MUST include args.confirm=true:
    shutdown, restart, sleep, hibernate, logoff.

Decision-making hints (non-binding, use if helpful):
{json.dumps(decision_hint, ensure_ascii=False) if decision_hint else "(none)"}

    Current allowed capabilities (soft constraint): {caps_str if caps_str else '(not specified)'}

Allowed action types (only include required fields):
- open_url: {{"type":"open_url","url":"https://..."}}  # preferred: direct URL
- web_search: {{"type":"web_search","query":"...","num_results":5}}
- fetch_url: {{"type":"fetch_url","url":"https://..."}}
- generate_email: {{"type":"generate_email","recipient":"...","subject":"...","body_prompt":"...","tone":"professional"}}
- open_app: {{"type":"open_app","app_name":"...","args":[]}}
- close_app: {{"type":"close_app","app_name":"..."}}
- switch_app: {{"type":"switch_app","app_name":"..."}}
- execute_command: {{"type":"execute_command","command":"...","wait":true}}
- type_text: {{"type":"type_text","text":"...","interval":0.02}}
- press_key: {{"type":"press_key","key":"enter","presses":1}}
- hotkey: {{"type":"hotkey","keys":["ctrl","a"]}}
- n8n_webhook: {{"type":"n8n_webhook","path":"your-webhook","method":"POST","payload":{{"key":"value"}}}}

# Filesystem actions (project-relative paths only; never absolute):
- read: {{"type":"read","path":"src/..."}}
- list: {{"type":"list","path":"src/..."}}
- mkdir: {{"type":"mkdir","path":"src/new_folder"}}
- write: {{"type":"write","path":"src/file.txt","content":"..."}}
- edit: {{"type":"edit","path":"src/file.txt","content":"..."}}
- delete: {{"type":"delete","path":"src/file.txt"}}
- move: {{"type":"move","path":"src/a.txt","dest":"src/b.txt"}}
- copy: {{"type":"copy","source":"src/a.txt","destination":"src/b.txt"}}
- cleanup: {{"type":"cleanup"}}
- find_files: {{"type":"find_files","query":"keyword","path":"src","in_content":true,"max_results":20}}

# Screen actions (must be explicitly requested by the user):
- capture_screen: {{"type":"capture_screen","region":{{"x":0,"y":0,"width":800,"height":600}}}}
- screen_navigation: {{"type":"screen_navigation","command":"scroll|click|type_text|press_key|hotkey|read_screen|find_text|move_mouse|get_mouse_position", "text":"...", "x":0, "y":0}}

# Task helpers:
- create_task: {{"type":"create_task","description":"...","steps":["..."],"priority":5}}
- stop_task: {{"type":"stop_task"}}
- check_errors: {{"type":"check_errors"}}
- fix_errors: {{"type":"fix_errors"}}

# MCP tools (only when explicitly appropriate):
- mcp_tool: {{"type":"mcp_tool","tool":"...","args":{{}}}}

- set_mode: {{"type":"set_mode","mode":"learn|update|execute|analyze|develop|creative|interact"}}

Safety rules for actions:
- Prefer fewer actions.
- If the user asks to open an app and write content, include BOTH open_app and type_text (and press_key only if needed).
- If the user asks to format/rewrite/polish/fix text (including: "format this", "make it professional", "convert to bullet points"), you MUST include the full final text in a type_text action (not only in the explanation). The user expects you to actually apply the change.
- For "format this" follow-ups, assume you are replacing the whole current document unless the user specifies a smaller range.
- Never output actions that can damage or remove the OS (e.g., formatting disks, disk partition tools, deleting system folders, boot/registry edits). If asked, refuse and return no such actions.
- For PC Settings/configuration requests: you may open the relevant settings page (e.g., Windows ms-settings: URIs) and provide safe step-by-step guidance, but do NOT apply security-critical/system-destructive changes.
- For PC automation tasks (low → high difficulty):
    - Low: open the right app/site/settings page.
    - Medium: switch_app + type_text/hotkey to edit content.
    - High: use web_search/fetch_url to learn the correct steps/tools first, then propose a minimal safe action plan.
- If the user is continuing an editing task (e.g., "format this", "rewrite this", "fix this"), do NOT reopen apps. Prefer switch_app and then edit/replace text.
- When the user says "this/that/same" (continuation), assume they mean the currently open/previously used app/document unless they explicitly ask to open a new one.
- If details are missing, make reasonable assumptions and still provide a helpful answer.
- Only ask at most 1 clarifying question, and only at the end (optional), and do NOT block the answer on it.
- If you truly cannot proceed without a specific detail (rare), ask the question and return no actions.
- For filesystem-related actions, use ONLY project-relative paths and never touch secrets.

Style tone: {tone}.
"""

        # Universal learning: inject prior cycle feedback so the LLM adapts to
        # past successes/failures for ANY scenario without new deterministic handlers.
        cycle_ctx = ""
        try:
            cycle_ctx = self._get_cycle_context_for_goal(text)
        except Exception:
            cycle_ctx = ""

        effective_context = context or ""
        if cycle_ctx:
            effective_context = (cycle_ctx + "\n\n" + effective_context).strip()

        complexity = self._estimate_complexity(text, mode)
        qtype = self._knowledge_query_type(text)
        include_rag = bool(complexity >= 1 and qtype in {"project", "debug", "system_behavior", "task_history"})
        runtime_ctx = self._fetch_recent_runtime_context(text, limit=3, include_rag=include_rag)
        cached_best = str((runtime_ctx or {}).get("cached_best_response") or "").strip()
        if cached_best and complexity == 0:
            return _finalize({
                "text": cached_best,
                "actions": [],
                "source": "learning-memory-cache",
            })
        if complexity == 0:
            cached_quick = self._get_cached_response(original_text, max_age_s=600)
            if isinstance(cached_quick, dict) and not (cached_quick.get("actions") or []):
                cached_quick["source"] = "fast-cache"
                return _finalize(cached_quick)
        reasoning_hint = self._build_reasoning_hint(text)
        short_mem = self._short_term_memory_block()
        long_mem = self._long_term_memory_block(user_prefs=user_prefs)
        delegation_hint = self._build_delegation_hint(text, decision_hint=decision_hint)

        def _build_user_prompt(context_window: int) -> str:
            clipped_ctx = effective_context[-context_window:] if effective_context else "(none)"
            recent_chat = "\n".join(f"- {x}" for x in (runtime_ctx.get("recent_chat") or [])[:3]) or "(none)"
            rag_context = "\n".join(f"- {x}" for x in (runtime_ctx.get("rag_context") or [])[:4]) or "(none)"
            last_task_outcome = str(runtime_ctx.get("last_task_outcome") or "(none)")
            error_fix_hints = "\n".join(f"- {x}" for x in (runtime_ctx.get("error_fix_hints") or [])[:3]) or "(none)"
            learning_hints = "\n".join(f"- {x}" for x in (runtime_ctx.get("learning_hints") or [])[:3]) or "(none)"
            return (
                "[USER_QUERY]\n"
                f"{text}\n\n"
                "[USER_HABITS_MEMORY]\n"
                f"{habits_block}\n\n"
                "[RECENT_CHAT_CONTEXT]\n"
                f"{recent_chat}\n\n"
                "[RAG_CONTEXT]\n"
                f"{rag_context}\n\n"
                "[LAST_TASK_OUTCOME]\n"
                f"{last_task_outcome}\n\n"
                "[SHORT_TERM_MEMORY]\n"
                f"{short_mem}\n\n"
                "[LONG_TERM_MEMORY]\n"
                f"{long_mem}\n\n"
                "[RUNTIME_REASONING_HINT]\n"
                f"{reasoning_hint}\n\n"
                "[DELEGATION_POLICY_HINT]\n"
                f"{delegation_hint}\n\n"
                "[ERROR_FIX_HINTS]\n"
                f"{error_fix_hints}\n\n"
                "[LEARNING_HINTS]\n"
                f"{learning_hints}\n\n"
                "[CONTEXTUAL_TASK_MEMORY]\n"
                f"{self._runtime_context_prompt_block(runtime_task_context)}\n\n"
                "[EXTERNAL_CONTEXT]\n"
                f"{clipped_ctx}\n\n"
                "Return ONLY valid JSON matching:\n"
                "{\n"
                "  \"text\": \"...\",\n"
                "  \"actions\": [{\"type\": \"...\"}]\n"
                "}\n"
            )

        try:
            start = datetime.now(timezone.utc)
            chosen_model = self._choose_model_for_request(text=text, mode=mode)
            chosen_provider = str(self.provider or "openai_compatible").strip().lower()
            route = self._resolve_model_ops_route(text=text, mode=mode)
            route_task_type = None
            route_profile = None
            route_fallback = None
            if isinstance(route, dict):
                route_task_type = route.get("task_type")
                route_profile = route.get("profile")
                p = route.get("primary") if isinstance(route.get("primary"), dict) else {}
                f = route.get("fallback") if isinstance(route.get("fallback"), dict) else {}
                p_model_id = str(p.get("model_id") or "").strip()
                p_provider = str(p.get("provider") or "").strip().lower()
                if p_model_id:
                    chosen_model = self._map_model_id_to_runtime_model(
                        p_model_id,
                        primary_model=str(self.primary_model or ""),
                        smart_model=str(self.smart_model or ""),
                        backup_model=str(self.backup_model or ""),
                    )
                if p_provider:
                    chosen_provider = p_provider
                route_fallback = f

            if self.cloud_mode and chosen_provider in {"ollama", "local_model"}:
                logger.info("[LLM] Skipping local provider in cloud mode")
                chosen_provider = "openai_compatible"

            prompt_windows = [1200, 800] if complexity == 0 else [2000, 1200, 800]
            budget_started = time.monotonic()
            # For simple chat, reserve enough time for backup provider attempts.
            request_budget_s = min(float(self.provider_budget_s), 16.0) if complexity == 0 else float(self.provider_budget_s)
            request_timeout_s = min(float(self.provider_timeout_s), 4.0) if complexity == 0 else float(self.provider_timeout_s)
            max_primary_attempts = 1 if complexity == 0 else len(prompt_windows)

            logger.info(
                "[llm.request] mode=%s provider=%s model=%s budget_s=%s task_type=%s profile=%s",
                str(mode or "chat"),
                str(chosen_provider or ""),
                str(chosen_model or ""),
                str(request_budget_s),
                str(route_task_type or "unknown"),
                str(route_profile or "default"),
            )

            response = None
            provider_source = "openai"
            routed_model = chosen_model
            last_err = None
            primary_provider_name = "ollama" if chosen_provider in {"ollama", "local_model"} else "openai"
            primary_endpoint_is_local = self._is_local_provider_url(self.primary_endpoint)
            fallback_endpoint_is_local = self._is_local_provider_url(self.backup_endpoint)

            # Primary provider retries with progressively smaller context windows.
            if self.cloud_mode and (
                primary_provider_name == "ollama" or primary_endpoint_is_local
            ):
                logger.info("[LLM] Skipping local provider in cloud mode")
                last_err = Exception("primary provider skipped in cloud mode: local endpoint/provider")
            elif self._provider_available(primary_provider_name):
                for attempt, ctx_window in enumerate(prompt_windows[:max_primary_attempts]):
                    elapsed = time.monotonic() - budget_started
                    remaining = max(0.0, float(request_budget_s) - elapsed)
                    if remaining <= 0.0:
                        last_err = TimeoutError("provider budget exhausted before primary response")
                        logger.warning("[llm.provider.timeout] stage=primary reason=budget_exhausted")
                        break
                    try:
                        user_prompt = _build_user_prompt(ctx_window)
                        if chosen_provider in {"ollama", "local_model"}:
                            provider_source = "ollama"
                            routed_model = chosen_model
                            logger.info(
                                "[llm.provider.start] provider=ollama attempt=%s ctx_window=%s remaining_s=%.2f",
                                attempt + 1,
                                ctx_window,
                                remaining,
                            )
                            response = await asyncio.wait_for(
                                self._call_ollama_chat(
                                    [
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": user_prompt},
                                    ],
                                    temperature=temperature,
                                    model=chosen_model,
                                    endpoint=self.primary_endpoint,
                                ),
                                timeout=max(1.0, min(float(request_timeout_s), remaining)),
                            )
                        else:
                            provider_source = "openai"
                            routed_model = chosen_model
                            logger.info(
                                "[llm.provider.start] provider=openai attempt=%s ctx_window=%s remaining_s=%.2f",
                                attempt + 1,
                                ctx_window,
                                remaining,
                            )
                            response = await asyncio.wait_for(
                                self._call_openai(
                                    [
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": user_prompt},
                                    ],
                                    max_tokens=max(256, min(max_tokens, self.max_max_tokens)),
                                    temperature=temperature,
                                    model=chosen_model,
                                    endpoint=self.primary_endpoint,
                                    api_key=self.primary_key,
                                ),
                                timeout=max(1.0, min(float(request_timeout_s), remaining)),
                            )
                        self._mark_provider_success(primary_provider_name)
                        break
                    except Exception as e_primary:
                        last_err = e_primary
                        logger.warning(
                            "[llm.provider.fail] provider=%s attempt=%s error=%s",
                            "ollama" if chosen_provider in {"ollama", "local_model"} else "openai",
                            attempt + 1,
                            str(e_primary),
                        )
                        print(f"[LLM WARN] Primary provider failed (attempt {attempt + 1}): {e_primary}")
                        if attempt < (max_primary_attempts - 1):
                            try:
                                backoff = min(0.25 * float(attempt + 1), 0.7)
                                if backoff > 0:
                                    await asyncio.sleep(backoff)
                            except Exception:
                                pass
                        if attempt >= 1 and not self._is_transient_provider_error(e_primary):
                            break
                if response is None:
                    self._mark_provider_failure(primary_provider_name)
            else:
                last_err = Exception(f"primary provider cooling down: {primary_provider_name}")
                logger.info("[llm.provider.skip] provider=%s reason=cooldown", primary_provider_name)

            # Fallback provider (Groq/OpenAI-compatible) with compact context.
            if response is None:
                fallback_provider = "groq"
                fallback_model = self.backup_model
                if isinstance(route_fallback, dict):
                    fb_provider = str(route_fallback.get("provider") or "").strip().lower()
                    fb_model_id = str(route_fallback.get("model_id") or "").strip()
                    if fb_provider:
                        fallback_provider = fb_provider
                    if fb_model_id:
                        fallback_model = self._map_model_id_to_runtime_model(
                            fb_model_id,
                            primary_model=str(self.primary_model or ""),
                            smart_model=str(self.smart_model or ""),
                            backup_model=str(self.backup_model or ""),
                        )

                if fallback_provider in {"fallback", "local_model"}:
                    raise last_err or Exception("Primary provider failed and deterministic fallback selected")

                if self.cloud_mode and fallback_endpoint_is_local:
                    logger.info("[LLM] Skipping local provider in cloud mode")
                    raise last_err or Exception("Backup provider skipped in cloud mode: local endpoint")

                if not self._provider_available(fallback_provider):
                    raise last_err or Exception(f"Backup provider cooling down: {fallback_provider}")

                if not self.backup_key and fallback_provider in {"groq", "openai_compatible", "openai"}:
                    raise last_err or Exception("Primary provider failed")
                elapsed = time.monotonic() - budget_started
                remaining = max(0.0, float(request_budget_s) - elapsed)
                if remaining <= 0.0:
                    raise TimeoutError("provider budget exhausted before fallback response")
                provider_source = "groq" if fallback_provider in {"groq", "openai_compatible", "openai"} else fallback_provider
                routed_model = fallback_model
                fallback_prompt = _build_user_prompt(prompt_windows[-1])
                fallback_attempts = 2
                for fb_attempt in range(fallback_attempts):
                    elapsed = time.monotonic() - budget_started
                    remaining = max(0.0, float(request_budget_s) - elapsed)
                    if remaining <= 0.0:
                        break
                    logger.info("[llm.provider.start] provider=%s attempt=%s remaining_s=%.2f", provider_source, fb_attempt + 1, remaining)
                    try:
                        response = await asyncio.wait_for(
                            self._call_openai(
                                [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": fallback_prompt},
                                ],
                                max_tokens=max(256, min(max_tokens, self.max_max_tokens)),
                                temperature=temperature,
                                model=fallback_model,
                                endpoint=self.backup_endpoint,
                                api_key=self.backup_key,
                            ),
                            timeout=max(1.0, min(float(request_timeout_s), remaining)),
                        )
                        self._mark_provider_success(fallback_provider)
                        break
                    except Exception as e_fallback:
                        last_err = e_fallback
                        if fb_attempt < (fallback_attempts - 1):
                            try:
                                backoff = min(0.3 * float(fb_attempt + 1), 0.8)
                                if backoff > 0:
                                    await asyncio.sleep(backoff)
                            except Exception:
                                pass
                            continue
                        self._mark_provider_failure(fallback_provider)
                        raise
            if chosen_provider in {"ollama", "local_model"} and isinstance(response, dict):
                msg = response.get("message") or {}
                content = str(msg.get("content") or "").strip()
            else:
                content = response["choices"][0]["message"]["content"].strip()

            # Attempt to parse JSON safely
            try:
                parsed = json.loads(content)
            except:
                # Extract JSON substring if model returned mixed text
                match = re.search(r"\{[\s\S]*\}", content)
                if match:
                    try:
                        parsed = json.loads(match.group())
                    except:
                        parsed = {"text": content, "actions": []}
                else:
                    parsed = {"text": content, "actions": []}

            # Ensure structure
            if "text" not in parsed:
                parsed["text"] = content
            if "actions" not in parsed:
                parsed["actions"] = []

            parsed = self._sanitize_output_text(text, parsed)

            # Post-process: if user asked to write/type in an app, ensure we emit type_text.
            # This makes the system robust even when the LLM returns only open_app.
            try:
                parsed = self._postprocess_write_actions(user_text=text, parsed=parsed)
            except Exception:
                # never fail the response due to postprocessing
                pass
            try:
                parsed = self._postprocess_email_clarification_actions(user_text=text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = self._postprocess_ambiguous_type_text_actions(user_text=text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = self._postprocess_missing_value_actions(user_text=text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = self._postprocess_file_action_clarification(user_text=text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = self._postprocess_research_clarification(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Generic clarification for under-specified requests (supports new intents).
            try:
                parsed = self._postprocess_generic_clarification(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Post-process: treat follow-ups as continuations; avoid reopening apps and replace text safely.
            try:
                parsed = self._postprocess_followup_edit_actions(user_text=text, context=context, parsed=parsed)
            except Exception:
                pass

            # Post-process: if the user clearly needs an online lookup but the model didn't emit web_search,
            # force a web_search so the backend can run the 2-pass web lookup pipeline.
            try:
                parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Post-process: enforce web lookup policy (avoid unnecessary web_search/fetch_url).
            try:
                parsed = self._postprocess_web_lookup_policy(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Post-process: normalize/auto-add open_url for common "open site" intents.
            try:
                parsed = self._postprocess_open_url_actions(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Minimal multi-step chaining for Jarvis-like execution (open -> search/play).
            try:
                parsed = self._postprocess_multi_step_chain_actions(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Auto web fallback: if the model is uncertain and the user asked an informational question,
            # trigger web_search so the 2-pass pipeline can answer with real sources.
            try:
                auto_web = bool(rd.AUTO_WEB_ON_UNCERTAINTY)
                if auto_web and isinstance(parsed, dict):
                    actions = parsed.get("actions")
                    if (not actions) and self._looks_uncertain(parsed.get("text", "")) and self._is_informational_question(text):
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                        parsed["source"] = parsed.get("source") or "auto-web-uncertainty"
            except Exception:
                pass

            # Post-process: for common PC settings requests, open the right Settings page safely.
            try:
                parsed = self._postprocess_pc_settings_actions(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Safety backstop: drop any actions that could modify OS/system files or run destructive commands.
            try:
                parsed = self._postprocess_system_safety(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Final pass: de-duplicate actions (prevents repeated open_app, etc.).
            try:
                parsed["actions"] = self._dedupe_actions(parsed.get("actions") or [])
            except Exception:
                pass

            # Assistant-style behavior: concise confirmation for important actions
            # and optional proactive follow-up suggestions when useful.
            try:
                parsed = self._postprocess_proactive_followup(text, parsed)
            except Exception:
                pass

            # Final guard: some later post-processors can reintroduce web actions.
            # Re-apply policy to keep simple informational questions fast.
            try:
                parsed = self._postprocess_web_lookup_policy(user_text=text, parsed=parsed)
            except Exception:
                pass

            # Error intelligence: when no actions are needed for debug/system reasoning,
            # ensure actionable cause/fix/next-step guidance from recent error signals.
            try:
                qtype = self._knowledge_query_type(text)
                no_actions = not isinstance(parsed.get("actions"), list) or not parsed.get("actions")
                if no_actions and qtype in {"debug", "system_behavior", "task_history"}:
                    hints = runtime_ctx.get("error_fix_hints") if isinstance(runtime_ctx, dict) else []
                    learning_hints = runtime_ctx.get("learning_hints") if isinstance(runtime_ctx, dict) else []
                    top_learning_fix = ""
                    if isinstance(learning_hints, list):
                        for h in learning_hints:
                            hs = str(h or "").strip()
                            if hs.lower().startswith("failure_fix_pattern:"):
                                top_learning_fix = hs
                                break
                    if isinstance(hints, list) and hints:
                        top = str(hints[0]).strip()
                        cause = top
                        fix = "Apply the smallest safe corrective step and retry once."
                        m = re.search(r"cause=([^|]+)", top, flags=re.IGNORECASE)
                        if m:
                            cause = m.group(1).strip()
                        m = re.search(r"fix=([^|]+)", top, flags=re.IGNORECASE)
                        if m:
                            fix = m.group(1).strip()
                        if top_learning_fix:
                            m_fix = re.search(r"fix=([^|]+)", top_learning_fix, flags=re.IGNORECASE)
                            if m_fix and str(m_fix.group(1) or "").strip():
                                fix = str(m_fix.group(1) or "").strip()
                        parsed["text"] = (
                            f"Cause: {cause}\n"
                            "Fix steps:\n"
                            f"1) {fix}\n"
                            "2) Re-run the failing command once to confirm.\n"
                            "3) Capture the first traceback/error line if it still fails.\n"
                            "Next step: Share that first failing line for a targeted patch."
                        )
                    elif top_learning_fix:
                        fix = "Apply validated failure-fix pattern and retry once."
                        m_fix = re.search(r"fix=([^|]+)", top_learning_fix, flags=re.IGNORECASE)
                        if m_fix and str(m_fix.group(1) or "").strip():
                            fix = str(m_fix.group(1) or "").strip()
                        parsed["text"] = (
                            "Cause: recurring runtime failure pattern detected from learning memory.\n"
                            "Fix steps:\n"
                            f"1) {fix}\n"
                            "2) Re-run the failing command once to verify recovery.\n"
                            "3) If it fails again, share the first error line for precise patching."
                        )
            except Exception:
                pass

            # Attach emotion hint for UI (optional).
            try:
                if "emotion" not in parsed:
                    parsed["emotion"] = self._infer_emotion(parsed.get("text") or text)
            except Exception:
                pass

            try:
                if "language" not in parsed and preferred_language_code:
                    parsed["language"] = preferred_language_code
            except Exception:
                pass

            latency = (datetime.now(timezone.utc) - start).total_seconds()
            parsed["latency"] = f"{latency:.2f}s"
            parsed["source"] = provider_source
            parsed["routing"] = {
                "provider": provider_source,
                "model": routed_model,
                "fallback_used": provider_source in {"groq"},
                "task_type": route_task_type,
                "deployment_profile": route_profile,
            }
            parsed["_planning_confidence"] = planning_confidence
            logger.info(
                "[llm.response] source=%s latency=%s fallback_used=%s",
                provider_source,
                parsed.get("latency"),
                str(provider_source in {"groq"}),
            )

            try:
                if self.learning_engine is not None:
                    self.learning_engine.record_model_performance(
                        task_type=str(route_task_type or self._knowledge_query_type(original_text) or "simple_chat"),
                        model_id=str(routed_model or chosen_model or "unknown"),
                        provider=str(provider_source or chosen_provider or "unknown"),
                        success=True,
                        latency_ms=float(max(0.0, latency * 1000.0)),
                        fallback_used=bool(provider_source in {"groq", "fallback"}),
                        error_kind=None,
                    )
            except Exception:
                pass

            try:
                self._cache_response(original_text, parsed)
            except Exception:
                pass

            try:
                self._recent_intents.append(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "intent": self._knowledge_query_type(original_text),
                        "task": route_task_type or "unknown",
                        "source": provider_source,
                    }
                )
                if len(self._recent_intents) > 12:
                    self._recent_intents = self._recent_intents[-12:]
            except Exception:
                pass

            self._learn_from_actions(original_text, parsed.get("actions") or [])

            print(f"[LLM] {parsed}")
            return _finalize(parsed)

        except Exception as e:
            print(f"[LLM ERROR] {e}")
            db.save_system_event("llm_error", str(e), "error")

            try:
                if self.learning_engine is not None:
                    self.learning_engine.record_model_performance(
                        task_type=str(self._knowledge_query_type(original_text) or "simple_chat"),
                        model_id=str((self._last_model_ops_route or {}).get("primary", {}).get("model_id") or self.primary_model or "unknown"),
                        provider=str((self._last_model_ops_route or {}).get("primary", {}).get("provider") or self.provider or "unknown"),
                        success=False,
                        latency_ms=0.0,
                        fallback_used=False,
                        error_kind=str(e.__class__.__name__ or "provider_error"),
                    )
            except Exception:
                pass

            # If the model call failed (often due to rate limits) but the request is clearly
            # time-sensitive / web-required, trigger a web lookup instead of returning a vague fallback.
            try:
                msg = str(e).lower()
                tl = (text or "").strip().lower()
                # Don't trigger web-search fallback for internal prompts used by the backend orchestration.
                if tl.startswith("you are ") or ("provided web context" in tl):
                    raise Exception("internal_prompt")

                if ("rate limit" in msg or "rate_limit" in msg) and self._should_use_web_lookup(text):
                    q = (text or "").strip()
                    # Keep query short and search-engine friendly.
                    q = re.sub(r"(?i)\byou\s+must\b[\s\S]*$", "", q).strip()
                    q = re.sub(
                        r"(?i)\b(look\s+(it\s+)?up|search)\b[\s\S]{0,24}?\b(online|on\s+the\s+internet|from\s+the\s+internet)\b",
                        "",
                        q,
                    ).strip()
                    q = re.sub(r"(?i)^(what\s+is|what\s+are|tell\s+me|give\s+me|find|search\s+for|look\s+up)\s+", "", q).strip()
                    q = re.sub(r"(?i)\b(and|please|kindly)\s*$", "", q).strip(" .-\t")
                    q = re.sub(r"\s+", " ", q).strip()
                    stop = {
                        "the","a","an","and","or","to","of","for","in","on","with","this","that","today","now","as","is","are","was","were",
                        "must","please","include","provide","sources","source","links","link","look","up","online","from","internet","summarize","summary",
                        "analyze","analysis","scenarios","scenario","bull","bear","base","current","trend","drivers","risks","assumptions",
                    }
                    toks = [t for t in re.findall(r"[a-z0-9]+", q.lower()) if t and t not in stop]
                    if toks:
                        q = " ".join(toks[:10]).strip()
                    if len(q) > 120:
                        q = " ".join(q.split()[:12]).strip()
                    if not q:
                        q = (text or "").strip()

                    return _finalize({
                        "text": "Looking it up online.",
                        "actions": [{"type": "web_search", "query": q, "num_results": 5}],
                        "source": "fallback-web",
                    })
            except Exception:
                pass

            # Cached-context fallback for short/simple prompts when providers fail.
            try:
                simple = bool(re.fullmatch(r"[\w\s?.!,:+\-/*]{1,180}", str(original_text or "").strip()))
                if simple:
                    cached = self._get_cached_response(original_text)
                    if isinstance(cached, dict):
                        return _finalize(cached)
            except Exception:
                pass

            # Reuse deterministic intent parsing when provider calls fail.
            # This keeps basic greetings, simple intents, and low-risk commands responsive
            # even when no external LLM is currently reachable.
            try:
                deterministic = self._preparse_deterministic_voice_actions(text)
                if isinstance(deterministic, dict):
                    out = {
                        "text": str(deterministic.get("text") or "Done."),
                        "actions": deterministic.get("actions") if isinstance(deterministic.get("actions"), list) else [],
                        "source": str(deterministic.get("source") or "fallback-local-deterministic"),
                    }
                    self._learn_from_actions(original_text, out.get("actions") or [])
                    return _finalize(out)
            except Exception:
                pass

            # Local deterministic fallback: if the model is unavailable, still try to
            # execute obvious low-risk intents (open_app/open_url/settings).
            try:
                parsed = {"text": "", "actions": []}
                try:
                    parsed = self._postprocess_open_url_actions(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_pc_settings_actions(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_write_actions(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_email_clarification_actions(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_ambiguous_type_text_actions(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_missing_value_actions(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_file_action_clarification(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_research_clarification(user_text=text, parsed=parsed)
                except Exception:
                    pass
                try:
                    parsed = self._postprocess_system_safety(user_text=text, parsed=parsed)
                except Exception:
                    pass

                actions = parsed.get("actions") or []
                if isinstance(actions, list) and actions:
                    first = actions[0] if isinstance(actions[0], dict) else {}
                    at = (first.get("type") or "").strip()
                    if at == "open_app":
                        name = str(first.get("app_name") or "").strip() or "the app"
                        parsed["text"] = f"Opening {name}."
                    elif at == "open_url":
                        parsed["text"] = "Opening it."
                    elif at == "web_search":
                        parsed["text"] = "Looking it up online."
                    else:
                        parsed["text"] = "Done."
                    parsed["source"] = "fallback-local"
                    self._learn_from_actions(original_text, parsed.get("actions") or [])
                    return _finalize(parsed)
            except Exception:
                pass

            # Human-readable fallback for basic chat cases when provider is unavailable.
            try:
                tl = (text or "").strip().lower()
                if re.match(r"^(hi|hello|hey|howdy|greetings|good\s+(morning|afternoon|evening|day))(\s+jarvis)?([\s!?.]*)$", tl):
                    return _finalize({
                        "text": "Hey! I am online. I can help with chat, research prompts, and connected PC actions.",
                        "actions": [],
                        "source": "fallback-local-chat",
                    })

                m = re.match(r"^(what\s+is\s+)?(-?\d+)\s*([+\-*/])\s*(-?\d+)\??$", tl)
                if m:
                    a = int(m.group(2))
                    op = m.group(3)
                    b = int(m.group(4))
                    if op == "+":
                        ans = a + b
                    elif op == "-":
                        ans = a - b
                    elif op == "*":
                        ans = a * b
                    else:
                        ans = "undefined" if b == 0 else (a / b)
                    return _finalize({
                        "text": f"{a} {op} {b} = {ans}",
                        "actions": [],
                        "source": "fallback-local-chat",
                    })

                if "what can you do" in tl or "capabilit" in tl:
                    return _finalize({
                        "text": "I can chat, answer questions, run research prompts, and trigger connected PC actions like opening apps, URLs, screenshots, and automation tasks when permissions are granted.",
                        "actions": [],
                        "source": "fallback-local-chat",
                    })

                if tl.startswith("summarize") or tl.startswith("summarise"):
                    return _finalize({
                        "text": "Here is a concise summary.",
                        "actions": [],
                        "source": "fallback-local-chat",
                    })

                if tl.startswith("compare ") or " compare " in tl:
                    return _finalize({
                        "text": "Quick comparison: option A usually offers stronger performance control and lower-level tuning, while option B typically offers faster development and simpler operations. If you share your exact use case, I can tailor the recommendation.",
                        "actions": [],
                        "source": "fallback-local-chat",
                    })
            except Exception:
                pass

            # Fallback humanlike reply
            # IMPORTANT: This is returned when the model call/parsing fails.
            # Avoid "thinking..." style filler that looks like a pending response.
            now = time.time()
            include_provider_notice = not self._has_local_fallback_capability(text)
            if (now - float(self._last_provider_notice_at or 0.0)) < 90.0:
                return _finalize({
                    "text": self._build_actionable_fallback_text(text, include_provider_notice=include_provider_notice),
                    "actions": [],
                    "source": "fallback",
                    "task_status_hint": "provider_unavailable",
                })
            self._last_provider_notice_at = now
            return _finalize({
                "text": self._build_actionable_fallback_text(text, include_provider_notice=include_provider_notice),
                "actions": [],
                "source": "fallback",
                "task_status_hint": "provider_unavailable",
            })

    def _postprocess_multi_step_chain_actions(self, user_text: str, parsed: dict) -> dict:
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        # Reuse previously successful chained plans from local learning cache when available.
        try:
            cached = self._get_cached_response(user_text, max_age_s=86400)
            cached_actions = (cached or {}).get("actions") if isinstance(cached, dict) else []
            if (not actions) and isinstance(cached_actions, list) and cached_actions:
                has_chain = any(isinstance(a, dict) and (a.get("depends_on") or a.get("step_id")) for a in cached_actions)
                if has_chain:
                    actions = [dict(a) for a in cached_actions if isinstance(a, dict)]
        except Exception:
            pass

        t = str(user_text or "").strip().lower()
        if not t:
            parsed["actions"] = actions
            return parsed

        if self._detect_compound_intent(user_text):
            steps = self._split_compound_intent_steps(user_text)
            chain_actions: list[dict] = []
            active_app = ""

            for step in steps:
                step_parsed = {"text": "", "actions": []}
                try:
                    step_parsed = self._postprocess_open_url_actions(user_text=step, parsed=step_parsed)
                except Exception:
                    pass
                try:
                    step_parsed = self._postprocess_pc_settings_actions(user_text=step, parsed=step_parsed)
                except Exception:
                    pass
                try:
                    step_parsed = self._postprocess_write_actions(user_text=step, parsed=step_parsed)
                except Exception:
                    pass
                try:
                    step_parsed = self._postprocess_ambiguous_type_text_actions(user_text=step, parsed=step_parsed)
                except Exception:
                    pass

                step_actions = step_parsed.get("actions") if isinstance(step_parsed.get("actions"), list) else []

                if not step_actions:
                    st = str(step or "").strip()
                    stl = st.lower()
                    m_open = re.search(r"\b(?:open|launch|start)\s+(.+)$", st, flags=re.IGNORECASE)
                    m_switch = re.search(r"\b(?:switch\s+to|switch|focus|go\s+to)\s+(.+)$", st, flags=re.IGNORECASE)
                    m_close = re.search(r"\b(?:close|quit|exit)\s+(.+)$", st, flags=re.IGNORECASE)
                    m_search = re.search(r"\b(?:search(?:\s+for)?|look\s+up|find)\s+(.+)$", st, flags=re.IGNORECASE)
                    m_type = re.search(r"\b(?:type|write|enter)\s+(.+)$", st, flags=re.IGNORECASE)

                    if m_open:
                        target = str(m_open.group(1) or "").strip(" .,!?")
                        app_name = self._maybe_map_local_app_name(target) or ""
                        if app_name:
                            step_actions = [{"type": "open_app", "app_name": app_name}]
                        elif target:
                            if re.match(r"^https?://", target, flags=re.IGNORECASE):
                                step_actions = [{"type": "open_url", "url": target}]
                            else:
                                host = re.sub(r"\s+", "", target)
                                if "." in host and re.search(r"[a-z0-9]", host):
                                    step_actions = [{"type": "open_url", "url": f"https://{host}"}]
                    elif m_switch:
                        target = str(m_switch.group(1) or "").strip(" .,!?")
                        app_name = self._maybe_map_local_app_name(target) or target
                        if app_name:
                            step_actions = [{"type": "switch_app", "app_name": app_name}]
                    elif m_close:
                        target = str(m_close.group(1) or "").strip(" .,!?")
                        app_name = self._maybe_map_local_app_name(target) or target
                        if app_name:
                            step_actions = [{"type": "close_app", "app_name": app_name}]
                    elif m_search:
                        query = str(m_search.group(1) or "").strip(" .,!?")
                        if query:
                            if active_app:
                                step_actions = [{"type": "type_text", "text": query}, {"type": "press_key", "key": "enter", "presses": 1}]
                            else:
                                step_actions = [{"type": "web_search", "query": query, "num_results": 5}]
                    elif m_type:
                        content = str(m_type.group(1) or "").strip(" \"'“”")
                        if content:
                            step_actions = [{"type": "type_text", "text": content}]
                            if re.search(r"\b(?:send|submit|search|enter)\b", stl):
                                step_actions.append({"type": "press_key", "key": "enter", "presses": 1})

                for a in step_actions:
                    if not isinstance(a, dict):
                        continue
                    at = str(a.get("type") or "").strip().lower()
                    if at in {"open_app", "switch_app"}:
                        active_app = str(a.get("app_name") or active_app).strip().lower()
                    elif at == "open_url":
                        active_app = "browser"
                    chain_actions.append(dict(a))

            compact_actions: list[dict] = []
            last_open_or_switch = ""
            for a in chain_actions:
                at = str(a.get("type") or "").strip().lower()
                if at in {"open_app", "switch_app"}:
                    app = str(a.get("app_name") or "").strip().lower()
                    tag = f"{at}:{app}"
                    if app and tag == last_open_or_switch:
                        continue
                    last_open_or_switch = tag
                compact_actions.append(a)

            if compact_actions:
                actions = compact_actions
                parsed["text"] = self._build_compound_response_text(actions, parsed.get("text") or "")

        # Annotate chain dependencies for sequential execution.
        for idx, a in enumerate(actions):
            if not isinstance(a, dict):
                continue
            if not a.get("step_id"):
                a["step_id"] = f"step_{idx + 1}"
            if idx > 0 and not a.get("depends_on"):
                a["depends_on"] = f"step_{idx}"

        parsed["actions"] = actions
        return parsed

    @staticmethod
    def _postprocess_open_url_actions(user_text: str, parsed: dict) -> dict:
        """Normalize open_url actions and add them for common website intents.

        Goals:
        - Convert legacy open_url {url_name:"youtube"} -> {url:"https://www.youtube.com"}
        - If user says "open/visit/go to <site>" and the model returned no actions,
          emit open_url for known sites, else web_search as a safe fallback.
        """
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        _maybe_map_local_app_name = LLMAdapter._maybe_map_local_app_name

        site_map = {
            "youtube": "https://www.youtube.com",
            "linkedin": "https://www.linkedin.com",
            "google": "https://www.google.com",
            "github": "https://www.github.com",
            "facebook": "https://www.facebook.com",
            "twitter": "https://www.twitter.com",
            "instagram": "https://www.instagram.com",
            "reddit": "https://www.reddit.com",
            "stack overflow": "https://stackoverflow.com",
            "stackoverflow": "https://stackoverflow.com",
            "wikipedia": "https://www.wikipedia.org",
            "gmail": "https://mail.google.com",
            "weather": "https://weather.com",
            "chatgpt": "https://chatgpt.com",
            "openai": "https://openai.com",
            "netflix": "https://www.netflix.com",
            "amazon": "https://www.amazon.com",
            "bing": "https://www.bing.com",
            "duckduckgo": "https://duckduckgo.com",
            "spotify": "https://www.spotify.com",
            "microsoft": "https://www.microsoft.com",
            # Communication / work
            "whatsapp": "https://web.whatsapp.com",
            "whatsapp web": "https://web.whatsapp.com",
            "teams": "https://teams.microsoft.com",
            "microsoft teams": "https://teams.microsoft.com",
            "slack": "https://slack.com",
            "discord": "https://discord.com/app",
            "zoom": "https://zoom.us",
            "telegram": "https://web.telegram.org",
            "outlook": "https://outlook.office.com/mail/",
            "office": "https://www.office.com",
            "onedrive": "https://onedrive.live.com",

            # Google apps
            "drive": "https://drive.google.com",
            "google drive": "https://drive.google.com",
            "docs": "https://docs.google.com",
            "google docs": "https://docs.google.com",
            "sheets": "https://sheets.google.com",
            "google sheets": "https://sheets.google.com",
            "calendar": "https://calendar.google.com",
            "google calendar": "https://calendar.google.com",
            "maps": "https://maps.google.com",
            "google maps": "https://maps.google.com",

            # Productivity
            "notion": "https://www.notion.so",
            "trello": "https://trello.com",
            "jira": "https://www.atlassian.com/software/jira",
            "confluence": "https://www.atlassian.com/software/confluence",

            # Developer tools
            "gitlab": "https://gitlab.com",
            "bitbucket": "https://bitbucket.org",
            "npm": "https://www.npmjs.com",
            "pypi": "https://pypi.org",
        }

        # High-confidence messaging intent: WhatsApp send message to a number.
        # Emit device_action wrappers so execution is routed to the PC agent
        # (instead of local UI-only open_url handling in the frontend).
        try:
            raw_text = (user_text or "").strip()
            tl_full = raw_text.lower()
            wants_whatsapp = bool(re.search(r"\b(watsapp|whatsapp)\b", tl_full))
            wants_send = bool(re.search(r"\b(send|message|msg)\b", tl_full))
            if wants_whatsapp and wants_send:
                phone_match = re.search(r"(?<!\w)(\+?\d[\d\s\-]{7,18}\d)(?!\w)", raw_text)
                msg_match = re.search(
                    r"\b(?:l?message|msg)\s*(?:is|:)?\s*[\"\u201c\u201d']?(.+?)[\"\u201c\u201d']?\s*$",
                    raw_text,
                    flags=re.IGNORECASE,
                )

                phone_raw = (phone_match.group(1).strip() if phone_match else "")
                phone_digits = re.sub(r"[^0-9+]", "", phone_raw)
                if phone_digits.startswith("+"):
                    phone_digits = phone_digits[1:]

                message_text = (msg_match.group(1).strip() if msg_match else "")
                message_text = re.sub(r"\s+", " ", message_text).strip()

                wa_url = "https://web.whatsapp.com"
                if phone_digits:
                    if message_text:
                        try:
                            from urllib.parse import quote_plus

                            encoded_msg = quote_plus(message_text)
                        except Exception:
                            encoded_msg = re.sub(r"\s+", "+", message_text)
                        wa_url = f"https://web.whatsapp.com/send?phone={phone_digits}&text={encoded_msg}"
                    else:
                        wa_url = f"https://web.whatsapp.com/send?phone={phone_digits}"

                out_actions = [{"type": "device_action", "action": "open_url", "url": wa_url}]

                # Only auto-press Enter when the user explicitly asked to send and provided message text.
                if message_text and re.search(r"\bsend\b", tl_full):
                    out_actions.append({
                        "type": "device_action",
                        "action": "press_key",
                        "key": "enter",
                        "presses": 1,
                        "before_ms": 2200,
                    })

                parsed["actions"] = out_actions
                parsed["text"] = "Opening WhatsApp and sending your message."
                return parsed
        except Exception:
            pass

        # If the user said "open/visit/go to ...", capture the target phrase.
        # We'll use this for safer search fallbacks (instead of trusting guessed URLs).
        user_target = ""
        try:
            tl = (user_text or "").strip().lower()
            m = re.search(r"\b(?:open|visit|go\s+to|browse|navigate\s+to)\b\s+(.+)$", tl)
            user_target = (m.group(1).strip() if m else "")
            user_target = re.sub(r"[\.!?]+$", "", user_target).strip()
        except Exception:
            user_target = ""

        # Extract any explicit URL/domain the user typed, so we can trust it.
        user_urls: set[str] = set()
        user_domains: set[str] = set()
        try:
            from urllib.parse import urlparse

            raw = (user_text or "").strip()
            if raw:
                for u in re.findall(r"\bhttps?://[^\s]+\b", raw, flags=re.IGNORECASE):
                    u2 = u.strip().rstrip(").,;\"'>")
                    user_urls.add(u2)
                    try:
                        p = urlparse(u2)
                        if p.netloc:
                            user_domains.add(p.netloc.lower())
                    except Exception:
                        pass
                for u in re.findall(r"\bwww\.[^\s]+\b", raw, flags=re.IGNORECASE):
                    u2 = ("https://" + u).strip().rstrip(").,;\"'>")
                    user_urls.add(u2)
                    try:
                        p = urlparse(u2)
                        if p.netloc:
                            user_domains.add(p.netloc.lower())
                    except Exception:
                        pass
                # Domain-like tokens (fallback). Keep it conservative.
                for d in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9\-\.]+\.[A-Za-z]{2,}\b", raw):
                    d2 = d.strip().rstrip(").,;\"'>").lower()
                    if d2 and len(d2) <= 255:
                        user_domains.add(d2)
        except Exception:
            pass

        # 1) Normalize existing open_url actions.
        normalized = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            if a.get("type") != "open_url":
                normalized.append(a)
                continue

            url = str(a.get("url") or "").strip()
            url_name = str(a.get("url_name") or "").strip().lower()

            # If the model used open_url for a local app intent (e.g., "notepad"), convert to open_app.
            local_app = _maybe_map_local_app_name(url_name)
            if (not url) and local_app:
                normalized.append({"type": "open_app", "app_name": local_app, "args": []})
                continue

            if not url and url_name:
                mapped = site_map.get(url_name)
                if not mapped and url_name:
                    # Accept a domain-like name or fallback to https://www.<name>.com
                    if "." in url_name:
                        mapped = f"https://{url_name}"
                    else:
                        mapped = f"https://www.{url_name}.com"
                url = mapped or ""

            if url:
                # Safety: do not trust invented URLs unless the user explicitly provided the URL/domain
                # or it's a known mapping.
                try:
                    from urllib.parse import urlparse

                    check_url = url
                    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", check_url):
                        check_url = "https://" + check_url
                    p = urlparse(check_url)
                    netloc = (p.netloc or "").lower()
                except Exception:
                    netloc = ""

                is_known = url in set(site_map.values())
                is_user_provided = (url in user_urls) or (netloc in user_domains)
                if is_known or is_user_provided:
                    normalized.append({"type": "open_url", "url": url})
                else:
                    # Replace with a search page so we still follow "open X" but avoid wrong sites.
                    query_text = (user_target or url_name or url).strip()
                    try:
                        from urllib.parse import quote_plus
                        q = quote_plus(query_text)
                    except Exception:
                        q = re.sub(r"\s+", "+", query_text).strip("+")
                    normalized.append({"type": "open_url", "url": f"https://www.google.com/search?q={q}"})

        actions = normalized

        # 2) If no actions and user intent is clearly "open a website", add best-effort action.
        if actions:
            parsed["actions"] = actions
            return parsed

        t = (user_text or "").strip().lower()
        if not t:
            parsed["actions"] = actions
            return parsed

        # Avoid stepping on Settings/app intents.
        if "ms-settings" in t or re.search(r"\b(settings|display|bluetooth|wi-?fi|wifi|network)\b", t):
            parsed["actions"] = actions
            return parsed

        wants_web_open = bool(re.search(r"\b(open|visit|go\s+to|browse|navigate)\b", t))
        if not wants_web_open:
            parsed["actions"] = actions
            return parsed

        # Extract a coarse "target" after the open verb.
        m = re.search(r"\b(?:open|visit|go\s+to|browse|navigate\s+to)\b\s+(.+)$", t)
        target = (m.group(1).strip() if m else "")
        target = re.sub(r"[\.!?]+$", "", target).strip()

        # If the target is clearly a local app name (e.g., "notepad"), emit open_app.
        local_app = _maybe_map_local_app_name(target)
        if local_app:
            parsed["actions"] = [{"type": "open_app", "app_name": local_app, "args": []}]
            # open_url postprocessing runs after write postprocessing; re-run write postprocessing
            # so "open notepad and type ..." reliably emits type_text.
            try:
                parsed = LLMAdapter._postprocess_write_actions(user_text=user_text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_email_clarification_actions(user_text=user_text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_ambiguous_type_text_actions(user_text=user_text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_missing_value_actions(user_text=user_text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_file_action_clarification(user_text=user_text, parsed=parsed)
            except Exception:
                pass
            try:
                parsed = LLMAdapter._postprocess_research_clarification(user_text=user_text, parsed=parsed)
            except Exception:
                pass
            return parsed

        if not target:
            parsed["actions"] = actions
            return parsed

        # If user provided a full URL, use it directly.
        if re.match(r"^(https?://|www\.)", target):
            url = target if target.startswith("http") else f"https://{target}"
            parsed["actions"] = [{"type": "open_url", "url": url}]
            return parsed

        # Try direct mapping for known sites (including multiword keys like 'stack overflow').
        mapped = site_map.get(target)
        if not mapped:
            # Try matching by inclusion (e.g., 'open github.com')
            for k, v in site_map.items():
                if k in target:
                    mapped = v
                    break

        if mapped:
            parsed["actions"] = [{"type": "open_url", "url": mapped}]
            return parsed

        # Unknown site: still follow the user's instruction ("open X") without back-and-forth.
        # Open a search results page directly so the user lands at a relevant result.
        try:
            from urllib.parse import quote_plus
            q = quote_plus(target)
        except Exception:
            q = re.sub(r"\s+", "+", target).strip("+")

        parsed["actions"] = [{"type": "open_url", "url": f"https://www.google.com/search?q={q}"}]
        return parsed

    @staticmethod
    def _postprocess_write_actions(user_text: str, parsed: dict) -> dict:
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        # Detect "open app" intent.
        open_app = None
        for a in actions:
            if isinstance(a, dict) and a.get("type") == "open_app":
                open_app = a
                break
        if not open_app:
            parsed["actions"] = actions
            return parsed

        has_type_text = any(isinstance(a, dict) and a.get("type") == "type_text" for a in actions)
        if has_type_text:
            parsed["actions"] = actions
            return parsed

        t = (user_text or "").strip()
        t_lower = t.lower()

        # Only auto-add typing when the user explicitly asked to write/type/draft/compose.
        wants_writing = bool(re.search(r"\b(write|type|draft|compose|create|make)\b", t_lower))
        if not wants_writing:
            parsed["actions"] = actions
            return parsed

        # If email intent is missing key details, ask a single clarifying question
        # and avoid typing a vague placeholder into the editor.
        if LLMAdapter._email_intent_needs_details(t_lower):
            base_text = (parsed.get("text") or "").strip()
            question = "What should the email be about, and who should it go to? If it’s HR, which company and which role?"
            parsed["text"] = question if not base_text else (base_text + "\n\n" + question)
            parsed["actions"] = actions
            return parsed

        # Only do this for simple text editors (typing into UI makes sense).
        app_name = str(open_app.get("app_name") or "").strip().lower()
        is_text_editor = any(k in app_name for k in ("notepad", "wordpad", "textedit", "word", "winword"))
        if not is_text_editor:
            parsed["actions"] = actions
            return parsed

        draft = LLMAdapter._build_reasonable_draft(t)
        if not draft:
            parsed["actions"] = actions
            return parsed

        # Show the draft to the user AND type it into the opened app.
        # Keep interval conservative to reduce risk of missed keystrokes (especially for long drafts).
        before_ms = 650
        try:
            app_for_delay = str(open_app.get("app_name") or "").strip().lower()
            if any(k in app_for_delay for k in ("word", "winword")):
                before_ms = 1800
            if len(draft) > 500:
                before_ms = max(before_ms, 2300)
        except Exception:
            before_ms = 650

        actions.append({
            "type": "type_text",
            "text": draft,
            "interval": 0.05,
            # Give the OS time to focus the newly opened window before typing.
            "before_ms": int(before_ms),
            # Mark deterministic drafts so ambiguity filters don't remove them.
            "auto_generated": True,
        })
        parsed["actions"] = actions

        # Ensure the user-facing text includes the draft (so it is visible even if typing fails).
        base_text = (parsed.get("text") or "").strip()
        if draft.strip() not in base_text:
            parsed["text"] = (base_text + "\n\n" + draft).strip() if base_text else draft

        return parsed

    @staticmethod
    def _email_intent_needs_details(tl: str) -> bool:
        if not re.search(r"\b(email|mail)\b", tl):
            return False
        if not re.search(r"\b(write|draft|compose|create|make|send)\b", tl):
            return False

        # If the user already provided quoted body text, assume sufficient detail.
        if re.search(r"[\"\u201c\u201d][^\"\u201c\u201d]{8,}[\"\u201c\u201d]", tl):
            return False

        has_recipient = bool(
            re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", tl)
            or re.search(
                r"\b(?:to|for)\b(?:\s+[a-z0-9&._-]+){0,6}\s+"
                r"(?:hr|human\s+resources|recruiter|hiring\s+manager|team|manager|boss)\b",
                tl,
            )
        )

        has_purpose = bool(
            re.search(r"\b(about|regarding|re:|subject)\b", tl)
            or re.search(
                r"\b(apply|application|interview|leave|meeting|proposal|complaint|follow\s*up|thank\s*you|"
                r"introduction|resume|cv|offer|invoice|payment|support|bug|issue|schedule|reschedule|"
                r"appointment|statement|quotation|quote|estimate|project|partnership|collaboration|"
                r"job|position|role|feedback)\b",
                tl,
            )
        )

        # Treat common voice phrasing as sufficiently specific for a usable first draft.
        has_style_or_business_context = bool(
            re.search(r"\b(formal|professional|official)\b", tl)
            or re.search(r"\b(it\s+company|company|corporate|hr\s+team)\b", tl)
        )

        # If recipient is known, generate a usable first draft directly.
        if has_recipient:
            return False

        return not (has_recipient and (has_purpose or has_style_or_business_context))

    @staticmethod
    def _postprocess_email_clarification_actions(user_text: str, parsed: dict) -> dict:
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        tl = (user_text or "").strip().lower()
        if not tl:
            parsed["actions"] = actions
            return parsed

        if not LLMAdapter._email_intent_needs_details(tl):
            parsed["actions"] = actions
            return parsed

        # Remove email generation/typing actions and ask for missing details.
        filtered = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            at = str(a.get("type") or "").strip().lower()
            if at in {"generate_email", "type_text"}:
                continue
            filtered.append(a)

        base_text = (parsed.get("text") or "").strip()
        question = "What should the email be about, and who should it go to? If it’s HR, which company and which role?"
        if not base_text:
            parsed["text"] = question
        elif question.lower() in base_text.lower():
            parsed["text"] = base_text
        else:
            parsed["text"] = base_text + "\n\n" + question
        parsed["actions"] = filtered
        parsed["clarification"] = {
            "kind": "email",
            "question": question,
            "original_user_text": user_text,
        }
        return parsed

    @staticmethod
    def _postprocess_missing_value_actions(user_text: str, parsed: dict) -> dict:
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        if not actions:
            parsed["actions"] = actions
            return parsed

        tl = (user_text or "").strip().lower()
        if not tl:
            parsed["actions"] = actions
            return parsed

        def _ask(question: str, filtered_actions: list[dict]) -> dict:
            base_text = (parsed.get("text") or "").strip()
            parsed["text"] = question if not base_text else (base_text + "\n\n" + question)
            parsed["actions"] = filtered_actions
            parsed["clarification"] = {
                "kind": "device_action",
                "question": question,
                "original_user_text": user_text,
            }
            return parsed

        filtered: list[dict] = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            at = str(a.get("type") or "").strip().lower()
            if at != "device_action":
                filtered.append(a)
                continue

            name = str(a.get("name") or "").strip().lower()
            args = a.get("args") if isinstance(a.get("args"), dict) else {}

            if name in {"set_brightness", "adjust_brightness", "set_volume", "adjust_volume"}:
                if "value" not in args:
                    return _ask("What level should I set it to (0–100)?", filtered)
            if name in {"set_power_plan", "set_energy_saver"}:
                if "plan" not in args and "enabled" not in args:
                    return _ask("Should I turn it on or off?", filtered)

            filtered.append(a)

        parsed["actions"] = filtered
        return parsed

    @staticmethod
    def _postprocess_file_action_clarification(user_text: str, parsed: dict) -> dict:
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        if not actions:
            parsed["actions"] = actions
            return parsed

        tl = (user_text or "").strip().lower()
        if not tl:
            parsed["actions"] = actions
            return parsed

        file_types = {"read", "list", "mkdir", "write", "edit", "delete", "move", "copy"}

        def _ask(question: str, filtered_actions: list[dict]) -> dict:
            base_text = (parsed.get("text") or "").strip()
            parsed["text"] = question if not base_text else (base_text + "\n\n" + question)
            parsed["actions"] = filtered_actions
            parsed["clarification"] = {
                "kind": "file_action",
                "question": question,
                "original_user_text": user_text,
            }
            return parsed

        filtered: list[dict] = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            at = str(a.get("type") or "").strip().lower()
            if at not in file_types:
                filtered.append(a)
                continue

            path = str(a.get("path") or "").strip()
            if at == "copy":
                src = str(a.get("source") or "").strip()
                dest = str(a.get("destination") or "").strip()
                if not src or not dest:
                    return _ask("Which file should I copy, and what is the destination path?", filtered)
            elif at == "move":
                dest = str(a.get("dest") or "").strip()
                if not path or not dest:
                    return _ask("Which file should I move, and what is the destination path?", filtered)
            else:
                if not path:
                    if at == "list":
                        return _ask("Which folder should I list?", filtered)
                    if at == "mkdir":
                        return _ask("What folder path should I create?", filtered)
                    if at in {"read", "write", "edit", "delete"}:
                        return _ask("Which file path should I use?", filtered)

            filtered.append(a)

        parsed["actions"] = filtered
        return parsed

    @staticmethod
    def _postprocess_research_clarification(user_text: str, parsed: dict) -> dict:
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        if not actions:
            parsed["actions"] = actions
            return parsed

        tl = (user_text or "").strip().lower()
        if not tl:
            parsed["actions"] = actions
            return parsed

        vague_queries = {"research", "market research", "market", "analysis", "summary"}

        def _terms(s: str) -> set[str]:
            toks = re.findall(r"[a-z0-9]+", (s or "").lower())
            stop = {
                "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "this", "that",
                "please", "research", "analysis", "summary", "find", "search", "look", "up", "online",
            }
            return {t for t in toks if t and t not in stop and len(t) >= 3}

        def _ask(question: str, filtered_actions: list[dict]) -> dict:
            base_text = (parsed.get("text") or "").strip()
            parsed["text"] = question if not base_text else (base_text + "\n\n" + question)
            parsed["actions"] = filtered_actions
            parsed["clarification"] = {
                "kind": "research",
                "question": question,
                "original_user_text": user_text,
            }
            return parsed

        filtered: list[dict] = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            at = str(a.get("type") or "").strip().lower()
            if at not in {"web_search", "fetch_url"}:
                filtered.append(a)
                continue

            query = ""
            if at == "web_search":
                query = str(a.get("query") or "").strip().lower()
            if at == "fetch_url":
                query = str(a.get("url") or "").strip().lower()

            if not query or query in vague_queries or len(re.findall(r"[a-z0-9]+", query)) < 3:
                if re.search(r"\b(research|market research|analysis|summary|report)\b", tl):
                    return _ask("What topic should I research, and which region or time range?", filtered)

            # Avoid low-signal web searches that are likely unrelated to user intent.
            if at == "web_search":
                q_terms = _terms(query)
                u_terms = _terms(tl)
                if q_terms and u_terms and len(q_terms.intersection(u_terms)) == 0:
                    return _ask(
                        "I need one specific topic before I search. What exact subject should I focus on?",
                        filtered,
                    )

            filtered.append(a)

        parsed["actions"] = filtered
        return parsed

    @staticmethod
    def _postprocess_generic_clarification(user_text: str, parsed: dict) -> dict:
        """Ask one clarifying question when the request is under-specified.

        This is intentionally intent-agnostic so the assistant can support new
        intents without hard-coding a small list.

        Triggers only when:
        - no actions are proposed, AND
        - we don't already have a more specific clarification, AND
        - the user text looks vague / underspecified.
        """
        try:
            if not isinstance(parsed, dict):
                return parsed

            # If we already asked something specific, don't override.
            if isinstance(parsed.get("clarification"), dict):
                return parsed

            actions = parsed.get("actions") or []
            if isinstance(actions, list) and actions:
                return parsed

            t = (user_text or "").strip()
            tl = t.lower()
            if not tl:
                return parsed

            # Avoid triggering on normal informational questions.
            if re.search(r"\b(what is|define|meaning of|explain)\b", tl):
                return parsed

            # Common vague phrases (especially from voice).
            vague = bool(
                re.fullmatch(r"(do it|do this|do that|same as before|like before|continue|go ahead)\.?", tl)
                or re.search(r"\b(improve|make it better|make better|optimi[sz]e|enhance|fix it|update it)\b", tl)
                or re.search(r"\b(improve yourself|better version|be better|give me proper results)\b", tl)
            )

            # If it's not obviously vague and has decent token content, let it be.
            if not vague:
                tok_count = len(re.findall(r"[a-z0-9]+", tl))
                if tok_count >= 10:
                    return parsed

            out_text = str(parsed.get("text") or "").strip()
            # If we already produced a long, concrete response, don't interrupt.
            if out_text and len(out_text) > 240 and not out_text.endswith("?"):
                return parsed

            if vague:
                if re.search(r"\b(email|mail|outlook)\b", tl):
                    question = "Do you want me to write the email or open Outlook?"
                elif re.search(r"\b(open|run|execute|start|launch)\b", tl):
                    question = "Do you want me to run this now, or just generate the steps?"
                else:
                    question = (
                        "To make sure I do exactly what you want: what is the specific outcome you want, "
                        "and are there any constraints (time range/region/output format)?"
                    )
                parsed["text"] = question if not out_text else (out_text + "\n\n" + question)
                parsed["actions"] = []
                parsed["clarification"] = {
                    "kind": "generic",
                    "question": question,
                    "original_user_text": user_text,
                }
            return parsed
        except Exception:
            return parsed

    @staticmethod
    def _postprocess_ambiguous_type_text_actions(user_text: str, parsed: dict) -> dict:
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        if not actions:
            parsed["actions"] = actions
            return parsed

        t = (user_text or "").strip()
        tl = t.lower()
        if not tl:
            parsed["actions"] = actions
            return parsed

        # If user provided quoted content, assume they gave explicit text.
        if re.search(r"[\"\u201c\u201d][^\"\u201c\u201d]{3,}[\"\u201c\u201d]", t):
            parsed["actions"] = actions
            return parsed

        # If user clearly specified a short literal after type/write, allow it.
        literal_match = re.search(r"\b(type|write)\b\s+(.+)$", tl)
        if literal_match:
            remainder = re.sub(r"[\.!?]+$", "", literal_match.group(2)).strip()
            words = re.findall(r"[a-z0-9']+", remainder)
            if 1 <= len(words) <= 3 and not re.search(r"\b(email|report|summary|essay|letter|proposal|resume|cv)\b", remainder):
                parsed["actions"] = actions
                return parsed

        # Heuristic: if type_text is just echoing the instruction, ask for details.
        stop = {"a","an","the","to","for","of","and","or","please","jarvis","hey","write","type","draft","compose","create","make"}

        def _word_set(s: str) -> set[str]:
            return {w for w in re.findall(r"[a-z0-9']+", s.lower()) if w and w not in stop}

        user_words = _word_set(tl)

        filtered: list[dict] = []
        for a in actions:
            if not isinstance(a, dict):
                continue
            at = str(a.get("type") or "").strip().lower()
            if at != "type_text":
                filtered.append(a)
                continue

            # Keep deterministic drafts that were explicitly generated by the assistant.
            if bool(a.get("auto_generated")):
                filtered.append(a)
                continue

            text = str(a.get("text") or "").strip()
            if not text:
                continue

            action_words = _word_set(text)
            overlap = (len(action_words & user_words) / max(1, len(user_words))) if user_words else 0

            looks_instructional = bool(
                re.search(r"\b(email|report|summary|proposal|letter|resume|cv|application)\b", text.lower())
                or re.search(r"\b(write|draft|compose|create|make)\b", text.lower())
            )

            if looks_instructional and (overlap >= 0.6 or len(text) <= 60):
                return {
                    **parsed,
                    "text": ((parsed.get("text") or "").strip() + "\n\n" if parsed.get("text") else "")
                    + "What should I write exactly? You can dictate the content or share key bullet points.",
                    "actions": filtered,
                }

            filtered.append(a)

        parsed["actions"] = filtered
        return parsed

    @staticmethod
    def _dedupe_actions(actions: list) -> list:
        """Remove obvious duplicates while preserving order.

        Primary goal: prevent repeated open_app (e.g., notepad twice) when postprocessors
        convert/augment actions.
        """
        if not isinstance(actions, list) or not actions:
            return [] if actions is None else (actions if isinstance(actions, list) else [])

        out: list[dict] = []
        seen: set[tuple] = set()

        for a in actions:
            if not isinstance(a, dict):
                continue
            t = str(a.get("type") or "").strip().lower()
            if not t:
                continue

            # Build a conservative de-dupe key.
            if t in {"open_app", "close_app", "switch_app"}:
                key = (t, str(a.get("app_name") or a.get("app") or "").strip().lower())
            elif t == "open_url":
                key = (t, str(a.get("url") or a.get("value") or "").strip().lower())
            elif t == "execute_command":
                key = (t, str(a.get("command") or "").strip().lower())
            else:
                # Do not over-dedupe editing actions; keep them.
                key = None

            if key is not None:
                if key in seen:
                    continue
                seen.add(key)

            out.append(a)

        return out

    def _load_skills(self) -> list[dict]:
        """Load skills from MongoDB (preferred) or data/skills.json (cached)."""
        try:
            skills = self._get_skills_catalog()
            if skills:
                return skills
        except Exception:
            pass
        try:
            root = Path(__file__).resolve().parents[2]
            skills_path = root / "data" / "skills.json"
            if not skills_path.exists():
                return []
            mtime = skills_path.stat().st_mtime
            if self._skills_cache is not None and mtime <= self._skills_cache_mtime:
                return self._skills_cache

            raw = skills_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            skills = [s for s in (data or []) if isinstance(s, dict) and s.get("enabled", True)]
            self._skills_cache = skills
            self._skills_cache_mtime = mtime
            return skills
        except Exception:
            return []

    def _skills_prompt_block(self) -> str:
        skills = self._load_skills()
        if not skills:
            return ""
        lines = ["Available skills (call via n8n_webhook):"]
        for s in skills[:12]:
            name = str(s.get("name") or "").strip()
            desc = str(s.get("description") or "").strip()
            path = str(s.get("path") or "").strip()
            inputs = s.get("inputs") or {}
            if not name or not path:
                continue
            if isinstance(inputs, dict) and inputs:
                lines.append(f"- {name}: {desc} (path: {path}, inputs: {', '.join(inputs.keys())})")
            else:
                lines.append(f"- {name}: {desc} (path: {path})")
        return "\n".join(lines)

    @staticmethod
    def _get_skills_catalog() -> list[dict]:
        try:
            db._ensure_connected()
            if db.db is None:
                return []
            col = db.db["skills"]
            skills = list(col.find({"enabled": True}, {"_id": 0}).sort("name", 1))
            return [s for s in skills if isinstance(s, dict)]
        except Exception:
            return []

    @staticmethod
    def _infer_emotion(text: str) -> str:
        t = (text or "").lower()
        if re.search(r"\b(error|fail|cannot|critical|danger|urgent|blocked|forbidden)\b", t):
            return "critical"
        if re.search(r"\b(thank|great|awesome|nice|good job|love it)\b", t):
            return "warm"
        if re.search(r"\b(analyz|analysis|research|thinking|processing|investigate)\b", t):
            return "analyzing"
        if re.search(r"\b(open|launch|execute|run|action|doing|working)\b", t):
            return "action"
        return "calm"

    @staticmethod
    def _postprocess_followup_edit_actions(user_text: str, context: str, parsed: dict) -> dict:
        """Prevent app re-open and other "repeat task" bugs on follow-ups.

        Goals:
        - If user says "format/rewrite/fix this" (or similar), treat it as editing the current document.
        - Prefer switch_app over open_app (avoids launching a new window).
        - When replacing text, use ctrl+a then type_text.
        - If the model omitted app focus entirely, infer the most recent app from context and switch to it.
        """
        actions = parsed.get("actions") or []
        if not isinstance(actions, list) or not actions:
            parsed["actions"] = actions if isinstance(actions, list) else []
            return parsed

        t = (user_text or "").strip().lower()
        if not t:
            parsed["actions"] = actions
            return parsed

        # Recognize common "format this" requests, including style/structure changes.
        is_followup_edit = bool(
            re.search(
                r"\b(format|reformat|rewrite|polish|improve|refine|fix|correct|cleanup|rephrase|paraphrase|summarize|shorten|expand|make\s+it\s+professional|make\s+it\s+formal|make\s+it\s+clearer|bullet\s+points?|numbered\s+list|headings?|title\s+case|grammar)\b",
                t,
            )
        )
        is_pronoun_followup = bool(re.search(r"\b(this|that|same|it)\b", t))
        if not is_followup_edit:
            parsed["actions"] = actions
            return parsed

        # If user explicitly asked to open/launch, don't override.
        if re.search(r"\b(open|launch|start)\b", t):
            parsed["actions"] = actions
            return parsed

        def _infer_recent_app_name(ctx: str) -> str:
            s = (ctx or "")[-2500:]
            # Look for recent app_name mentions in context/logs.
            m = re.findall(r"\"app_name\"\s*:\s*\"([^\"]+)\"", s)
            if m:
                return str(m[-1]).strip()
            # Fallback: simple phrases
            m2 = re.findall(r"\b(?:open|opened|switch to|switched to)\s+([A-Za-z0-9 _\-]{2,32})\b", s, flags=re.IGNORECASE)
            if m2:
                return str(m2[-1]).strip()
            return ""

        def _is_editor_app(name: str) -> bool:
            nl = (name or "").lower()
            return any(k in nl for k in ("word", "winword", "notepad", "wordpad", "textedit", "vscode", "code"))

        # Identify relevant actions
        first_open_app_idx = None
        first_switch_app_idx = None
        first_type_text_idx = None
        open_app_name = ""

        for idx, a in enumerate(actions):
            if not isinstance(a, dict):
                continue
            at = a.get("type")
            if at == "open_app" and first_open_app_idx is None:
                first_open_app_idx = idx
                open_app_name = str(a.get("app_name") or "").strip()
            if at == "switch_app" and first_switch_app_idx is None:
                first_switch_app_idx = idx
            if at == "type_text" and first_type_text_idx is None:
                first_type_text_idx = idx

        inferred_app = _infer_recent_app_name(context) if (is_pronoun_followup or is_followup_edit) else ""
        target_app = open_app_name or inferred_app

        # If we have no app focus but we are editing text, try to focus last app.
        if first_open_app_idx is None and first_switch_app_idx is None and first_type_text_idx is not None and target_app:
            if _is_editor_app(target_app):
                actions.insert(0, {"type": "switch_app", "app_name": target_app})
                first_type_text_idx += 1

        # Convert open_app -> switch_app for follow-ups to avoid launching a new instance.
        if first_open_app_idx is not None:
            app_name = open_app_name or target_app
            if app_name:
                actions[first_open_app_idx] = {"type": "switch_app", "app_name": app_name}
                # Drop any additional open_app actions.
                actions = [a for a in actions if not (isinstance(a, dict) and a.get("type") == "open_app")]

        # Insert ctrl+a before the first type_text for editor-like apps.
        if first_type_text_idx is not None and target_app and _is_editor_app(target_app):
            # Recompute index after potential filtering.
            for idx, a in enumerate(actions):
                if isinstance(a, dict) and a.get("type") == "type_text":
                    first_type_text_idx = idx
                    break
            # Avoid duplicating if hotkey already exists nearby.
            has_select_all = any(isinstance(a, dict) and a.get("type") == "hotkey" and (a.get("keys") == ["ctrl", "a"] or a.get("key") == "ctrl+a") for a in actions)
            if not has_select_all:
                actions.insert(first_type_text_idx, {"type": "hotkey", "keys": ["ctrl", "a"]})

        parsed["actions"] = actions
        return parsed

    @staticmethod
    def _build_reasonable_draft(user_text: str) -> str:
        """Create a safe, generic draft for common 'write X' requests.

        We intentionally keep this deterministic/lightweight (no extra LLM call) so voice mode
        and automation remain responsive.
        """
        t = (user_text or "").strip()
        tl = t.lower()

        if "email" in tl and ("hr" in tl or "human resources" in tl):
            return (
                "Subject: Request for Information / Application Inquiry\n\n"
                "Dear HR Team,\n\n"
                "I hope you are doing well. My name is [Your Name], and I am reaching out to inquire about opportunities at [Company Name] "
                "for the position of [Role/Designation].\n\n"
                "I have [X years] of experience in [Your Domain/Technology] and have worked on:\n"
                "- [Project/Responsibility 1]\n"
                "- [Project/Responsibility 2]\n"
                "- [Project/Responsibility 3]\n\n"
                "Please let me know if there are any current openings that match my profile. I have attached my resume for your review "
                "and would be grateful for the opportunity to discuss further.\n\n"
                "Thank you for your time and consideration.\n\n"
                "Sincerely,\n"
                "[Your Name]\n"
                "[Phone Number]\n"
                "[Email Address]\n"
                "[LinkedIn / Portfolio URL]"
            )

        # Generic fallback: keep it short and clearly marked.
        # Extract a rough topic after 'write'/'type' if possible.
        # If the user explicitly provided text to type (often quoted), prefer typing exactly that.
        try:
            quoted = re.findall(r"[\"\u201c\u201d]([^\"\u201c\u201d]{1,500})[\"\u201c\u201d]", t)
            if not quoted:
                quoted = re.findall(r"'([^']{1,500})'", t)
            if quoted:
                return str(quoted[-1]).strip() + "\n"
        except Exception:
            pass

        m = re.search(r"\b(?:write|type|draft|compose|create|make)\b\s*(.*)", t, re.IGNORECASE)
        topic = (m.group(1).strip() if m else "")
        if not topic:
            topic = t

        # For explicit 'type X' commands, type the text as-is (no 'Draft:' label).
        try:
            is_type = bool(re.search(r"\btype\b", tl))
            is_drafty = bool(re.search(r"\b(draft|compose)\b", tl))
            if is_type and not is_drafty and ("email" not in tl):
                return f"{topic}\n"
        except Exception:
            pass

        return f"Draft:\n{topic}\n"
