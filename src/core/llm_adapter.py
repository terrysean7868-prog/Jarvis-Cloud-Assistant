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
        )
        needs_internet = bool(re.search(r"\b(latest|today|current|202\d|news|price|release|docs|documentation)\b", tl))

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
    def _postprocess_pc_settings_actions(user_text: str, parsed: dict) -> dict:
        """Best-effort helper: open PC Settings pages safely.

        We do NOT directly change OS/security-critical configuration.
        We only open the relevant Settings page (primarily Windows via ms-settings:)
        and keep the rest as user-guided steps.
        """
        actions = parsed.get("actions") or []
        if not isinstance(actions, list):
            actions = []

        # If model already emitted a settings opener, don't add duplicates.
        if any(isinstance(a, dict) and a.get("type") in ("open_app", "execute_command") for a in actions):
            parsed["actions"] = actions
            return parsed

        t = (user_text or "").strip().lower()
        if not t:
            parsed["actions"] = actions
            return parsed

        wants_settings = bool(re.search(r"\b(settings|configuration|configure|wifi|wi-?fi|bluetooth|sound|volume|display|screen|notification)\b", t))
        if not wants_settings:
            parsed["actions"] = actions
            return parsed

        # Only auto-open common *low-risk* settings pages.
        # We intentionally avoid updates, recovery, security, firewall, registry, disk, etc.
        ms_uri = None
        if re.search(r"\b(bluetooth|bt)\b", t):
            ms_uri = "ms-settings:bluetooth"
        elif re.search(r"\b(wi-?fi|wifi|wireless)\b", t):
            ms_uri = "ms-settings:network-wifi"
        elif re.search(r"\b(sound|volume|speaker|microphone|audio)\b", t):
            ms_uri = "ms-settings:sound"
        elif re.search(r"\b(display|resolution|scale|scaling|brightness|screen)\b", t):
            ms_uri = "ms-settings:display"
        elif re.search(r"\b(notification|notifications|do not disturb|focus)\b", t):
            ms_uri = "ms-settings:notifications"

        if ms_uri:
            # Using cmd's start with an empty title is the most reliable form.
            actions.append({"type": "execute_command", "command": f'start "" "{ms_uri}"', "wait": False})
            parsed["actions"] = actions

            base_text = (parsed.get("text") or "").strip()
            tip = "I opened the relevant Settings page. Tell me what you want to change and I’ll guide the safest steps."
            if tip not in base_text:
                parsed["text"] = (base_text + "\n\n" + tip).strip() if base_text else tip
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

    async def generate_response(self, text: str, context: str = "", mode="chat", capabilities=None):
        """Generate a rich, humanlike structured response."""
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
    - If you need facts, definitions, docs, or "latest" information, emit web_search (and optionally fetch_url) actions first.
    - Only ask at most 1 clarifying question, only if it requires private/user-specific info that cannot be searched.

Output rules:
- Return ONLY valid JSON.
- JSON must be an object: {{"text": string, "actions": array}}.
- If no action is needed, use an empty array.

    Current allowed capabilities (soft constraint): {caps_str if caps_str else '(not specified)'}

Allowed action types (only include required fields):
- open_url: {{"type":"open_url","url":"https://..."}}
- web_search: {{"type":"web_search","query":"...","num_results":5}}
- fetch_url: {{"type":"fetch_url","url":"https://..."}}
- generate_email: {{"type":"generate_email","recipient":"...","subject":"...","body_prompt":"...","tone":"professional"}}
- open_app: {{"type":"open_app","app_name":"...","args":[]}}
- close_app: {{"type":"close_app","app_name":"..."}}
- switch_app: {{"type":"switch_app","app_name":"..."}}
- execute_command: {{"type":"execute_command","command":"...","wait":true}}
- type_text: {{"type":"type_text","text":"...","interval":0.02}}
- press_key: {{"type":"press_key","key":"enter","presses":1}}

Safety rules for actions:
- Prefer fewer actions.
- If the user asks to open an app and write content, include BOTH open_app and type_text (and press_key only if needed).
- Never output actions that can damage or remove the OS (e.g., formatting disks, disk partition tools, deleting system folders, boot/registry edits). If asked, refuse and return no such actions.
- For PC Settings/configuration requests: you may open the relevant settings page (e.g., Windows ms-settings: URIs) and provide safe step-by-step guidance, but do NOT apply security-critical/system-destructive changes.
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

            # Fallback humanlike reply
            fallback_replies = [
                "I'm thinking it through, one moment please.",
                "Let me process that for you, sir.",
                "I’m analyzing the best response — hang tight!",
                "Working on it, boss."
            ]
            return {"text": random.choice(fallback_replies), "actions": [], "source": "fallback"}

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
        is_text_editor = any(k in app_name for k in ("notepad", "wordpad", "textedit"))
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
        m = re.search(r"\b(?:write|type|draft|compose|create|make)\b\s*(.*)", t, re.IGNORECASE)
        topic = (m.group(1).strip() if m else "")
        if not topic:
            topic = t

        return (
            "Draft:\n"
            f"{topic}\n"
        )
