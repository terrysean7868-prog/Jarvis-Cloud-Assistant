# src/core/jarvis_brain.py
import asyncio
from datetime import datetime
from src.utils.db import db
from src.core.llm_adapter import LLMAdapter
from src.utils.task_manager import task_manager

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
            # Check if this is a self-update command
            text_lower = text.lower()
            is_self_update = any(keyword in text_lower for keyword in [
                "update", "modify", "improve", "edit", "change", "add", "create", "make", "build"
            ]) and any(keyword in text_lower for keyword in [
                "file", "module", "component", "code", "system", "bot", "jarvis"
            ])
            
            capabilities = ["open_url", "search", "calculate", "news", "mode_switch"]
            if is_self_update:
                capabilities.append("self_update")
                capabilities.append("self_add")
            
            response = await self.llm.generate_response(
                text, context=context, mode=mode, capabilities=capabilities
            )

            # Save to memory
            self.memory.append({"role": "user", "text": text})
            self.memory.append({"role": "assistant", "text": response["text"]})

            # Save context for wakeup command
            task_manager.save_wakeup_context(text, response["text"], response.get("actions", []))

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
