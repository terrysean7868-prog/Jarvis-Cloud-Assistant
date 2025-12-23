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

    async def _call_openai(self, messages):
        """Call OpenAI API directly."""
        await self._ensure_session()
        headers = {
            "Authorization": f"Bearer {self.primary_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.primary_model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 400,
        }
        async with self.session.post(self.primary_endpoint, json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"OpenAI API error: {await r.text()}")
            return await r.json()

    async def generate_response(self, text: str, context: str = "", mode="chat", capabilities=None):
        """Generate a rich, humanlike structured response."""
        persona = self.personality.get(self.persona, self.personality["formal-gentle"])
        prefix = persona["prefix"]
        tone = persona["tone"]

        system_prompt = f"""
You are Jarvis, an advanced AI assistant — intelligent, emotional, and deeply loyal.
You respond like a real human: warm, contextual, confident, and witty when appropriate.

You are aware of your capabilities: {', '.join(capabilities or ['basic chat'])}.

      You can return actions for automation.

      ==========================
      ACTION SCHEMA (STRICT)
      ==========================
      Return JSON only with keys: "text" and "actions".

      Each action MUST be a JSON object with a "type" field and ONLY the fields required for that type.

      Allowed action types and required fields:

      1) Browser / web
      - open_url: {{"type":"open_url","url":"https://..."}}
      - web_search: {{"type":"web_search","query":"...","num_results":5}}
      - fetch_url: {{"type":"fetch_url","url":"https://..."}}

      2) Email
      - generate_email: {{"type":"generate_email","recipient":"...","subject":"...","body_prompt":"...","tone":"professional"}}

      3) Apps (runs on the user's PC via agent when deployed)
      - open_app: {{"type":"open_app","app_name":"notepad","args":[]}}
      - close_app: {{"type":"close_app","app_name":"chrome"}}
      - switch_app: {{"type":"switch_app","app_name":"vscode"}}

      4) Command execution (runs on the user's PC via agent when deployed)
      - execute_command: {{"type":"execute_command","command":"...","wait":true}}

      5) Filesystem (runs on the user's PC via agent when deployed)
      IMPORTANT: Use ONLY project-relative paths (no absolute paths). Never touch secrets (.env, keys) or system directories.
      - read:   {{"type":"read","path":"docs/README.md"}}
      - list:   {{"type":"list","path":"src"}}
      - mkdir:  {{"type":"mkdir","path":"docs/new_folder"}}
      - write:  {{"type":"write","path":"docs/notes.txt","content":"..."}}
      - edit:   {{"type":"edit","path":"src/core/x.py","content":"<full new file content>"}}
      - delete: {{"type":"delete","path":"docs/old.txt"}}
      - move:   {{"type":"move","path":"docs/a.txt","dest":"docs/archive/a.txt"}}
      - copy:   {{"type":"copy","source":"docs/a.txt","destination":"docs/b.txt"}}
      - cleanup: {{"type":"cleanup"}}  (cleans caches like __pycache__ etc)

      6) Tasks
      - create_task: {{"type":"create_task","description":"...","steps":[...],"priority":5}}
      - stop_task: {{"type":"stop_task"}}

      7) Diagnostics
      - check_errors: {{"type":"check_errors"}}
      - check_render_logs: {{"type":"check_render_logs"}}

      Rules:
      - If you are not confident an action is safe or correct, do NOT emit it; ask a clarifying question instead.
      - Prefer fewer actions; never spam actions.
      - For file edits: output the COMPLETE file content (no diffs).

      Response format:
      {{
        "text": "<humanlike reply>",
        "actions": [ ... ]
      }}

==========================
⭐ **MCP TOOL CALLING (NEW)**
==========================
When working with Jarvis internal code, modules, system logic or filesystem,
use actions with type "mcp_tool".

Examples you MUST follow:

1) Patch a file:
{{
  "type": "mcp_tool",
  "tool": "patch_file",
  "args": {{
    "path": "src/core/jarvis_brain.py",
    "search": "old text",
    "replace": "new text"
  }}
}}

2) Write or overwrite a file:
{{
  "type": "mcp_tool",
  "tool": "write_file",
  "args": {{
    "path": "src/core/new_module.py",
    "content": "<python code>"
  }}
}}

3) Create a new file:
{{
  "type": "mcp_tool",
  "tool": "create_file",
  "args": {{
    "path": "src/utils/new_feature.py",
    "content": "..."
  }}
}}

4) Run a shell command:
{{
  "type": "mcp_tool",
  "tool": "run_command",
  "args": {{
    "cmd": "ls -la"
  }}
}}

5) Commit changes:
{{
  "type": "mcp_tool",
  "tool": "git_commit",
  "args": {{
    "msg": "Improved logic"
  }}
}}

Always return valid JSON.
If no action needed, return "actions": [].

Your response format:
{{
  "text": "<humanlike spoken reply>",
  "actions": [ ... ]
}}
==========================

Style tone: {tone}.
"""

        user_prompt = f"""
User said: "{text}"

Context:
{context[-400:] if context else '(none)'}

Return JSON only:
{{
  "text": "...",
  "actions": [{{"type": "...", "tool": "...", "args": {{...}} }}]
}}
"""

        try:
            start = datetime.utcnow()
            response = await self._call_openai([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ])
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
