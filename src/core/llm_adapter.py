# src/core/llm_adapter.py
import os
import json
import aiohttp
import re
import random
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
from src.utils.db import db
from src.config import runtime_defaults as rd
from src.config.secrets import llm_secrets

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
        # Primary: OpenAI (ChatGPT). Fallback: Groq (OpenAI-compatible endpoint).
        self.primary_model = rd.PRIMARY_MODEL
        # Optional smarter model for hard tasks (routing by heuristic complexity).
        # Keep PRIMARY_MODEL as a safe default to avoid breaking existing deployments.
        self.smart_model = (rd.SMART_MODEL or "").strip()
        self.smart_model_min_complexity = int(rd.SMART_MODEL_MIN_COMPLEXITY)
        self.primary_key = llm_secrets().primary_api_key
        self.primary_endpoint = (rd.PRIMARY_ENDPOINT or "").strip()

        # Fallback provider (Groq). If primary fails, we attempt this once.
        self.backup_model = rd.BACKUP_MODEL
        self.backup_key = llm_secrets().backup_api_key
        self.backup_endpoint = (rd.BACKUP_ENDPOINT or "").strip()
        self.persona = rd.PERSONA
        self.session = None
        self.timeout = aiohttp.ClientTimeout(total=30)
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

    @staticmethod
    def _resolve_local_reasoner_state_key() -> str:
        try:
            key = str(getattr(rd, "LOCAL_REASONER_STATE_KEY", "global") or "global").strip().lower()
            return key or "global"
        except Exception:
            return "global"

    @staticmethod
    def _sanitize_local_reasoner_key_part(value: str) -> str:
        v = (value or "").strip().lower()
        if not v:
            return ""
        v = re.sub(r"\s+", "_", v)
        v = re.sub(r"[^a-z0-9@._\-]", "", v)
        return v[:120]

    def _extract_user_identity_from_prefs(self, user_prefs: dict | None) -> str:
        if not isinstance(user_prefs, dict):
            return ""
        for field in (
            "user_id",
            "auth_user_id",
            "id",
            "username",
            "email",
            "session_user",
            "profile_id",
        ):
            raw = user_prefs.get(field)
            if raw is None:
                continue
            s = self._sanitize_local_reasoner_key_part(str(raw))
            if s:
                return s
        return ""

    def _derive_local_reasoner_state_key(self, user_prefs: dict | None) -> str:
        base = self._resolve_local_reasoner_state_key()
        if base in {"", "global"}:
            return "global"

        if base in {"user", "per_user", "user_scoped", "userscoped"}:
            ident = self._extract_user_identity_from_prefs(user_prefs)
            if ident:
                return f"user:{ident}"
            return "global"

        # Optional template support: e.g. "tenant:{user}".
        if "{user}" in base:
            ident = self._extract_user_identity_from_prefs(user_prefs)
            if ident:
                return base.replace("{user}", ident)
            return base.replace("{user}", "global")

        return base

    def _ensure_local_reasoner_state_scope(self, user_prefs: dict | None) -> None:
        try:
            desired_key = self._derive_local_reasoner_state_key(user_prefs)
            if desired_key == self._local_reasoner_state_key:
                return
            self._local_reasoner_state_key = desired_key
            self._local_reasoner_state = self._load_local_reasoner_state()
            self._local_reasoner_daily_maintenance()
        except Exception:
            pass

    @staticmethod
    def _resolve_local_reasoner_state_path() -> Path:
        try:
            raw = str(getattr(rd, "LOCAL_REASONER_STATE_FILE", "data/local_reasoner_state.json") or "").strip()
            if not raw:
                raw = "data/local_reasoner_state.json"
            p = Path(raw)
            if p.is_absolute():
                return p
            root = Path(__file__).resolve().parents[2]
            return root / p
        except Exception:
            root = Path(__file__).resolve().parents[2]
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
        }

    def _load_local_reasoner_state(self) -> dict:
        state = self._default_local_reasoner_state()
        if bool(getattr(rd, "LOCAL_REASONER_DB_ENABLED", True)):
            try:
                remote = db.local_reasoner_state_get(state_key=self._local_reasoner_state_key)
                if isinstance(remote, dict):
                    for k, v in state.items():
                        if k not in remote:
                            remote[k] = v
                    if not isinstance(remote.get("app_aliases"), dict):
                        remote["app_aliases"] = {}
                    if not isinstance(remote.get("site_aliases"), dict):
                        remote["site_aliases"] = {}
                    if not isinstance(remote.get("stats"), dict):
                        remote["stats"] = {"learn_events": 0, "hits": 0}
                    return remote

                # User-scoped cold start: inherit global seeded knowledge when user
                # state doesn't exist yet, then continue learning in user scope.
                if str(self._local_reasoner_state_key).startswith("user:"):
                    seeded = db.local_reasoner_state_get(state_key="global")
                    if isinstance(seeded, dict):
                        for k, v in state.items():
                            if k not in seeded:
                                seeded[k] = v
                        if not isinstance(seeded.get("app_aliases"), dict):
                            seeded["app_aliases"] = {}
                        if not isinstance(seeded.get("site_aliases"), dict):
                            seeded["site_aliases"] = {}
                        if not isinstance(seeded.get("stats"), dict):
                            seeded["stats"] = {"learn_events": 0, "hits": 0}
                        return seeded
            except Exception:
                pass
        try:
            p = self._local_reasoner_state_path
            if not p.exists():
                return state
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return state
            for k, v in state.items():
                if k not in data:
                    data[k] = v
            if not isinstance(data.get("app_aliases"), dict):
                data["app_aliases"] = {}
            if not isinstance(data.get("site_aliases"), dict):
                data["site_aliases"] = {}
            if not isinstance(data.get("stats"), dict):
                data["stats"] = {"learn_events": 0, "hits": 0}
            return data
        except Exception:
            return state

    def _save_local_reasoner_state(self) -> None:
        self._local_reasoner_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if bool(getattr(rd, "LOCAL_REASONER_DB_ENABLED", True)):
            try:
                ok = db.local_reasoner_state_upsert(
                    state=self._local_reasoner_state,
                    state_key=self._local_reasoner_state_key,
                )
                if ok:
                    return
            except Exception:
                pass
        try:
            p = self._local_reasoner_state_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._local_reasoner_state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

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

    def _choose_model_for_request(self, text: str, mode: str) -> str:
        """Route to a stronger model for complex tasks when configured."""
        try:
            if not self.smart_model:
                return self.primary_model
            complexity = self._estimate_complexity(text, mode)
            if complexity >= self.smart_model_min_complexity:
                return self.smart_model
        except Exception:
            pass
        return self.primary_model

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
        tl = (user_text or "").strip().lower()
        if not tl:
            return False
        if re.search(r"\b(what|why|how|when|where|which|who)\b", tl):
            return True
        if re.search(r"\b(explain|define|meaning|documentation|docs|guide|tutorial)\b", tl):
            return True
        return False

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
            return "Opening it."
        if at == "web_search":
            return "Looking it up online."
        if at == "device_action":
            return "Applying that setting."
        return "Done."

    def _is_local_reasoner_candidate(self, text: str, mode: str, decision_hint: dict | None) -> bool:
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

        try:
            # Reuse deterministic parser (already battle-tested for local PC commands).
            pre = self._preparse_deterministic_voice_actions(text)
            if isinstance(pre, dict) and isinstance(pre.get("actions"), list) and pre.get("actions"):
                try:
                    pre["actions"] = self._dedupe_actions(pre.get("actions") or [])
                except Exception:
                    pass
                pre["source"] = "local-reasoner"
                if not (pre.get("text") or "").strip():
                    pre["text"] = self._action_text_from_first_action(pre.get("actions") or [])
                self._learn_from_actions(text, pre.get("actions") or [])
                return pre
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

        tl = t.lower().strip()

        # Skill invocation: "run skill X" / "use X skill" / "skill X"
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

        t = (user_text or "").strip().lower()
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

        # High-level analysis doesn't always need web (many prompts are conceptual).
        # Only trigger web for analysis when the user explicitly requests research/sources
        # or when the topic is time-sensitive.
        try:
            if LLMAdapter._is_high_level_analysis_task(user_text):
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
        """Generate a rich, humanlike structured response."""
        try:
            self._ensure_local_reasoner_state_scope(user_prefs)
        except Exception:
            pass

        # Deterministic voice mode: handle simple commands without relying on LLM.
        try:
            if (mode or "").lower() == "voice":
                pre = self._preparse_deterministic_voice_actions(text)
                if isinstance(pre, dict) and isinstance(pre.get("actions"), list) and pre["actions"]:
                    return pre
        except Exception:
            pass

        # Voice + web-worthy requests: do web_search first (better UX, fewer hallucinations).
        # We only do this when we did NOT match a deterministic PC command above.
        try:
            if (mode or "").lower() == "voice":
                if self._should_use_web_lookup(text):
                    parsed = {"text": "Looking it up online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "voice-pre-web"
                        return parsed
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
                if (offline_only or offline_web_only or (offline_analysis and self._is_high_level_analysis_task(text))) and self._should_use_web_lookup(text):
                    parsed = {"text": "Looking it up online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    # Ensure we actually returned a web_search action.
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "offline-web"
                        return parsed

                # Also bypass OpenAI when there's no key configured, but only for
                # web-required high-level questions (keeps local automation usable).
                if (not self.primary_key and not self.backup_key) and self._should_use_web_lookup(text) and self._is_high_level_analysis_task(text):
                    parsed = {"text": "Looking it up online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "offline-web"
                        return parsed
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
                if explicit_research and self._should_use_web_lookup(text):
                    parsed = {"text": "Researching online.", "actions": []}
                    try:
                        parsed = self._postprocess_force_web_lookup(user_text=text, parsed=parsed)
                    except Exception:
                        pass
                    if isinstance(parsed, dict) and isinstance(parsed.get("actions"), list) and parsed["actions"]:
                        parsed["source"] = "pre-web"
                        return parsed
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

                    # Fast-path: in voice mode, if the decision maker is very confident and
                    # the action is safe/obvious, skip the LLM entirely.
                    if (mode or "").lower() == "voice":
                        conf = float(decision_hint.get("confidence") or 0.0)
                        if conf >= float(rd.DECISION_FASTPATH_CONF):
                            a = decision_hint.get("recommended_action")
                            if isinstance(a, dict) and a.get("type") in {"open_app", "open_url", "web_search", "fetch_url"}:
                                out = {"text": "Okay.", "actions": [a], "source": "decision-maker"}
                                self._learn_from_actions(text, out.get("actions") or [])
                                return out
        except Exception:
            decision_hint = None

        # If the assistant likely lacks knowledge (unknown/low-confidence intent), auto-search the web.
        # This triggers the existing 2-pass web pipeline (search -> continue with web context).
        try:
            auto_unknown = bool(rd.AUTO_WEB_ON_UNKNOWN)
            if auto_unknown and self._is_informational_question(text):
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
                        return parsed
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
                    return local
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

        user_prompt = f"""
User said: "{text}"

Context:
    {context[-1800:] if context else '(none)'}

Return ONLY valid JSON matching:
{{
  "text": "...",
  "actions": [{{"type": "..."}}]
}}
"""

        try:
            start = datetime.now(timezone.utc)
            chosen_model = self._choose_model_for_request(text=text, mode=mode)

            provider_source = "openai"
            try:
                response = await self._call_openai(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                    model=chosen_model,
                    endpoint=self.primary_endpoint,
                    api_key=self.primary_key,
                )
            except Exception as e_primary:
                print(f"[LLM WARN] Primary provider failed: {e_primary}")
                if not self.backup_key:
                    raise
                provider_source = "groq"
                response = await self._call_openai(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                    model=self.backup_model,
                    endpoint=self.backup_endpoint,
                    api_key=self.backup_key,
                )
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

            self._learn_from_actions(text, parsed.get("actions") or [])

            print(f"[LLM] {parsed}")
            return parsed

        except Exception as e:
            print(f"[LLM ERROR] {e}")
            db.save_system_event("llm_error", str(e), "error")

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

                    return {
                        "text": "Looking it up online.",
                        "actions": [{"type": "web_search", "query": q, "num_results": 5}],
                        "source": "fallback-web",
                    }
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
                    self._learn_from_actions(text, parsed.get("actions") or [])
                    return parsed
            except Exception:
                pass

            # Fallback humanlike reply
            # IMPORTANT: This is returned when the model call/parsing fails.
            # Avoid "thinking..." style filler that looks like a pending response.
            return {
                "text": (
                    "I couldn't generate a response right now (AI provider unavailable or misconfigured). "
                    "Please try again in a moment. If this keeps happening, check your API key/network "
                    "(env: OPENAI_API_KEY/PRIMARY_API_KEY; fallback: GROQ_API_KEY/BACKUP_API_KEY)."
                ),
                "actions": [],
                "source": "fallback",
            }

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
        actions.append({
            "type": "type_text",
            "text": draft,
            "interval": 0.05,
            # Give the OS time to focus the newly opened window before typing.
            "before_ms": 650,
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
            or re.search(r"\b(to|for)\s+(hr|human\s+resources|recruiter|hiring\s+manager|team|manager|boss)\b", tl)
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

        return not (has_recipient and has_purpose)

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
        parsed["text"] = question if not base_text else (base_text + "\n\n" + question)
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
