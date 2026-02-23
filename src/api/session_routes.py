from __future__ import annotations

from typing import Any

from fastapi import APIRouter


def build_session_router(session_manager: Any) -> APIRouter:
    router = APIRouter(tags=["session"])

    @router.post("/api/session/extend")
    async def extend_session_endpoint(req: dict):
        session_id = req.get("session_id")
        if not session_id:
            return {"status": "error", "message": "session_id required"}

        is_valid, username = session_manager.validate_session(session_id, update_activity=True)
        if not is_valid:
            return {
                "status": "session_expired",
                "message": "Session expired. Please login again.",
                "action": "redirect_to_login",
            }

        extended = session_manager.extend_session(session_id)
        return {
            "status": "success" if extended else "error",
            "message": "Session extended" if extended else "Failed to extend session",
            "username": username,
            "session_info": session_manager.get_session_info(session_id),
        }

    @router.post("/api/session/check")
    async def check_session_endpoint(req: dict):
        session_id = req.get("session_id")
        if not session_id:
            return {"valid": False, "message": "No session_id provided"}

        is_valid, username = session_manager.validate_session(session_id, update_activity=False)
        return {
            "valid": is_valid,
            "username": username,
            "session_info": session_manager.get_session_info(session_id) if is_valid else None,
        }

    @router.post("/api/session/logout")
    async def logout_session_endpoint(req: dict):
        session_id = req.get("session_id")
        if not session_id:
            return {"status": "error", "message": "session_id required"}

        success = session_manager.invalidate_session(session_id)
        return {
            "status": "success" if success else "error",
            "message": "Logged out successfully" if success else "Session not found",
        }

    @router.get("/api/session/stats")
    async def get_session_stats():
        return session_manager.get_session_stats()

    return router
