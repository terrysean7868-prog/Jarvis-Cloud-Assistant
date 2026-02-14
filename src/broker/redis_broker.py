from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.config.settings import settings


JsonDict = dict[str, Any]


def _try_import_redis_asyncio():
    try:
        import redis.asyncio as redis  # type: ignore

        return redis
    except Exception:
        return None


@dataclass
class RedisBroker:
    """Minimal Redis pub/sub broker.

    This is intentionally tiny:
    - publish_json(channel, payload)
    - start_listener(channel, on_message)

    If Redis is not configured, do not instantiate this.
    """

    url: str
    prefix: str = "jarvis"

    def __post_init__(self) -> None:
        redis_mod = _try_import_redis_asyncio()
        if redis_mod is None:
            raise RuntimeError(
                "Redis broker requested but redis package is not installed. "
                "Install 'redis' and set JARVIS_REDIS_URL."
            )
        self._redis_mod = redis_mod
        self._redis = redis_mod.from_url(self.url, decode_responses=True)

    def channel(self, name: str) -> str:
        n = (name or "").strip()
        p = (self.prefix or "jarvis").strip()
        return f"{p}:{n}" if p else n

    async def close(self) -> None:
        try:
            await self._redis.close()
        except Exception:
            pass

    async def publish_json(self, channel: str, payload: JsonDict) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await self._redis.publish(self.channel(channel), data)

    async def get_json(self, key: str) -> Optional[JsonDict]:
        try:
            raw = await self._redis.get(self.channel(key))
        except Exception:
            return None
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None

    async def set_json(self, key: str, payload: JsonDict, *, ttl_seconds: int) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # setex uses seconds
        await self._redis.setex(self.channel(key), int(max(1, ttl_seconds)), data)

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(self.channel(key))
        except Exception:
            pass

    def start_listener(
        self,
        channel: str,
        on_message: Callable[[JsonDict], Awaitable[None]],
        *,
        task_name: str = "redis-listener",
    ) -> asyncio.Task:
        async def _run() -> None:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(self.channel(channel))
            try:
                async for msg in pubsub.listen():
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("type") != "message":
                        continue
                    raw = msg.get("data")
                    if not raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        try:
                            await on_message(obj)
                        except Exception:
                            # Never crash the listener.
                            pass
            finally:
                try:
                    await pubsub.unsubscribe()
                except Exception:
                    pass
                try:
                    await pubsub.close()
                except Exception:
                    pass

        t = asyncio.create_task(_run(), name=task_name)
        return t


def maybe_create_broker() -> Optional[RedisBroker]:
    """Create a RedisBroker if configured, otherwise return None."""
    if not settings.redis_enabled:
        return None
    return RedisBroker(url=settings.redis_url, prefix=settings.redis_prefix)
