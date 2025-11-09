# llm_adapter.py
import os
import requests
from dotenv import load_dotenv

load_dotenv()

class LLMAdapter:
    """Use Groq's free API (LLaMA 3 or Mixtral) instead of OpenAI or Gemini."""
    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY")
        self.model = os.getenv("GROQ_MODEL", "llama3-8b-8192")  # free model

    async def generate(self, prompt: str, temperature: float = 0.6):
        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "You are Jarvis, a brilliant and loyal AI assistant."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
            }
            resp = requests.post("https://api.groq.com/openai/v1/chat/completions",
                                 headers=headers, json=payload, timeout=60)
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[LLM] Groq Error: {e}")
            return "Sorry sir, I’m unable to access my neural link right now."
