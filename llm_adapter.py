# llm_adapter.py
import os
import re
import json
import requests
from dotenv import load_dotenv

load_dotenv()

class LLMAdapter:
    """
    Jarvis Cloud - LLM Adapter for Groq (Free)
    Returns dicts compatible with JarvisBrain:
    { "text": "...", "actions": [] }
    """

    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY")
        if not self.api_key:
            raise RuntimeError("Missing GROQ_API_KEY in environment")

        # ✅ Use updated supported Groq model
        self.model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

    async def generate(
        self,
        prompt: str,
        system: str = "You are Jarvis, a loyal and intelligent AI assistant.",
        max_tokens: int = 2048,
        temperature: float = 0.6,
    ):
        """Generate chat completion via Groq API and return {text, actions}"""
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )

            if resp.status_code != 200:
                print(f"[LLM] Groq API error {resp.status_code}: {resp.text}")
                return {
                    "text": f"Apologies sir, Groq returned an error ({resp.status_code}).",
                    "actions": [],
                }

            data = resp.json()
            text_out = data["choices"][0]["message"]["content"].strip()

            actions = self.parse_actions_from_text(text_out)

            return {"text": text_out, "actions": actions}

        except Exception as e:
            print(f"[LLM] Groq Exception: {e}")
            return {
                "text": f"Apologies sir, I encountered an internal issue: {str(e)}",
                "actions": [],
            }

    def parse_actions_from_text(self, text: str):
        """
        Extract { "actions": [...] } blocks from model output text safely.
        Example:
        text = "Here’s the update {\"actions\": [{\"type\":\"write\",\"path\":\"file.py\"}]}"
        """
        actions = []
        try:
            matches = re.findall(r"\{[\s\S]*?\"actions\"[\s\S]*?\}", text)
            for m in matches:
                try:
                    obj = json.loads(m)
                    if isinstance(obj, dict) and "actions" in obj:
                        actions.extend(obj["actions"])
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            print(f"[LLM] parse_actions_from_text error: {e}")
        return actions
