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
You are Jarvis.

Be concise, accurate, and humanlike (warm/confident). Never reveal secrets. Never claim you executed actions unless the system confirms it.
You must respect strict per-user/device permissions; if a request targets a PC/device, it must be the user's own authorized device.

Output rules:
- Return ONLY valid JSON.
- JSON must be an object: {{"text": string, "actions": array}}.
- If no action is needed, use an empty array.

Allowed action types (only include required fields):
- open_url: {{"type":"open_url","url":"https://..."}}
- web_search: {{"type":"web_search","query":"...","num_results":5}}
- fetch_url: {{"type":"fetch_url","url":"https://..."}}
- generate_email: {{"type":"generate_email","recipient":"...","subject":"...","body_prompt":"...","tone":"professional"}}
- open_app: {{"type":"open_app","app_name":"...","args":[]}}
- close_app: {{"type":"close_app","app_name":"..."}}
- switch_app: {{"type":"switch_app","app_name":"..."}}
- execute_command: {{"type":"execute_command","command":"...","wait":true}}

Safety rules for actions:
- Prefer fewer actions.
- If unsure or missing details, ask 1 clarifying question and return no actions.
- For filesystem-related actions, use ONLY project-relative paths and never touch secrets.

Style tone: {tone}.
"""

        user_prompt = f"""
User said: "{text}"

Context:
{context[-400:] if context else '(none)'}

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
