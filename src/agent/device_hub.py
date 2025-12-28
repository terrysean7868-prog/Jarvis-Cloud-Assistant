import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class AgentConnection:
    device_id: str
    websocket: WebSocket
    connected_at: str
    last_seen_at: str
    capabilities: Dict[str, Any]


class DeviceHub:
    """In-memory hub for a single-process deployment.

    NOTE: This requires a single web process/worker. If you scale horizontally or
    run multiple gunicorn workers, you need a shared broker (Redis) instead.
    """

    def __init__(self, shared_secret: str):
        self._shared_secret = shared_secret
        self._lock = asyncio.Lock()
        self._agents: Dict[str, AgentConnection] = {}

    async def register(self, device_id: str, secret: str, websocket: WebSocket, capabilities: Optional[Dict[str, Any]] = None) -> None:
        if not self._shared_secret:
            raise PermissionError("JARVIS_AGENT_SHARED_SECRET is not set")
        if secret != self._shared_secret:
            raise PermissionError("Invalid agent secret")
        if not device_id:
            raise PermissionError("Missing device_id")

        now = datetime.utcnow().isoformat()
        async with self._lock:
            self._agents[device_id] = AgentConnection(
                device_id=device_id,
                websocket=websocket,
                connected_at=now,
                last_seen_at=now,
                capabilities=capabilities or {},
            )

    async def get_agent(self, device_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            conn = self._agents.get(device_id)
            if not conn:
                return None
            return {
                "device_id": device_id,
                "connected_at": conn.connected_at,
                "last_seen_at": conn.last_seen_at,
                "capabilities": conn.capabilities or {},
            }

    async def unregister(self, device_id: str) -> None:
        async with self._lock:
            self._agents.pop(device_id, None)

    async def touch(self, device_id: str) -> None:
        now = datetime.utcnow().isoformat()
        async with self._lock:
            conn = self._agents.get(device_id)
            if conn:
                conn.last_seen_at = now

    async def update_capabilities(self, device_id: str, capabilities: Dict[str, Any]) -> None:
        async with self._lock:
            conn = self._agents.get(device_id)
            if conn:
                conn.capabilities = capabilities or {}

    async def is_connected(self, device_id: str) -> bool:
        async with self._lock:
            return device_id in self._agents

    async def send_job(self, device_id: str, job: Dict[str, Any]) -> None:
        """Send a job to the connected agent."""
        async with self._lock:
            conn = self._agents.get(device_id)
        if not conn:
            raise RuntimeError(f"Agent not connected: {device_id}")

        await conn.websocket.send_text(json.dumps({"type": "job", **job}))

    async def list_agents(self) -> Dict[str, Dict[str, Any]]:
        async with self._lock:
            return {
                device_id: {
                    "device_id": device_id,
                    "connected_at": conn.connected_at,
                    "last_seen_at": conn.last_seen_at,
                    "capabilities": conn.capabilities or {},
                }
                for device_id, conn in self._agents.items()
            }
