from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel


class TelegramAuthRequest(BaseModel):
    user_id: str
    username: str
    action: str
    voice_sample_hash: str | None = None
    password: str | None = None
    role: str | None = None


def build_telegram_router(
    *,
    voice_only_mode: bool,
    telegram_bot: Any,
    brain: Any,
    executor: Any,
    env: Any,
    admin_only_action_types: set[str],
    user_explicitly_requested_screen_capture: Callable[[str], bool],
    build_web_context_from_action_results: Callable[[list[dict]], str],
    persist_web_context_items: Callable[..., None],
    web_lookup_found: Callable[[list[dict]], bool],
    continue_user_using_web_context: Callable[[str, str, str, bool], Awaitable[dict | None]],
) -> APIRouter:
    router = APIRouter(tags=["telegram"])

    @router.post("/api/telegram/register-start")
    async def telegram_register_start(req: dict):
        user_id = req.get("user_id")
        username = req.get("username")
        if not user_id or not username:
            return {"status": "error", "message": "user_id and username required"}
        return telegram_bot.start_registration(user_id, username)

    @router.post("/api/telegram/process-voice")
    async def telegram_process_voice(req: dict):
        user_id = req.get("user_id")
        voice_file_id = req.get("voice_file_id")
        voice_bytes = req.get("voice_bytes", b"")
        if not user_id or not voice_file_id:
            return {"status": "error", "message": "user_id and voice_file_id required"}
        return telegram_bot.process_voice_sample(user_id, voice_file_id, voice_bytes)

    @router.post("/api/telegram/complete-registration")
    async def telegram_complete_registration(auth_req: TelegramAuthRequest):
        if not auth_req.voice_sample_hash or not auth_req.password:
            return {"status": "error", "message": "voice_sample_hash and password required"}
        return telegram_bot.complete_registration(
            auth_req.user_id,
            auth_req.voice_sample_hash,
            auth_req.password,
            auth_req.username,
            auth_req.role or "user",
        )

    @router.post("/api/telegram/login")
    async def telegram_login(auth_req: TelegramAuthRequest):
        if not auth_req.voice_sample_hash:
            return {"status": "error", "message": "voice_sample_hash required"}
        return telegram_bot.telegram_login(
            auth_req.user_id,
            auth_req.username,
            auth_req.voice_sample_hash,
        )

    @router.post("/api/telegram/validate-session")
    async def telegram_validate_session(req: dict):
        user_id = req.get("user_id")
        if not user_id:
            return {"status": "error", "message": "user_id required"}
        is_valid, username = telegram_bot.validate_telegram_session(user_id)
        return {
            "valid": is_valid,
            "username": username,
            "user_info": telegram_bot.get_user_info(user_id),
        }

    @router.post("/api/telegram/logout")
    async def telegram_logout(req: dict):
        user_id = req.get("user_id")
        if not user_id:
            return {"status": "error", "message": "user_id required"}
        success = telegram_bot.logout_telegram_user(user_id)
        return {
            "status": "success" if success else "error",
            "message": "Logged out successfully" if success else "User not found",
        }

    @router.post("/api/telegram/chat")
    async def telegram_chat(req: dict, background_tasks: BackgroundTasks):
        if voice_only_mode:
            return {
                "status": "error",
                "message": "Voice-only mode is enabled on this assistant.",
            }

        user_id = req.get("user_id")
        text = req.get("text")
        if not user_id or not text:
            return {"status": "error", "message": "user_id and text required"}

        is_valid, username = telegram_bot.validate_telegram_session(user_id)
        if not is_valid:
            return {
                "status": "auth_required",
                "message": "Please login first",
                "action": "redirect_to_login",
            }

        response = await brain.handle_message(text, mode="chat")
        actions = response.get("actions", [])

        if actions and not user_explicitly_requested_screen_capture(text):
            actions = [a for a in actions if (a or {}).get("type") not in ("capture_screen",)]
            response["actions"] = actions

        role = ((telegram_bot.get_user_info(user_id) or {}).get("role") or "user").strip().lower()
        if role not in ("user", "admin"):
            role = "user"

        if actions:
            if role != "admin":
                blocked = [a for a in actions if (a or {}).get("type") in admin_only_action_types]
                actions = [a for a in actions if (a or {}).get("type") not in admin_only_action_types]
                response["actions"] = actions
                if blocked:
                    response["text"] = (response.get("text") or "") + "\n\n(Some actions require admin privileges and were skipped.)"

            immediate_types = {"web_search", "fetch_url", "search"}
            immediate_actions = [a for a in actions if (a or {}).get("type") in immediate_types]
            deferred_actions = [a for a in actions if (a or {}).get("type") not in immediate_types]
            if immediate_actions:
                continued_actions = None
                try:
                    tool_results = await executor.process_actions(immediate_actions, (username or "user"))
                    if False:
                        response["action_results"] = tool_results
                    mode = response.get("mode") or "chat"
                    web_ctx = build_web_context_from_action_results(tool_results)
                    persist_web_context_items(topic=text, action_results=tool_results)
                    found = web_lookup_found(tool_results)
                    if "answer" in ("append", "both"):
                        response["text"] = response.get("text") or ""
                    else:
                        continued = await continue_user_using_web_context(text, web_ctx, mode=mode, found=found)
                        if continued:
                            response["text"] = continued.get("text") or response.get("text") or ""
                            continued_actions = continued.get("actions") or []
                except Exception as e:
                    response["text"] = (response.get("text") or "") + f"\n\n(Web lookup failed: {e})"
                actions = deferred_actions
                if isinstance(continued_actions, list) and continued_actions:
                    actions = continued_actions
                response["actions"] = actions

            if actions:
                background_tasks.add_task(executor.process_actions, actions, username)

        return {
            "status": "success",
            "user_id": user_id,
            "username": username,
            "response": response.get("text", ""),
            "actions": actions,
        }

    return router
