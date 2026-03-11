from __future__ import annotations

from typing import Any

from src.agents.base_agent import BaseAgent
from src.utils.db import db


class DeviceAgent(BaseAgent):
    name = "DeviceAgent"

    def __init__(self, device_hub: Any):
        self.device_hub = device_hub

    async def plan(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "agent": self.name,
            "strategy": "multi_device_dispatch",
            "steps": ["inspect registry", "pick target devices", "dispatch actions"],
            "task": task,
        }

    async def execute(self, task: dict[str, Any]) -> dict[str, Any]:
        agents = await self.device_hub.list_agents()
        registry = []
        try:
            db._ensure_connected()
            if db.db is not None:
                rows = list(db.db["device_registry"].find({}, {"_id": 0}).limit(100))
                registry = rows
        except Exception:
            registry = []

        return {
            "status": "success",
            "agent": self.name,
            "connected_devices": list(agents.keys()),
            "count": len(agents),
            "registry": registry,
        }

    async def evaluate(self, task: dict[str, Any], execution_result: dict[str, Any]) -> dict[str, Any]:
        ok = str(execution_result.get("status") or "").lower() == "success"
        return {"agent": self.name, "task_id": task.get("task_id"), "status": "pass" if ok else "fail"}
