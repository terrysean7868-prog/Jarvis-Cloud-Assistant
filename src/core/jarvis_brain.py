# src/core/jarvis_brain.py
import asyncio
from datetime import datetime
from src.utils.db import db
from src.core.llm_adapter import LLMAdapter

class JarvisBrain:
    """Enhanced brain with memory context, reasoning, and humanlike tone."""

    def __init__(self, llm: LLMAdapter, user_id="default"):
        self.llm = llm
        self.user_id = user_id
        self.memory = []
        self.last_mode = "interactive"

    async def handle_message(self, text: str, mode="chat", user_id=None):
        """Main conversational + action reasoning pipeline."""
        session_id = user_id or self.user_id
        context = "\n".join([f"{m['role']}: {m['text']}" for m in self.memory[-6:]])

        try:
            response = await self.llm.generate_response(
                text, context=context, mode=mode, capabilities=["open_url", "search", "calculate", "news", "mode_switch"]
            )

            # Save to memory
            self.memory.append({"role": "user", "text": text})
            self.memory.append({"role": "assistant", "text": response["text"]})

            # Auto memory trim
            if len(self.memory) > 50:
                self.memory.pop(0)

            # Log chat
            db.save_chat(
                user_input=text,
                bot_response=response["text"],
                session_id=session_id,
                intent="auto",
                context={"actions": response.get("actions", []), "mode": mode}
            )

            return {
                "text": response["text"],
                "actions": response.get("actions", []),
                "mode": mode,
                "source": response.get("source", "openai")
            }

        except Exception as e:
            err_msg = f"Error in reasoning: {e}"
            print(err_msg)
            db.save_system_event("brain_error", err_msg, "error")
            return {"text": "I'm having a processing issue, sir.", "actions": [], "mode": mode}
