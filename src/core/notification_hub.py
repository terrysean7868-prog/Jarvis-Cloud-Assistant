from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set


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

    async def register(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        uid = (user_id or "").strip()
        async with self._lock:
            self._queues_by_user.setdefault(uid, set()).add(q)
        return q

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
        async with self._lock:
            qs = list(self._queues_by_user.get(uid) or [])

        if not qs:
            return

        for q in qs:
            try:
                q.put_nowait(payload)
            except Exception:
                # Drop on backpressure; receiver can still fetch via tasks endpoint.
                pass


notification_hub = NotificationHub()
