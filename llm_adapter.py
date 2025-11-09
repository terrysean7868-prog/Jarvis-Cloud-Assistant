# llm_adapter.py
import os
import requests
from dotenv import load_dotenv

load_dotenv()

class LLMAdapter:
    """
    Jarvis Cloud - LLM Adapter for Groq (free)
    Compatible with JarvisBrain expected args: system, max_tokens, temperature
    """

    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY")
        if not self.api_key:
            raise RuntimeError("Missing GROQ_API_KEY in environment")

        # Default free Groq model
        self.model = os.getenv("GROQ_MODEL", "llama3-8b-8192")

    async def generate(
        self,
        prompt: str,
        system: str = "You are Jarvis, an intelligent and loyal AI assistant.",
        max_tokens: int = 2048,
        temperature: float = 0.6,
    ):
        """
        Generate a response via Groq’s OpenAI-compatible API
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
                print(f"[LLM] Groq API Error {resp.status_code}: {resp.text}")
                return f"Apologies sir, Groq API returned an error: {resp.status_code}"

            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

        except Exception as e:
            print(f"[LLM] Exception: {e}")
            return f"Apologies sir, I encountered an internal issue: {str(e)}"
