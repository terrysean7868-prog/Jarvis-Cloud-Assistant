from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from src.config.settings import settings

try:
    from src.broker.redis_broker import RedisBroker
except Exception:
    RedisBroker = None  # type: ignore


JsonDict = Dict[str, Any]


@dataclass
class NotificationHub:
    """In-process pub/sub for pushing server events to clients.

    Notes:
    - This is per-process (not cross-worker). It is intentionally lightweight.
    - We keep the persisted TaskManager fallback so users can still retrieve results
      even if they miss the push notification.
    """

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _queues_by_user: Dict[str, Set[asyncio.Queue]] = field(default_factory=dict)

    # Optional broker for multi-instance deployments.
    _broker: Optional[Any] = None
    _instance_id: str = field(default_factory=lambda: settings.instance_id)
    _listener_task: Optional[asyncio.Task] = None

    async def register(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        uid = (user_id or "").strip()
        async with self._lock:
            self._queues_by_user.setdefault(uid, set()).add(q)
        return q

    def attach_broker(self, broker: Any) -> None:
        """Attach a broker (e.g., Redis) for cross-instance publish/subscribe."""
        self._broker = broker

    def start_broker_listener(self) -> None:
        """Start the broker subscription loop (idempotent)."""
        if not self._broker:
            return
        if self._listener_task and not self._listener_task.done():
            return

        async def _on_message(msg: JsonDict) -> None:
            try:
                origin = (msg.get("origin") or "").strip()
                if origin and origin == self._instance_id:
                    return
                user_id = (msg.get("user_id") or "").strip()
                payload = msg.get("payload")
                if not user_id or not isinstance(payload, dict):
                    return
                await self._publish_local(user_id, payload)
            except Exception:
                return

        try:
            self._listener_task = self._broker.start_listener(
                "notifications",
                _on_message,
                task_name="broker-notifications",
            )
        except Exception:
            self._listener_task = None

    async def shutdown(self) -> None:
        """Stop broker listener (best-effort)."""
        try:
            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
        except Exception:
            pass

    async def _publish_local(self, user_id: str, payload: JsonDict) -> None:
        uid = (user_id or "").strip()
        if not uid:
            return
        async with self._lock:
            qs = list(self._queues_by_user.get(uid) or [])

        if not qs:
            return

        for q in qs:
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    async def unregister(self, user_id: str, q: asyncio.Queue) -> None:
        uid = (user_id or "").strip()
        async with self._lock:
            qs = self._queues_by_user.get(uid)
            if not qs:
                return
            qs.discard(q)
            if not qs:
                self._queues_by_user.pop(uid, None)

    async def publish(self, user_id: str, payload: JsonDict) -> None:
        uid = (user_id or "").strip()
        if not uid:
            return

        # Always deliver to local listeners first for low latency.
        await self._publish_local(uid, payload)

        # Multi-instance: publish to broker so other instances can deliver to their local clients.
        if self._broker is not None:
            try:
                await self._broker.publish_json(
                    "notifications",
                    {
                        "origin": self._instance_id,
                        "user_id": uid,
                        "payload": payload,
                    },
                )
            except Exception:
                # Best-effort: do not break the main flow.
                pass


notification_hub = NotificationHub()
