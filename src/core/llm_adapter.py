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
You can return actions for automation (like open_url, search, code_update, calculate, mode_switch, fetch_news, self_update, self_add, generate_email, screen_navigation, capture_screen, open_app, close_app, switch_app, execute_command, create_task, stop_task, check_errors, check_render_logs, etc.)
Use JSON format strictly when actions are needed.
If user asks to update, add, or edit code/files, use self_update or self_add action types.
If user asks to generate email or send mail, use generate_email action type.
If user asks to interact with screen, navigate, click, type, or capture screen, use screen_navigation or capture_screen action types.
If user asks to open/close/switch applications, use open_app, close_app, or switch_app action types.
If user asks to run commands or execute tasks on PC, use execute_command or create_task action types.
If user says "stop" or wants to stop current operation, use stop_task action type.
If user asks to check errors or fix issues, use check_errors or check_render_logs action types.
You can perform real operations on the user's PC - open apps, run commands, manage files, etc.

Style tone: {tone}.
"""

        user_prompt = f"""
User said: "{text}"
Recent context:
{context[-400:] if context else '(none)'}
Return valid JSON like:
{{
  "text": "<spoken reply, humanlike>",
  "actions": [{{"type": "<action_type>", "value": "<info>"}}]
}}
If no action required, keep "actions": [].
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
