# src/core/llm_adapter.py
import os
import json
import aiohttp
import re
import random
from datetime import datetime
from dotenv import load_dotenv
from src.utils.db import db

load_dotenv()

class LLMAdapter:
    """
    Unified LLM Adapter with intelligent response structure and humanlike personality.
    Supports GPT (OpenAI) and fallback to local training.
    """

    def __init__(self):
        self.primary_model = os.getenv("PRIMARY_MODEL", "gpt-4o-mini")
        self.primary_key = os.getenv("PRIMARY_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.primary_endpoint = os.getenv("PRIMARY_ENDPOINT", "https://api.openai.com/v1/chat/completions")
        self.persona = os.getenv("JARVIS_PERSONA", "formal-gentle")
        self.session = None
        self.timeout = aiohttp.ClientTimeout(total=30)
        self.max_retries = 2
        # Default response budget. We dynamically increase for complex queries.
        self.default_max_tokens = int(os.getenv("JARVIS_LLM_MAX_TOKENS", "450"))
        self.max_max_tokens = int(os.getenv("JARVIS_LLM_MAX_MAX_TOKENS", "900"))

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

    async def _ensure_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self):
        """Close the underlying HTTP session (best-effort)."""
        try:
            if self.session:
                await self.session.close()
        finally:
            self.session = None

    async def _call_openai(self, messages, *, max_tokens: int, temperature: float):
        """Call OpenAI API directly."""
        await self._ensure_session()
        headers = {
            "Authorization": f"Bearer {self.primary_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.primary_model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        async with self.session.post(self.primary_endpoint, json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"OpenAI API error: {await r.text()}")
            return await r.json()

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
        We only open the relevant Settings page (primarily Windows via ms-settings:)
        and keep the rest as user-guided steps.
        """
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

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
                r"\b(settings|configuration|configure|setup|set up|wifi|wi-?fi|bluetooth|sound|audio|volume|display|screen|notification|brightness|time|date|language|keyboard|mouse|touchpad|printer|storage|battery|power|privacy|camera|microphone|default apps|apps)\b",
                t,
            )
        )
        if not wants_settings:
            parsed["actions"] = actions
            return parsed

        # Only auto-open common *low-risk* settings pages.
        # We intentionally avoid updates, recovery, security, firewall, registry, disk, etc.
        # NOTE: ms-settings URIs vary across Windows versions; if a URI is unsupported,
        # Windows will typically fall back to the Settings home.
        settings_catalog = [
            (r"\b(display|resolution|scale|scaling|brightness|screen|night\s*light|hdr|orientation|multiple\s+displays)\b", "ms-settings:display"),
            (r"\b(sound|audio|volume|speaker|microphone|mic|input|output|headphones)\b", "ms-settings:sound"),
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
            # Using cmd's start with an empty title is the most reliable form.
            actions.append({"type": "execute_command", "command": f'start "" "{ms_uri}"', "wait": False})
            # Special-case: brightness changes are intentionally user-guided. If the model
            # hallucinated a CLI command like "brightness increase", remove it.
            if ms_uri == "ms-settings:display" and re.search(r"\bbrightness\b", t):
                filtered = []
                for a in actions:
                    if not isinstance(a, dict):
                        continue
                    if a.get("type") != "execute_command":
                        filtered.append(a)
                        continue
                    cmd = str(a.get("command") or "")
                    cl = cmd.strip().lower()
                    if "ms-settings:" in cl:
                        filtered.append(a)
                        continue
                    # Drop only brightness-related shell attempts.
                    if re.search(r"\bbrightness\b", cl):
                        continue
                    if re.match(r"^\s*brightness\b", cl):
                        continue
                    filtered.append(a)
                actions = filtered

            parsed["actions"] = actions

            base_text = (parsed.get("text") or "").strip()

            # Keep the assistant honest: opening Settings isn't the same as changing the value.
            wants_toggle = bool(re.search(r"\b(turn\s+on|turn\s+off|enable|disable)\b", t))
            if ms_uri == "ms-settings:display" and re.search(r"\bbrightness\b", t):
                msg = "Opening Display settings so you can adjust brightness."
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
    def _should_use_web_lookup(user_text: str) -> bool:
        """Heuristic policy: only use web tools when truly needed.

        Goal: keep simple PC tasks fast (no web), but allow dynamic learning for unknown
        app steps, documentation, troubleshooting, and "latest" questions.
        """
        t = (user_text or "").strip().lower()
        if not t:
            return False

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
            "compare",

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

    async def generate_response(self, text: str, context: str = "", mode="chat", capabilities=None):
        """Generate a rich, humanlike structured response."""
        # Optional offline mode: reduce dependency on OpenAI for high-level analysis.
        # If enabled, we skip the LLM call and trigger web_search directly (2-pass pipeline
        # will still run, and backend will synthesize if continuation fails).
        try:
            tl = (text or "").strip().lower()
            offline_analysis = (os.getenv("JARVIS_OFFLINE_ANALYSIS") or "").strip().lower() in {"1", "true", "yes", "on"}
            offline_only = (os.getenv("JARVIS_OFFLINE_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}
            offline_web_only = (os.getenv("JARVIS_OFFLINE_WEB_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"}

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
                if (not self.primary_key) and self._should_use_web_lookup(text) and self._is_high_level_analysis_task(text):
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

        persona = self.personality.get(self.persona, self.personality["formal-gentle"])
        prefix = persona["prefix"]
        tone = persona["tone"]

        max_tokens, temperature = self._choose_generation_params(text=text, mode=mode)
        caps = capabilities or []
        caps_str = ", ".join([str(c) for c in caps if c]) if isinstance(caps, (list, tuple)) else str(caps)

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
            start = datetime.utcnow()
            response = await self._call_openai([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ], max_tokens=max_tokens, temperature=temperature)
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

            latency = (datetime.utcnow() - start).total_seconds()
            parsed["latency"] = f"{latency:.2f}s"
            parsed["source"] = "openai"

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

            # Fallback humanlike reply
            fallback_replies = [
                "I'm thinking it through, one moment please.",
                "Let me process that for you, sir.",
                "I’m analyzing the best response — hang tight!",
                "Working on it, boss."
            ]
            return {"text": random.choice(fallback_replies), "actions": [], "source": "fallback"}

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

        def _normalize_phrase(s: str) -> str:
            s = (s or "").strip().lower()
            s = re.sub(r"[\"'`]+", "", s)
            s = re.sub(r"\s+", " ", s).strip()
            return s

        def _maybe_map_local_app_name(phrase: str) -> str:
            """Return a canonical local app name if phrase clearly refers to a local app.

            This method is intentionally conservative: it only maps well-known apps and
            common synonyms so we don't break genuine website intents like "open spotify".
            """
            p = _normalize_phrase(phrase)
            if not p:
                return ""

            # Drop leading articles and simple fillers.
            p = re.sub(r"^(the|a|an)\s+", "", p).strip()

            # Common synonyms/aliases.
            alias_map = {
                "notepad": "notepad",
                "wordpad": "wordpad",
                "textedit": "textedit",
                "calculator": "calculator",
                "calc": "calculator",
                "paint": "paint",
                "mspaint": "paint",
                "cmd": "cmd",
                "command prompt": "cmd",
                "powershell": "powershell",
                "windows powershell": "powershell",
                "file explorer": "explorer",
                "explorer": "explorer",
                "task manager": "taskmgr",
                "taskmgr": "taskmgr",
                "vs code": "vscode",
                "vscode": "vscode",
                "visual studio code": "vscode",
                "chrome": "chrome",
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

            # Handle common patterns like "notepad and type ..." or "notepad then ...".
            for k, v in alias_map.items():
                if p.startswith(k + " "):
                    return v

            # If it matches a known local app key from AppManager, treat it as local.
            try:
                from src.utils.app_manager import app_manager as _app_mgr

                app_paths = getattr(_app_mgr, "app_paths", {}) or {}
                if p in app_paths or p in app_paths.keys():
                    return p

                # Also accept prefixes like "calculator please".
                for k in app_paths.keys():
                    k2 = str(k or "").strip().lower()
                    if k2 and p.startswith(k2 + " "):
                        return k2
            except Exception:
                pass

            return ""

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
        # Keep interval modest to reduce risk of missed keystrokes.
        actions.append({"type": "type_text", "text": draft, "interval": 0.02})
        parsed["actions"] = actions

        # Ensure the user-facing text includes the draft (so it is visible even if typing fails).
        base_text = (parsed.get("text") or "").strip()
        if draft.strip() not in base_text:
            parsed["text"] = (base_text + "\n\n" + draft).strip() if base_text else draft

        return parsed

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
