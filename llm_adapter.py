# llm_adapter.py
import os
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

        # ✅ Use updated supported model
        self.model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

    async def generate(
        self,
        prompt: str,
        system: str = "You are Jarvis, a loyal and intelligent AI assistant.",
        max_tokens: int = 2048,
        temperature: float = 0.6,
    ):
        """
        Generate chat completion with Groq API.
        Returns dict with 'text' key for JarvisBrain compatibility.
        """
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
                    "text": f"Apologies sir, the Groq API returned an error ({resp.status_code}).",
                    "actions": [],
                }

            data = resp.json()
            text_out = data["choices"][0]["message"]["content"].strip()

            # Return in expected Jarvis format
            return {"text": text_out, "actions": []}

        except Exception as e:
            print(f"[LLM] Groq Exception: {e}")
            return {
                "text": f"Apologies sir, I encountered an internal issue: {str(e)}",
                "actions": [],
            }
