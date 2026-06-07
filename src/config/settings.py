from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass

from . import env
from . import runtime_defaults as rd


logger = logging.getLogger(__name__)


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

    openai_api_key: str
    groq_api_key: str
    gemini_api_key: str

    mongodb_uri: str
    mongodb_db_name: str

    jwt_secret: str
    jwt_issuer: str

    jarvis_allowed_paths: str
    openweather_key: str
    telegram_token: str
    voice_max_samples: int
    voice_text_similarity_threshold: float

    # Task persistence mode:
    # - auto: MongoDB when available; local file only when not cloud
    # - mongo: require MongoDB
    # - file: force file (local only)
    # - memory: in-memory only (debug)
    task_store: str
    use_database_for_training: bool

    llm_fast_model: str
    llm_provider_timeout_s: int
    llm_provider_budget_s: int
    llm_provider_cooldown_s: int
    model_ops_routing_enabled: bool
    self_hosted_llm_enabled: bool
    self_hosted_llm_endpoint: str
    self_hosted_llm_model: str

    @property
    def redis_enabled(self) -> bool:
        return bool((self.redis_url or "").strip())


def load_settings() -> Settings:
    # Cloud mode uses runtime detection by default to preserve local/cloud compatibility.
    cloud_mode = bool(rd.CLOUD_MODE)

    instance_id = f"jarvis_{uuid.uuid4().hex[:12]}"

    redis_url = (env.get_str("JARVIS_REDIS_URL", "") or "").strip()
    redis_prefix = "jarvis"
    if not redis_prefix:
        redis_prefix = "jarvis"

    default_task_store = "mongo" if cloud_mode else "auto"
    task_store = default_task_store
    if task_store not in {"auto", "mongo", "file", "memory"}:
        task_store = "auto"

    # Safety backstop: never allow file-based task store in cloud mode.
    if cloud_mode and task_store == "file":
        task_store = "mongo"

    use_database_for_training = True

    llm_fast_model = ""
    llm_provider_timeout_s = 12
    llm_provider_budget_s = max(
        llm_provider_timeout_s,
        16,
    )
    llm_provider_cooldown_s = 45
    model_ops_routing_enabled = True

    openai_api_key = (env.get_str("OPENAI_API_KEY", "") or "").strip()
    groq_api_key = (env.get_str("GROQ_API_KEY", "") or "").strip()
    self_hosted_llm_enabled = bool(getattr(rd, "SELF_HOSTED_LLM_ENABLED", False))
    self_hosted_llm_endpoint = str(getattr(rd, "SELF_HOSTED_LLM_ENDPOINT", "") or "").strip()
    self_hosted_llm_model = str(getattr(rd, "SELF_HOSTED_LLM_MODEL", "") or "").strip()
    gemini_api_key = (env.get_str("GEMINI_API_KEY", "") or "").strip()
    mongodb_uri = (env.get_str("MONGODB_URI", "mongodb://localhost:27017/jarvis") or "").strip()
    mongodb_db_name = (env.get_str("MONGODB_DB_NAME", "jarvis_db") or "jarvis_db").strip() or "jarvis_db"
    jwt_secret = (env.get_str("JARVIS_JWT_SECRET", "") or "").strip()
    jwt_issuer = (env.get_str("JARVIS_JWT_ISSUER", "jarvis") or "jarvis").strip() or "jarvis"
    jarvis_allowed_paths = (env.get_str("JARVIS_ALLOWED_PATHS", "") or "").strip()
    openweather_key = (env.get_str("OPENWEATHER_KEY", "") or "").strip()
    telegram_token = (env.get_str("TELEGRAM_TOKEN", "") or "").strip()
    voice_max_samples = max(1, int(env.get_int("VOICE_MAX_SAMPLES", 5)))
    voice_text_similarity_threshold = max(0.1, min(0.99, float(env.get_float("VOICE_TEXT_SIMILARITY_THRESHOLD", 0.85))))

    self_hosted_ready = self_hosted_llm_enabled and bool(self_hosted_llm_endpoint)
    if not (openai_api_key or groq_api_key or self_hosted_ready):
        logger.warning(
            "[settings] missing LLM provider config: set OPENAI_API_KEY, GROQ_API_KEY, or a reachable SELF_HOSTED_LLM_ENDPOINT"
        )
    if not mongodb_uri:
        logger.warning("[settings] missing MONGODB_URI")
    if not jwt_secret:
        logger.warning("[settings] missing JARVIS_JWT_SECRET")

    return Settings(
        cloud_mode=bool(cloud_mode),
        instance_id=instance_id,
        redis_url=redis_url,
        redis_prefix=redis_prefix,
        openai_api_key=openai_api_key,
        groq_api_key=groq_api_key,
        gemini_api_key=gemini_api_key,
        mongodb_uri=mongodb_uri,
        mongodb_db_name=mongodb_db_name,
        jwt_secret=jwt_secret,
        jwt_issuer=jwt_issuer,
        jarvis_allowed_paths=jarvis_allowed_paths,
        openweather_key=openweather_key,
        telegram_token=telegram_token,
        voice_max_samples=voice_max_samples,
        voice_text_similarity_threshold=voice_text_similarity_threshold,
        task_store=task_store,
        use_database_for_training=use_database_for_training,
        llm_fast_model=llm_fast_model,
        llm_provider_timeout_s=llm_provider_timeout_s,
        llm_provider_budget_s=llm_provider_budget_s,
        llm_provider_cooldown_s=llm_provider_cooldown_s,
        model_ops_routing_enabled=bool(model_ops_routing_enabled),
        self_hosted_llm_enabled=bool(self_hosted_llm_enabled),
        self_hosted_llm_endpoint=self_hosted_llm_endpoint,
        self_hosted_llm_model=self_hosted_llm_model,
    )


settings = load_settings()
