import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket

from src.config.settings import settings
from src.utils.db import db

try:
    from src.broker.redis_broker import RedisBroker
except Exception:
    RedisBroker = None  # type: ignore

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

    def __init__(self, shared_secret: str, *, broker: Any | None = None, instance_id: str | None = None):
        self._shared_secret = shared_secret
        self._lock = asyncio.Lock()
        self._agents: Dict[str, AgentConnection] = {}

        # Optional broker for multi-instance routing.
        self._broker = broker
        self._instance_id = (instance_id or settings.instance_id).strip()
        self._listener_task: asyncio.Task | None = None
        self._registry_ttl_seconds = 60

    def attach_broker(self, broker: Any) -> None:
        self._broker = broker

    def start_broker_listener(self) -> None:
        """Listen for jobs destined for this instance and forward to connected agents."""
        if not self._broker:
            return
        if self._listener_task and not self._listener_task.done():
            return

        channel = f"agent_jobs:{self._instance_id}"

        async def _on_message(msg: dict) -> None:
            try:
                job = msg.get("job")
                device_id = (msg.get("device_id") or "").strip().lower()
                if not device_id or not isinstance(job, dict):
                    return
                # Forward only if the agent is connected on this instance.
                async with self._lock:
                    conn = self._agents.get(device_id)
                if not conn:
                    return
                await conn.websocket.send_text(json.dumps({"type": "job", **job}))
            except Exception:
                return

        try:
            self._listener_task = self._broker.start_listener(channel, _on_message, task_name="broker-agent-jobs")
        except Exception:
            self._listener_task = None

    async def shutdown(self) -> None:
        try:
            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
        except Exception:
            pass

    async def _set_registry(self, device_id: str, conn: AgentConnection) -> None:
        """Persist device->instance mapping in broker so dispatch can route across workers."""
        try:
            db._ensure_connected()
            if db.db is not None:
                db.db["device_registry"].update_one(
                    {"device_id": device_id},
                    {
                        "$set": {
                            "device_id": device_id,
                            "instance_id": self._instance_id,
                            "connected": True,
                            "connected_at": conn.connected_at,
                            "last_seen_at": conn.last_seen_at,
                            "capabilities": conn.capabilities or {},
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                    upsert=True,
                )
        except Exception:
            pass

        if not self._broker:
            return
        try:
            key = f"agent_registry:{device_id}"
            await self._broker.set_json(
                key,
                {
                    "device_id": device_id,
                    "instance_id": self._instance_id,
                    "connected_at": conn.connected_at,
                    "last_seen_at": conn.last_seen_at,
                    "capabilities": conn.capabilities or {},
                },
                ttl_seconds=self._registry_ttl_seconds,
            )
        except Exception:
            return

    async def _del_registry(self, device_id: str) -> None:
        try:
            db._ensure_connected()
            if db.db is not None:
                db.db["device_registry"].update_one(
                    {"device_id": device_id},
                    {
                        "$set": {
                            "connected": False,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                    upsert=True,
                )
        except Exception:
            pass

        if not self._broker:
            return
        try:
            await self._broker.delete(f"agent_registry:{device_id}")
        except Exception:
            pass

    async def _get_registry(self, device_id: str) -> Optional[dict]:
        if not self._broker:
            return None
        try:
            return await self._broker.get_json(f"agent_registry:{device_id}")
        except Exception:
            return None

    async def register(self, device_id: str, secret: str, websocket: WebSocket, capabilities: Optional[Dict[str, Any]] = None) -> None:
        if not self._shared_secret:
            raise PermissionError("JARVIS_AGENT_SHARED_SECRET is not set")
        if secret != self._shared_secret:
            raise PermissionError("Invalid agent secret")
        if not device_id:
            raise PermissionError("Missing device_id")

        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._agents[device_id] = AgentConnection(
                device_id=device_id,
                websocket=websocket,
                connected_at=now,
                last_seen_at=now,
                capabilities=capabilities or {},
            )

        # Best-effort multi-instance registry.
        try:
            async with self._lock:
                conn = self._agents.get(device_id)
            if conn:
                await self._set_registry(device_id, conn)
        except Exception:
            pass

    async def register_token(self, device_id: str, websocket: WebSocket, capabilities: Optional[Dict[str, Any]] = None) -> None:
        """Register an agent using a verified server-issued JWT token.

        No shared secret is required for this path.
        """
        if not device_id:
            raise PermissionError("Missing device_id")

        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._agents[device_id] = AgentConnection(
                device_id=device_id,
                websocket=websocket,
                connected_at=now,
                last_seen_at=now,
                capabilities=capabilities or {},
            )

        try:
            async with self._lock:
                conn = self._agents.get(device_id)
            if conn:
                await self._set_registry(device_id, conn)
        except Exception:
            pass

    async def get_agent(self, device_id: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            conn = self._agents.get(device_id)
            if conn:
                return {
                    "device_id": device_id,
                    "connected_at": conn.connected_at,
                    "last_seen_at": conn.last_seen_at,
                    "capabilities": conn.capabilities or {},
                }

        # Multi-instance: return registry info if available.
        reg = await self._get_registry(device_id)
        if isinstance(reg, dict) and reg.get("instance_id"):
            return {
                "device_id": device_id,
                "connected_at": reg.get("connected_at"),
                "last_seen_at": reg.get("last_seen_at"),
                "capabilities": reg.get("capabilities") or {},
                "instance_id": reg.get("instance_id"),
            }
        return None

    async def unregister(self, device_id: str) -> None:
        async with self._lock:
            self._agents.pop(device_id, None)
        await self._del_registry(device_id)

    async def touch(self, device_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            conn = self._agents.get(device_id)
            if conn:
                conn.last_seen_at = now

        # Refresh registry TTL.
        try:
            async with self._lock:
                conn2 = self._agents.get(device_id)
            if conn2:
                await self._set_registry(device_id, conn2)
        except Exception:
            pass

    async def update_capabilities(self, device_id: str, capabilities: Dict[str, Any]) -> None:
        async with self._lock:
            conn = self._agents.get(device_id)
            if conn:
                merged = dict(conn.capabilities or {})
                merged.update(capabilities or {})
                conn.capabilities = merged

        try:
            async with self._lock:
                conn2 = self._agents.get(device_id)
            if conn2:
                await self._set_registry(device_id, conn2)
        except Exception:
            pass

    async def is_connected(self, device_id: str) -> bool:
        async with self._lock:
            if device_id in self._agents:
                return True
        reg = await self._get_registry(device_id)
        return bool(isinstance(reg, dict) and reg.get("instance_id"))

    async def send_job(self, device_id: str, job: Dict[str, Any]) -> None:
        """Send a job to the connected agent."""
        async with self._lock:
            conn = self._agents.get(device_id)
        if conn:
            await conn.websocket.send_text(json.dumps({"type": "job", **job}))
            return

        # Multi-instance: route to the instance holding the websocket.
        if self._broker is not None:
            reg = await self._get_registry(device_id)
            target = (reg or {}).get("instance_id") if isinstance(reg, dict) else None
            if target:
                await self._broker.publish_json(
                    f"agent_jobs:{str(target).strip()}",
                    {
                        "device_id": device_id,
                        "job": job,
                    },
                )
                return

        raise RuntimeError(f"Agent not connected: {device_id}")

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
