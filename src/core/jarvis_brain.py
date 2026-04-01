# src/core/jarvis_brain.py
import asyncio
import os
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from src.utils.db import db
from src.core.llm_adapter import LLMAdapter
from src.utils.task_manager import task_manager
from src.config import runtime_defaults as rd
from src.config.settings import settings as jarvis_settings
from src.config.secrets import llm_secrets

# MCP support (existing integration from prior patch)
try:
    from mcp import MCPClient
except Exception:
    MCPClient = None


class JarvisBrain:
    """Enhanced brain with memory context, reasoning, and humanlike tone."""

    def __init__(self, llm: LLMAdapter, user_id="default"):
        self.llm = llm
        self.user_id = user_id
        self.memory: List[Dict[str, str]] = []
        self.last_mode = "interactive"
        # Operational mode (learn/update/execute/analyze/develop/creative/interact)
        # Stored per-user when possible; fallback to in-memory for anonymous users.
        self._anon_operational_mode = "interact"
        self._seeded_skill_owners: set[str] = set()

        # MCP client (if installed)
        if MCPClient:
            self.mcp = MCPClient(server_url="http://localhost:9090")
            self.mcp_connected = False
        else:
            self.mcp = None
            self.mcp_connected = False

        # -------------------------
        # Self-learning components
        # -------------------------
        # In-memory buffer of interactions to convert into training examples.
        # Each item: {"prompt": str, "completion": str, "meta": {...}}
        self.learning_buffer: List[Dict[str, Any]] = []
        # Minimum number of examples to generate a dataset
        self.min_examples_for_finetune = int(rd.MIN_FINETUNE_EXAMPLES)
        # Safety toggle: require manual approval before any automated code change
        self.require_manual_approval = bool(rd.REQUIRE_MANUAL_APPROVAL)

        # Filesystem sandboxing
        self.project_root = Path(__file__).resolve().parents[2]
        # Default allowlist: keep self-modifying actions inside the repo only.
        allowed = str(rd.ALLOWED_PATHS_CSV).strip()
        self.allowed_roots = []
        for rel in [p.strip() for p in allowed.split(",") if p.strip()]:
            self.allowed_roots.append((self.project_root / rel).resolve())

        # Blocklist for sensitive files/dirs even if under allowed roots
        self.blocked_names = {
            ".env",
            ".env.example",
            "id_rsa",
            "id_rsa.pub",
        }
        self.blocked_dirnames = {
            ".git",
            "venv",
            "__pycache__",
            ".pytest_cache",
            "node_modules",
        }

    def is_path_allowed(self, path: str) -> bool:
        """Return True if the given path is inside the allowed sandbox.

        This is used by the action executor to prevent arbitrary file access.
        """
        try:
            if not path:
                return False

            p = Path(path)
            if not p.is_absolute():
                p = (self.project_root / p)
            rp = p.resolve()

            # Must be inside project root at all
            try:
                rp.relative_to(self.project_root)
            except Exception:
                return False

            # Block sensitive names / directories
            if rp.name in self.blocked_names:
                return False
            if any(part in self.blocked_dirnames for part in rp.parts):
                return False

            # Must be inside one of the allowed roots
            for root in self.allowed_roots:
                try:
                    rp.relative_to(root)
                    return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    # -------------------------
    # MCP helpers (non-breaking)
    # -------------------------
    async def ensure_mcp(self):
        if not self.mcp:
            return False
        if not self.mcp_connected:
            try:
                await self.mcp.connect()
                self.mcp_connected = True
                print("[MCP] Connected")
            except Exception as e:
                print(f"[MCP] Connection failed: {e}")
                self.mcp_connected = False
        return self.mcp_connected

    async def run_mcp_tool(self, tool_name, args):
        if not self.mcp:
            return f"[MCP] Not available in this environment."
        ok = await self.ensure_mcp()
        if not ok:
            return f"[MCP] Cannot run tool '{tool_name}' — server unavailable."

        try:
            result = await self.mcp.call(tool_name, args)
            return result
        except Exception as e:
            return f"[MCP] Tool error: {e}"

    # -------------------------
    # Self-learning utilities
    # -------------------------
    def _redact_for_storage(self, text: str) -> str:
        """Best-effort secret redaction before saving to DB."""
        try:
            if not text:
                return text
            import re

            out = text

            # Common API key patterns / env dumps
            out = re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED_API_KEY]", out)
            out = re.sub(r"(?i)(OPENAI_API_KEY|PRIMARY_API_KEY|JARVIS_JWT_SECRET|MONGODB_URI)\s*[:=]\s*[^\s\n\r]+", r"\1=[REDACTED]", out)

            # Bearer tokens
            out = re.sub(r"(?i)Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", "Bearer [REDACTED]", out)
            return out
        except Exception:
            return text

    def save_interaction_for_learning(self, user_text: str, assistant_text: str, actions: List[Dict]=None):
        """
        Add a curated example to the learning buffer.
        This collects the user prompt and the assistant reply (cleaned).
        """
        try:
            if not user_text or not assistant_text:
                return

            # Clean text minimally
            prompt = user_text.strip()
            completion = assistant_text.strip()

            # Avoid trivial or extremely short entries
            if len(prompt) < 5 or len(completion) < 5:
                return

            # Optionally attach actions metadata (useful for behavior cloning)
            example = {
                "prompt": prompt,
                "completion": completion,
                "meta": {
                    "when": datetime.utcnow().isoformat(),
                    "actions": actions or []
                }
            }

            self.learning_buffer.append(example)

            # Persist to DB (opt-out via env)
            if bool(rd.LEARNING_ENABLED):
                try:
                    db.save_learning_example(
                        user_id=self.user_id,
                        prompt=self._redact_for_storage(prompt),
                        completion=self._redact_for_storage(completion),
                        meta=example.get("meta", {}),
                        tags=[],
                    )
                except Exception as e:
                    # Non-fatal
                    db.save_system_event("learn_persist_error", str(e), "warning")

            # Keep buffer bounded in memory
            max_buf = int(rd.LEARNING_BUFFER_MAX)
            if len(self.learning_buffer) > max_buf:
                # drop oldest
                self.learning_buffer = self.learning_buffer[-max_buf:]

        except Exception as e:
            print(f"[LEARN SAVE ERROR] {e}")
            db.save_system_event("learn_save_error", str(e), "warning")

    def prepare_finetune_dataset(self, path: str):
        """
        Convert learning_buffer -> jsonl file at `path`.
        Format per OpenAI fine-tune (prompt / completion).
        Returns number of examples written.
        """
        try:
            if not self.learning_buffer:
                return 0

            # Ensure directory exists
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

            written = 0
            with open(path, "w", encoding="utf-8") as fh:
                for ex in self.learning_buffer:
                    prompt = ex.get("prompt", "")
                    # Ensure prompt closed appropriately for fine-tuning and model delim
                    prompt_text = prompt.strip() + "\n\n###\n\n"
                    completion_text = " " + ex.get("completion", "").strip() + " END"

                    obj = {"prompt": prompt_text, "completion": completion_text}
                    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    written += 1

            return written

        except Exception as e:
            print(f"[PREPARE FINETUNE ERROR] {e}")
            db.save_system_event("prepare_finetune_error", str(e), "error")
            return 0

    async def run_self_learning_cycle(self, save_path: str = "data/finetune/jarvis_dataset.jsonl", submit: bool = False):
        """
        Prepare a fine-tune dataset from collected examples and optionally submit it.
        This is manual / on-demand — nothing runs automatically.
        - save_path: where JSONL is written
        - submit: if True, attempt to call OpenAI fine-tune endpoint (opt-in)
        Returns a report dict.
        """
        report = {"status": "started", "saved": 0, "submitted": False, "errors": []}

        try:
            # 1) If db exposes recent chats, prefer them; otherwise use in-memory buffer
            try:
                recent = []
                if hasattr(db, "load_recent_chats"):
                    recent = db.load_recent_chats(limit=1000) or []
                if recent:
                    # Convert DB chats -> candidate examples (simple heuristic)
                    for c in recent:
                        user_text = c.get("user_input") or c.get("input") or ""
                        bot_text = c.get("bot_response") or c.get("response") or ""
                        actions = None
                        # don't overwrite buffer with duplicates, but we keep them in memory as needed
                        if user_text and bot_text:
                            self.save_interaction_for_learning(user_text, bot_text, actions)
            except Exception as e:
                # Non-fatal - continue with currently collected buffer
                report["errors"].append(f"db-load-fallback:{e}")

            # 2) Validate buffer size
            cnt = len(self.learning_buffer)
            report["buffer_count"] = cnt
            if cnt < self.min_examples_for_finetune:
                report["status"] = "insufficient_examples"
                return report

            # 3) Prepare dataset
            written = self.prepare_finetune_dataset(save_path)
            report["saved"] = written
            report["status"] = "dataset_prepared"

            # 4) Optional: quick self-eval (generate few prompts from dataset with LLM and compare)
            try:
                eval_samples = min(5, max(0, written))
                eval_report = []
                for i in range(eval_samples):
                    ex = self.learning_buffer[-(i+1)]
                    prompt = ex["prompt"]
                    # Ask the LLM to produce a reply given the prompt (use mode=chat)
                    gen = await self.llm.generate_response(prompt, context="", mode="chat", capabilities=["self_eval"])
                    gen_text = gen.get("text", "").strip()
                    # Basic similarity (substring) check
                    match = ex["completion"].strip()[:80] in gen_text[:160]
                    eval_report.append({"prompt_preview": prompt[:120], "match": match, "gen": gen_text[:200]})
                report["eval"] = eval_report
            except Exception as e:
                report["errors"].append(f"eval_error:{e}")

            # 5) Optional submit to OpenAI fine-tune (explicit opt-in)
            if submit:
                # Minimal check for keys / endpoint availability
                openai_key = llm_secrets().primary_api_key
                if not openai_key:
                    report["errors"].append("openai_key_missing")
                else:
                    # Attempt to call OpenAI fine-tune endpoint via aiohttp
                    try:
                        import aiohttp
                        # NOTE: This submission is a simple lightweight attempt.
                        # Many production steps (file upload, file_id, fine-tune creation) are omitted for brevity.
                        # For full control use the OpenAI CLI or SDK to upload file then create fine-tune.
                        report["submit_hint"] = "Use OpenAI CLI or SDK to upload JSONL and create fine-tune job. This patch does not fully orchestrate upload."
                        report["submitted"] = False
                    except Exception as e:
                        report["errors"].append(f"openai_submit_error:{e}")

            return report

        except Exception as e:
            report["status"] = "failed"
            report["errors"].append(str(e))
            db.save_system_event("self_learning_error", str(e), "error")
            return report

    # -------------------------
    # MAIN REASONING LOGIC (unchanged except for saving examples)
    # -------------------------
    async def handle_message(self, text: str, mode="chat", user_id=None):
        """Main conversational + action reasoning pipeline."""
        session_id = user_id or self.user_id
        # Keep brain identity aligned with the caller for learning/persistence.
        self.user_id = session_id

        resumed_from_pending = False

        # --- Dialogue: pending clarification (human-like follow-up) ---
        # If Jarvis asked a clarifying question previously, treat the next short reply
        # as the answer and resume the original request.
        try:
            from src.core.dialogue_state import get_dialogue_state_store, PendingClarification
            from src.core.clarification_learning import get_clarification_learner, ClarificationExample

            ds = get_dialogue_state_store()
            pending = ds.load_pending(session_id)

            if pending is not None:
                # Allow the user to cancel the pending clarification.
                if ds.is_cancel_message(text):
                    ds.clear_pending(session_id)
                elif ds.looks_like_direct_answer(text, pending_kind=pending.kind):
                    # Learn from this clarification answer (best-effort).
                    try:
                        learner = get_clarification_learner()
                        slots = learner.extract_slots(pending.kind, pending.question, text)
                        ex = ClarificationExample(
                            kind=pending.kind,
                            question=pending.question,
                            original_user_text=pending.original_user_text,
                            answer_text=text.strip(),
                            slots=slots,
                            created_at=time.time(),
                        )
                        learner.record(session_id, ex)
                    except Exception:
                        pass

                    # Merge: original request + answer context.
                    # Keep it explicit so downstream logic/LLM can ground decisions.
                    resumed = (
                        f"{pending.original_user_text}\n\n"
                        f"Follow-up answer: {text.strip()}"
                    ).strip()
                    ds.clear_pending(session_id)
                    text = resumed
                    resumed_from_pending = True
        except Exception:
            pass

        # If any background research finished since the last user interaction, surface it now.
        research_notice = ""
        try:
            from src.utils.task_manager import task_manager, TaskStatus

            tasks = task_manager.get_all_tasks() or []
            # Find newest completed, unnotified research tasks for this user.
            completed = []
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                meta = t.get("meta") if isinstance(t.get("meta"), dict) else {}
                if (meta.get("task_type") or "") != "research":
                    continue
                if (meta.get("user_id") or "") != session_id:
                    continue
                if meta.get("notified") is True:
                    continue
                if t.get("status") != TaskStatus.COMPLETED.value:
                    continue
                completed.append(t)

            # Show up to 2 most recent completions.
            completed = completed[-2:]
            if completed:
                parts = []
                for t in completed:
                    meta = t.get("meta") if isinstance(t.get("meta"), dict) else {}
                    topic = (meta.get("topic") or t.get("description") or "").strip()
                    summary = ""
                    for r in reversed(t.get("results") or []):
                        if isinstance(r, dict) and r.get("type") == "research_result":
                            summary = (r.get("summary") or "").strip()
                            break
                    if not summary:
                        summary = "(No summary available.)"
                    parts.append(f"Research is complete about: {topic}\n\n{summary}")
                    # Mark notified.
                    try:
                        task_manager.update_task(t.get("id"), meta_update={"notified": True})
                    except Exception:
                        pass
                research_notice = "\n\n---\n\n".join(parts).strip()
        except Exception:
            research_notice = ""

        def _is_research_status_question(raw: str) -> bool:
            tl = (raw or "").strip().lower()
            if not tl:
                return False
            return bool(
                re.search(
                    r"\b(?:did|do|have|has|are|is)\s+(?:you\s+)?(?:already\s+)?(?:complete|completed|finish|finished|done)\s+(?:the\s+)?research\b",
                    tl,
                )
                or re.search(r"\bresearch\s+(?:status|progress|update)\b", tl)
            )

        # If the user is asking for research status, answer from our task manager (internal state)
        # rather than starting a new web lookup.
        try:
            if _is_research_status_question(text):
                try:
                    from src.utils.task_manager import task_manager, TaskStatus

                    tasks = task_manager.get_all_tasks() or []
                    active = []
                    for t in tasks:
                        if not isinstance(t, dict):
                            continue
                        meta = t.get("meta") if isinstance(t.get("meta"), dict) else {}
                        if (meta.get("task_type") or "") != "research":
                            continue
                        if (meta.get("user_id") or "") != session_id:
                            continue
                        if t.get("status") in {TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value, TaskStatus.PAUSED.value}:
                            active.append(t)

                    parts = []
                    if active:
                        lines = []
                        for t in active[-3:]:
                            meta = t.get("meta") if isinstance(t.get("meta"), dict) else {}
                            topic = (meta.get("topic") or t.get("description") or "").strip()
                            status = (t.get("status") or "").strip()
                            lines.append(f"- {topic or '(topic unknown)'} ({status})")
                        parts.append("Research is still running:\n" + "\n".join(lines))
                    else:
                        parts.append("No research task is currently running.")

                    # If something just completed, show it immediately.
                    if research_notice:
                        parts.insert(0, research_notice)

                    return {
                        "text": "\n\n".join([p for p in parts if p]).strip(),
                        "actions": [],
                        "tool_results": [],
                        "mode": mode,
                        "source": "research-status",
                    }
                except Exception:
                    # Fallback if task manager isn't available.
                    base = "I can notify you when research completes."
                    if research_notice:
                        base = (research_notice + "\n\n" + base).strip()
                    return {"text": base, "actions": [], "tool_results": [], "mode": mode, "source": "research-status"}
        except Exception:
            pass

        def _parse_mode_switch_command(raw: str) -> str | None:
            tl = (raw or "").strip().lower()
            if not tl:
                return None

            # Common voice patterns
            # Examples: "change mode to learn", "switch to execute mode", "set mode analyze"
            m = None
            m1 = re.search(r"\b(?:change|switch|set)\s+(?:the\s+)?mode\s+(?:to\s+)?(learn|update|execute|analyze|develop|creative|interact)\b", tl)
            if m1:
                m = m1.group(1)
            if not m:
                m2 = re.search(r"\b(?:switch|go)\s+to\s+(learn|update|execute|analyze|develop|creative|interact)\s+mode\b", tl)
                if m2:
                    m = m2.group(1)
            if not m:
                # Short command: "learn mode" / "execute mode"
                m3 = re.search(r"\b(learn|update|execute|analyze|develop|creative|interact)\s+mode\b", tl)
                if m3 and tl.split()[-1] == "mode":
                    m = m3.group(1)
            return m

        # Handle mode switching early (especially important for voice UX)
        try:
            requested_mode = _parse_mode_switch_command(text)
            if requested_mode:
                try:
                    from src.utils.voice_auth import voice_auth

                    if session_id and session_id != "default":
                        # Persist per-user (works for both DB + file-backed auth stores)
                        out = voice_auth.set_operational_mode(session_id, requested_mode)
                        if out.get("status") == "success":
                            return {
                                "text": f"Mode changed to '{requested_mode}'.",
                                "actions": [],
                                "tool_results": [],
                                "mode": mode,
                                "source": "mode-switch",
                            }
                except Exception:
                    # Fallback to per-process mode memory for anonymous or if auth store not available
                    self._anon_operational_mode = requested_mode
                    return {
                        "text": f"Mode changed to '{requested_mode}'.",
                        "actions": [],
                        "tool_results": [],
                        "mode": mode,
                        "source": "mode-switch",
                    }
        except Exception:
            pass

        # Resolve current operational mode for this user (used for better decision-making)
        operational_mode = "interact"
        user_prefs: dict = {}
        username = None
        try:
            from src.utils.voice_auth import voice_auth

            if session_id and session_id != "default":
                ok, username = voice_auth.validate_session(session_id)
                if ok and username:
                    operational_mode = voice_auth.get_operational_mode(username)
                    user_prefs = voice_auth.get_preferences(username) or {}
                else:
                    operational_mode = self._anon_operational_mode
                    user_prefs = {}
            else:
                operational_mode = self._anon_operational_mode
                user_prefs = {}
        except Exception:
            operational_mode = self._anon_operational_mode
            user_prefs = {}

        # Merge DB preferences (if available) to keep UI/API preferences consistent.
        try:
            if username:
                db._ensure_connected()
                if db.db is not None:
                    col = db.db["user_preferences"]
                    user_id = (username or "").strip().lower()
                    doc = col.find_one({"user_id": user_id}, {"_id": 0, "preferences": 1}) or {}
                    db_prefs = doc.get("preferences") if isinstance(doc, dict) else {}
                    if isinstance(db_prefs, dict):
                        user_prefs.update(db_prefs)
        except Exception:
            pass

        # Seed default skills per user (research + web scrape) once.
        try:
            if username and username not in self._seeded_skill_owners:
                self._seeded_skill_owners.add(username)
                db._ensure_connected()
                if db.db is not None:
                    col = db.db["skills"]
                    col.create_index([("owner", 1), ("name", 1)], unique=True)
                    owner = (username or "").strip().lower()
                    existing = col.find_one({"owner": owner})
                    if not existing:
                        now = datetime.utcnow()
                        defaults = [
                            {
                                "owner": owner,
                                "name": "market_research",
                                "description": "Researches a topic and returns a summarized answer with sources.",
                                "type": "n8n",
                                "path": "skills/market-research",
                                "enabled": True,
                                "version": "1.0",
                                "tags": ["research", "summary"],
                                "inputs": {"query": "string", "region": "string", "time_range": "string"},
                                "outputs": {"summary": "string", "sources": "list"},
                                "trigger_phrases": ["research", "market research", "analyze market"],
                                "created_at": now,
                                "updated_at": now,
                            },
                            {
                                "owner": owner,
                                "name": "web_scrape",
                                "description": "Scrapes a URL and returns extracted content.",
                                "type": "n8n",
                                "path": "skills/web-scrape",
                                "enabled": True,
                                "version": "1.0",
                                "tags": ["scrape", "extract"],
                                "inputs": {"url": "string", "selector": "string"},
                                "outputs": {"content": "string"},
                                "trigger_phrases": ["scrape", "extract from", "get content from"],
                                "created_at": now,
                                "updated_at": now,
                            },
                        ]
                        try:
                            col.insert_many(defaults, ordered=False)
                        except Exception:
                            pass
        except Exception:
            pass

        def _maybe_update_preferences_from_user_text(raw: str) -> None:
            """Heuristically learn preferences from explicit or implicit user feedback."""
            tl = (raw or "").strip().lower()
            if not tl:
                return

            # Verbosity controls
            verbosity = None
            if re.search(r"\b(be\s+)?(brief|short|concise)\b", tl) or re.search(r"\b(short\s+answer|one\s+line)\b", tl):
                verbosity = "low"
            elif re.search(r"\b(more\s+detail(ed)?|detailed|explain\s+more|step\s+by\s+step|deep\s+dive)\b", tl):
                verbosity = "high"
            elif re.search(r"\btoo\s+long\b", tl):
                verbosity = "low"

            persona = None
            if re.search(r"\b(be\s+)?friendly\b", tl):
                persona = "friendly"
            elif re.search(r"\b(be\s+)?formal\b", tl):
                persona = "formal-gentle"
            elif re.search(r"\b(analyst|technical|just\s+facts)\b", tl):
                persona = "analyst"

            # Language preference (explicit)
            language = None
            m_lang = re.search(r"\b(set|switch|change|use|add)\s+language\s+(to\s+)?([a-zA-Z\- ]{2,32})\b", tl)
            if m_lang:
                language = (m_lang.group(3) or "").strip().lower()

            if not verbosity and not persona and not language:
                return

            # Persist preferences for authenticated users; otherwise keep in-session only.
            if session_id and session_id != "default" and username:
                try:
                    from src.utils.voice_auth import voice_auth

                    if verbosity:
                        voice_auth.set_preference(username, "verbosity", verbosity)
                        user_prefs["verbosity"] = verbosity
                    if persona:
                        voice_auth.set_preference(username, "persona", persona)
                        user_prefs["persona"] = persona
                    if language:
                        voice_auth.set_preference(username, "language", language)
                        # track language list
                        langs = user_prefs.get("languages")
                        if not isinstance(langs, list):
                            langs = []
                        if language not in langs:
                            langs.append(language)
                        voice_auth.set_preference(username, "languages", langs)
                        user_prefs["language"] = language
                        user_prefs["languages"] = langs
                except Exception:
                    if verbosity:
                        user_prefs["verbosity"] = verbosity
                    if persona:
                        user_prefs["persona"] = persona
                    if language:
                        user_prefs["language"] = language
            else:
                if verbosity:
                    user_prefs["verbosity"] = verbosity
                if persona:
                    user_prefs["persona"] = persona
                if language:
                    user_prefs["language"] = language

        # Auto-learn preferences from user usage/feedback.
        try:
            _maybe_update_preferences_from_user_text(text)
        except Exception:
            pass

        def _normalize_lang(name: str) -> tuple[str, str] | None:
            if not name:
                return None
            n = name.strip().lower()
            mapping = {
                "english": ("English", "en-US"),
                "hindi": ("Hindi", "hi-IN"),
                "gujarati": ("Gujarati", "gu-IN"),
            }
            if n in mapping:
                return mapping[n]

            # Allow direct language codes (e.g., en-US, fr-FR, es)
            if re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", n):
                code = n if "-" in n else f"{n}-{n.upper()}"
                return (code, code)
            # Fallback: store arbitrary language name
            return (name.strip().title(), name.strip().title())

        def _parse_language_command(raw: str) -> tuple[str, str] | None:
            tl = (raw or "").strip().lower()
            if not tl:
                return None
            m = re.search(r"\b(add|set|switch|change|use)\s+language\s+(to\s+)?([a-zA-Z\- ]{2,32})\b", tl)
            if not m:
                return None
            lang = (m.group(3) or "").strip()
            norm = _normalize_lang(lang)
            return norm if norm else None

        try:
            lang = _parse_language_command(text)
            if lang:
                lang_name, lang_code = lang
                # Persist preferences for authenticated users when possible.
                if username:
                    try:
                        from src.utils.voice_auth import voice_auth
                        voice_auth.set_preference(username, "language", lang_name)
                        voice_auth.set_preference(username, "language_code", lang_code)
                        langs = user_prefs.get("languages")
                        if not isinstance(langs, list):
                            langs = []
                        if lang_name not in langs:
                            langs.append(lang_name)
                        voice_auth.set_preference(username, "languages", langs)
                        user_prefs["language"] = lang_name
                        user_prefs["language_code"] = lang_code
                        user_prefs["languages"] = langs
                    except Exception:
                        user_prefs["language"] = lang_name
                        user_prefs["language_code"] = lang_code

                    # Also persist to DB preferences if available.
                    try:
                        db._ensure_connected()
                        if db.db is not None:
                            col = db.db["user_preferences"]
                            user_id = (username or "").strip().lower()
                            col.update_one(
                                {"user_id": user_id},
                                {"$set": {
                                    "preferences.language": lang_name,
                                    "preferences.language_code": lang_code,
                                    "preferences.languages": user_prefs.get("languages", [lang_name]),
                                    "updated_at": datetime.utcnow(),
                                }, "$setOnInsert": {"user_id": user_id, "created_at": datetime.utcnow()}},
                                upsert=True,
                            )
                    except Exception:
                        pass
                else:
                    user_prefs["language"] = lang_name
                    user_prefs["language_code"] = lang_code

                return {
                    "text": f"Language set to {lang_name}.",
                    "actions": [],
                    "language": lang_code,
                    "source": "language-set",
                }
        except Exception:
            pass

        def _slugify(name: str) -> str:
            s = re.sub(r"[^a-zA-Z0-9_\- ]+", "", name or "").strip().lower()
            s = re.sub(r"\s+", "-", s)
            return s[:60] if s else "skill"

        def _save_skill(owner: str, skill: dict) -> bool:
            try:
                db._ensure_connected()
                if db.db is not None:
                    col = db.db["skills"]
                    col.create_index([("owner", 1), ("name", 1)], unique=True)
                    col.update_one(
                        {"owner": owner, "name": skill.get("name")},
                        {"$set": skill, "$setOnInsert": {"created_at": datetime.utcnow()}},
                        upsert=True,
                    )
                    return True
            except Exception:
                pass
            # Fallback to local file if DB unavailable
            try:
                root = Path(__file__).resolve().parents[2]
                skills_path = root / "data" / "skills.json"
                data = []
                if skills_path.exists():
                    data = json.loads(skills_path.read_text(encoding="utf-8")) or []
                data = [s for s in data if isinstance(s, dict) and s.get("name") != skill.get("name")]
                data.append({k: v for k, v in skill.items() if k in {"name", "description", "type", "path", "enabled"}})
                skills_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
                return True
            except Exception:
                return False

        # Skill management via voice: add/enable/disable
        try:
            tl = (text or "").strip().lower()
            add_match = re.search(r"\b(add|create)\s+skill\s+([a-z0-9_\- ]{2,80})", tl)
            enable_match = re.search(r"\b(enable|disable)\s+skill\s+([a-z0-9_\- ]{2,80})", tl)

            if add_match:
                raw_name = add_match.group(2).strip()
                desc_match = re.search(r"\b(description|about|for)\b\s+(.+)$", tl)
                description = (desc_match.group(2).strip() if desc_match else None)
                path = f"skills/{_slugify(raw_name)}"
                owner = (username or "default").strip().lower()
                skill = {
                    "owner": owner,
                    "name": raw_name,
                    "description": description,
                    "type": "n8n",
                    "path": path,
                    "enabled": True,
                    "version": "1.0",
                    "tags": [],
                    "inputs": {"query": "string"},
                    "outputs": {"result": "string"},
                    "trigger_phrases": [f"run skill {raw_name}"],
                    "updated_at": datetime.utcnow(),
                }
                ok = _save_skill(owner, skill)
                if ok:
                    return {
                        "text": f"Skill '{raw_name}' added. Create an N8N webhook at {path} and I can run it.",
                        "actions": [],
                        "source": "skill-add",
                    }

            if enable_match:
                raw_name = enable_match.group(2).strip()
                enabled = enable_match.group(1).strip().lower() == "enable"
                owner = (username or "default").strip().lower()
                skill = {
                    "owner": owner,
                    "name": raw_name,
                    "enabled": enabled,
                    "updated_at": datetime.utcnow(),
                }
                ok = _save_skill(owner, skill)
                if ok:
                    return {
                        "text": f"Skill '{raw_name}' {'enabled' if enabled else 'disabled'}.",
                        "actions": [],
                        "source": "skill-toggle",
                    }
        except Exception:
            pass

        context = "\n".join([f"{m['role']}: {m['text']}" for m in self.memory[-6:]])
        if operational_mode:
            context = (context + "\n\n" if context else "") + f"Operational mode: {operational_mode}"

        # If the user explicitly requested research/analysis, add a compact hint.
        try:
            tl = (text or "").strip().lower()
            wants_research = bool(
                re.search(
                    r"\b(research|do\s+research|make\s+research|with\s+sources|with\s+citations|with\s+links|"
                    r"sources?|citations?|cite|links?|analyze|analysis|summarize|summary)\b",
                    tl,
                )
            )
            if wants_research:
                context = (context + "\n" if context else "") + "Research requested: prioritize web_search/fetch_url first, then provide a grounded summary + analysis."
        except Exception:
            pass

        # Personalization context (kept compact)
        try:
            if isinstance(user_prefs, dict) and user_prefs:
                compact = {}
                for k in ("verbosity", "persona", "language", "language_code"):
                    v = user_prefs.get(k)
                    if v is not None:
                        compact[k] = v
                if compact:
                    context = (context + "\n" if context else "") + f"User preferences: {json.dumps(compact, ensure_ascii=False)}"
        except Exception:
            pass

        # RAG-lite: pull a few relevant past examples from DB and inject as additional context
        try:
            if bool(rd.LEARNING_RETRIEVE):
                k = int(rd.LEARNING_RETRIEVAL_K)
                k = max(0, min(k, 8))
                if k:
                    hits = db.search_learning_examples(text, user_id=session_id, limit=k)
                    if hits:
                        max_chars = int(rd.LEARNING_MAX_CONTEXT_CHARS)
                        parts = []
                        for h in hits:
                            p = (h.get("prompt") or "").strip()
                            c = (h.get("completion") or "").strip()
                            if not p or not c:
                                continue
                            parts.append(f"User: {p}\nAssistant: {c}")
                        learned_block = "\n---\n".join(parts)
                        learned_block = learned_block[:max_chars]
                        if learned_block:
                            context = (context + "\n\n" if context else "") + "Learned examples (use as guidance, not verbatim):\n" + learned_block
        except Exception:
            # Never block replies on retrieval issues
            pass

        # Web knowledge: inject a few relevant internet-fetched summaries (stored in DB)
        try:
            if bool(rd.WEB_KNOWLEDGE_CONTEXT):
                k = int(rd.WEB_KNOWLEDGE_K)
                k = max(0, min(k, 6))
                if k:
                    items = db.search_web_training(text, limit=k)
                    if items:
                        max_chars = int(rd.WEB_KNOWLEDGE_MAX_CHARS)
                        chunks = []
                        for it in items:
                            title = (it.get("title") or "").strip()
                            url = (it.get("url") or "").strip()
                            snippet = (it.get("snippet") or "").strip()
                            summary = (it.get("summary") or "").strip()
                            insight = (it.get("analysis_insight") or "").strip()
                            tags = it.get("analysis_tags") or []
                            if not isinstance(tags, list):
                                tags = []

                            # Prefer compact, precomputed insight (background analysis) for best UX.
                            body = insight or summary or snippet
                            if not body:
                                continue
                            header = title if title else (it.get("topic") or "").strip()
                            if url:
                                if tags:
                                    tag_str = ", ".join([str(t) for t in tags[:8] if str(t).strip()])
                                    if tag_str:
                                        chunks.append(f"- {header}\n  {body}\n  Tags: {tag_str}\n  Source: {url}")
                                    else:
                                        chunks.append(f"- {header}\n  {body}\n  Source: {url}")
                                else:
                                    chunks.append(f"- {header}\n  {body}\n  Source: {url}")
                            else:
                                chunks.append(f"- {header}\n  {body}")
                        web_block = "\n".join(chunks)[:max_chars]
                        if web_block:
                            context = (context + "\n\n" if context else "") + "Recent web knowledge (summaries; verify if critical):\n" + web_block
        except Exception:
            pass

        try:
            # Detect self-updates
            text_lower = text.lower()
            is_self_update = any(keyword in text_lower for keyword in [
                "update", "modify", "improve", "edit", "change", "add", "create", "make", "build"
            ]) and any(keyword in text_lower for keyword in [
                "file", "module", "component", "code", "system", "bot", "jarvis"
            ])

            # Auto-apply learned defaults from prior clarifications to reduce follow-up questions.
            # Skip when we just resumed from a clarification (we already have the answer).
            try:
                if not resumed_from_pending:
                    from src.core.clarification_learning import get_clarification_learner

                    learner = get_clarification_learner()
                    text, _applied = learner.augment_request(session_id, text)
            except Exception:
                pass

            # Soft capability hints for the LLM (not enforced). Keep aligned with executor/UI support.
            capabilities = [
                # Internet/research
                "web_search",
                "fetch_url",
                "open_url",
                "n8n_webhook",

                # PC/device
                "open_app",
                "close_app",
                "switch_app",
                "execute_command",
                "type_text",
                "press_key",
                "hotkey",
                "capture_screen",
                "screen_navigation",

                # Files (repo sandbox)
                "read",
                "list",
                "mkdir",
                "write",
                "edit",
                "delete",
                "move",
                "copy",
                "cleanup",
                "find_files",

                # Task helpers
                "create_task",
                "stop_task",
                "check_errors",
                "fix_errors",
                "generate_email",

                # Other
                "mode_switch",
            ]

            # allow LLM to output MCP tool actions when self update requested
            if is_self_update:
                capabilities.append("self_update")
                capabilities.append("self_add")
                capabilities.append("mcp_tools")

            # Generate LLM response
            response = None

            # Optional: rule-first agentic loop (deterministic planning/execution).
            # This reduces reliance on the LLM for common assistant workflows.
            agentic_enabled = bool(getattr(rd, "AGENTIC_LOOP", False))
            if agentic_enabled:
                try:
                    from src.core.agent_loop import get_agent_loop

                    min_conf = float(getattr(rd, "AGENTIC_MIN_CONFIDENCE", 0.88))
                    max_sub = int(getattr(rd, "AGENTIC_MAX_SUBTASKS", 6))

                    loop = await get_agent_loop(min_confidence=min_conf, max_subtasks=max_sub)
                    proposed = await loop.propose(text=text, mode=mode, context=context or "")
                    if isinstance(proposed, dict) and (proposed.get("text") is not None):
                        response = proposed
                except Exception:
                    response = None

            # Fallback to LLM when agentic path is disabled/uncertain.
            if response is None:
                response = await self.llm.generate_response(
                    text, context=context, mode=mode, capabilities=capabilities, user_prefs=user_prefs
                )

            # If LLM returned actions, run MCP tools (if available)
            actions = response.get("actions", [])
            tool_results = []

            # Apply mode changes requested by the LLM (do not forward as an executor action).
            if isinstance(actions, list) and actions:
                try:
                    filtered = []
                    for a in actions:
                        if not isinstance(a, dict):
                            continue
                        if a.get("type") in {"set_mode", "mode_switch"}:
                            new_mode = (a.get("mode") or a.get("new_mode") or "").strip().lower()
                            if new_mode:
                                try:
                                    from src.utils.voice_auth import voice_auth

                                    if session_id and session_id != "default":
                                        voice_auth.set_operational_mode(session_id, new_mode)
                                    else:
                                        self._anon_operational_mode = new_mode
                                except Exception:
                                    self._anon_operational_mode = new_mode
                            # Don't forward this as an action.
                            continue
                        filtered.append(a)
                    actions = filtered
                    response["actions"] = actions
                except Exception:
                    pass

            for action in actions:
                if action.get("type") == "mcp_tool":
                    # Cloud deployments must not perform server-side tool execution.
                    if bool(jarvis_settings.cloud_mode):
                        tool_results.append({
                            "tool": action.get("tool"),
                            "args": action.get("args", {}),
                            "result": "[MCP] Disabled in cloud mode."
                        })
                        continue
                    tool_name = action.get("tool")
                    args = action.get("args", {})

                    result = await self.run_mcp_tool(tool_name, args)
                    tool_results.append({
                        "tool": tool_name,
                        "args": args,
                        "result": result
                    })

            # -------------------------
            # LEARNING: save good interactions into buffer
            # -------------------------
            # Heuristic: if assistant provided a substantive reply (length) or executed actions, save it
            try:
                assistant_text = response.get("text", "")
                if assistant_text and (len(assistant_text.strip()) > 30 or actions):
                    self.save_interaction_for_learning(text, assistant_text, actions)
            except Exception as e:
                print(f"[LEARN SAVE ERR] {e}")

            # Save to memory
            self.memory.append({"role": "user", "text": text})
            self.memory.append({"role": "assistant", "text": response["text"]})

            # Auto memory trim
            if len(self.memory) > 50:
                self.memory.pop(0)

            # Log chat
            task_manager.save_wakeup_context(text, response["text"], actions)

            db.save_chat(
                user_input=text,
                bot_response=response["text"],
                session_id=session_id,
                intent="auto",
                context={"actions": actions, "mode": mode}
            )

            # Return reply and tool results
            final_text = response["text"]
            if research_notice:
                final_text = (research_notice + "\n\n" + (final_text or "")).strip()

            # If the adapter asked for clarification, persist it so the next user message
            # can be interpreted as the answer (instead of starting an unrelated action).
            try:
                clarification = response.get("clarification") if isinstance(response, dict) else None
                if isinstance(clarification, dict):
                    kind = str(clarification.get("kind") or "").strip() or "generic"
                    question = str(clarification.get("question") or "").strip() or (final_text or "")
                    original_user_text = str(clarification.get("original_user_text") or "").strip() or (text or "")

                    if question and original_user_text:
                        from src.core.dialogue_state import get_dialogue_state_store, PendingClarification

                        ds = get_dialogue_state_store()
                        ds.save_pending(
                            session_id,
                            PendingClarification(
                                kind=kind,
                                question=question,
                                original_user_text=original_user_text,
                                created_at=time.time(),
                            ),
                        )
            except Exception:
                pass

            # Ensure intent-aware metadata is present for downstream policy and UI.
            try:
                if hasattr(self.llm, "_classify_intent_profile"):
                    profile = self.llm._classify_intent_profile(text)  # type: ignore[attr-defined]
                else:
                    profile = {}
                if isinstance(profile, dict):
                    response["intent_type"] = response.get("intent_type") or profile.get("intent_type")
                    response["intent_depth"] = response.get("intent_depth") or profile.get("intent_depth")
                    response["response_strategy"] = response.get("response_strategy") or profile.get("response_strategy")
                    response["intent"] = response.get("intent") or response.get("intent_type") or "chat"
            except Exception:
                pass

            return {
                "text": final_text,
                "actions": actions,
                "tool_results": tool_results,
                "mode": mode,
                "source": response.get("source", "openai"),
                "intent": response.get("intent", "chat"),
                "intent_type": response.get("intent_type"),
                "intent_depth": response.get("intent_depth"),
                "response_strategy": response.get("response_strategy"),
                "proactive_followup_added": bool(response.get("proactive_followup_added")),
                "user_preference_influenced": bool(response.get("user_preference_influenced")),
            }

        except Exception as e:
            err_msg = f"Error in reasoning: {e}"
            print(err_msg)
            db.save_system_event("brain_error", err_msg, "error")
            return {"text": "I'm having a processing issue, sir.", "actions": [], "mode": mode}
