from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from src.config import env
from src.config import runtime_defaults as rd


@dataclass(frozen=True)
class Settings:
    """Centralized runtime settings.

    Goal:
    - Define environment-driven configuration in one place
    - Keep behavior consistent across modules (avoid CLOUD_MODE mismatches)
    - Support multi-instance deployments when a shared broker is configured
    """

    cloud_mode: bool

    instance_id: str

    redis_url: str
    redis_prefix: str

    # Task persistence mode:
    # - auto: MongoDB when available; local file only when not cloud
    # - mongo: require MongoDB
    # - file: force file (local only)
    # - memory: in-memory only (debug)
    task_store: str

    @property
    def redis_enabled(self) -> bool:
        return bool((self.redis_url or "").strip())


def load_settings() -> Settings:
    # Environment variables (canonical list; keep in sync with code):
    # - JARVIS_CLOUD_MODE: force cloud mode on/off (defaults to hosted runtime detection)
    # - JARVIS_INSTANCE_ID: unique server instance id (auto-generated if missing)
    # - JARVIS_REDIS_URL: enable Redis broker for multi-instance notifications + agent routing
    # - JARVIS_REDIS_PREFIX: Redis key/channel prefix (default: jarvis)
    # - JARVIS_TASK_STORE: auto|mongo|file|memory (auto prefers Mongo; cloud never uses file)
    cloud_mode = env.get_bool("JARVIS_CLOUD_MODE", bool(rd.CLOUD_MODE))

    instance_id = (env.get_str("JARVIS_INSTANCE_ID", "") or "").strip()
    if not instance_id:
        instance_id = f"jarvis_{uuid.uuid4().hex[:12]}"

    redis_url = (env.get_str("JARVIS_REDIS_URL", "") or "").strip()
    redis_prefix = (env.get_str("JARVIS_REDIS_PREFIX", "jarvis") or "jarvis").strip()
    if not redis_prefix:
        redis_prefix = "jarvis"

    default_task_store = "mongo" if cloud_mode else "auto"
    task_store = (env.get_str("JARVIS_TASK_STORE", default_task_store) or default_task_store).strip().lower()
    if task_store not in {"auto", "mongo", "file", "memory"}:
        task_store = "auto"

    # Safety backstop: never allow file-based task store in cloud mode.
    if cloud_mode and task_store == "file":
        task_store = "mongo"

    return Settings(
        cloud_mode=bool(cloud_mode),
        instance_id=instance_id,
        redis_url=redis_url,
        redis_prefix=redis_prefix,
        task_store=task_store,
    )


settings = load_settings()
