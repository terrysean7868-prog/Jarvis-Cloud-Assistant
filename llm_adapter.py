# llm_adapter.py
import os
import requests
from dotenv import load_dotenv

load_dotenv()


class LLMAdapter:
    """
    Universal LLM adapter — using Groq (free, fast, OpenAI-compatible)
    Compatible with JarvisBrain's signature (system, max_tokens, etc.)
    """

    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY")
        if not self.api_key:
            raise RuntimeError("Missing GROQ_API_KEY in environment variables")

        # Default Groq model (free)
        self.model = os.getenv("GROQ_MODEL", "llama3-8b-8192")

    async def generate(
        self,
        prompt: str,
        system: str = "You are Jarvis, a helpful and intelligent AI assistant.",
        temperature: float = 0.6,
        max_tokens: int = 1024,
    ):
        """
        Generate a completion using Groq API.
        Matches OpenAI ChatCompletion style.
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

            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )

            if response.status_code != 200:
                print(f"[LLM] Groq API error {response.status_code}: {response.text}")
                return "Apologies sir, the Groq model encountered an issue."

            data = response.json()
            return data["choices"][0]["message"]["content"].strip()

        except Exception as e:
            print(f"[LLM] Groq Exception: {e}")
            return "Apologies sir, my neural interface seems unstable at the moment."
