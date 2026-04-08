from __future__ import annotations

import os
import platform


"""Runtime defaults for Jarvis.

This module is intentionally *not* driven by custom `.env` keys for behavior tuning.
Edit these constants directly to change default behaviors.

Environment variables remain appropriate for secrets/credentials (API keys, DB URIs)
and for platform-provided runtime detection (Render/Heroku/Docker).
"""


def is_cloud_runtime() -> bool:
    """Best-effort hosted/runtime detection (Render/Heroku/Docker).

    Uses platform-provided environment markers rather than Jarvis-specific flags.
    """
    try:
        if "/opt/render" in os.getcwd():
            return True
    except Exception:
        pass
    return False


# -------------------------
# Safety / runtime modes
# -------------------------
CLOUD_MODE: bool = is_cloud_runtime()


# -------------------------
# Research / web behavior
# -------------------------
RESEARCH_ASYNC_DEFAULT: bool = True
RESEARCH_FETCH_URLS_DEEP: int = 2  # clamped by orchestrator

OFFLINE_ANALYSIS: bool = False
OFFLINE_ONLY: bool = False
OFFLINE_WEB_ONLY: bool = False

WEB_RESULTS_MODE: str = "answer"  # "answer" | "sources" | ...
AUTO_FETCH_LINKS_MODE: str = "always"  # "always" | "never" | "ask"
AUTO_FETCH_LINKS_MAX: int = 1
AUTO_FETCH_WIKIPEDIA_LINKS_ONLY: bool = False

# Global factual mode (all topics): prefer web-backed answers over memory-only responses.
# Note: no AI system can guarantee 100% correctness, but this mode maximizes reliability.
GLOBAL_FACTUAL_MODE: bool = True
GLOBAL_FACTUAL_REQUIRE_SOURCES: bool = True
GLOBAL_FACTUAL_INCLUDE_CONFIDENCE: bool = True

RETURN_ACTION_RESULTS: bool = False
FETCH_URL_CACHE_SECONDS: int = 600

# Inline execution/UX behavior for safe non-web actions.
# These run synchronously in the chat response path so users get immediate outcomes.
INLINE_NON_WEB_ACTION_TYPES_CSV: str = "create_task,stop_task,check_errors,check_render_logs,generate_email,find_files"
APPEND_INLINE_ACTION_SUMMARY: bool = True


# -------------------------
# Decision / routing
# -------------------------
DECISION_FASTPATH_CONF: float = 0.92
AUTO_WEB_ON_UNKNOWN: bool = True
AUTO_WEB_ON_UNCERTAINTY: bool = True
LOCAL_REASONER_ENABLED: bool = True
LOCAL_REASONER_CHAT_ENABLED: bool = True
LOCAL_REASONER_MIN_CONFIDENCE: float = 0.84
LOCAL_REASONER_LEARNING_ENABLED: bool = True
LOCAL_REASONER_DB_ENABLED: bool = True
LOCAL_REASONER_STATE_KEY: str = "user"
LOCAL_REASONER_STATE_FILE: str = "data/local_reasoner_state.json"
LOCAL_REASONER_MAX_ALIASES: int = 400
LOCAL_REASONER_DAILY_DECAY: float = 0.98
LOCAL_REASONER_MIN_ALIAS_SCORE: float = 0.20


# -------------------------
# Agentic (rule-first) loop
# -------------------------
# When enabled (typically via env in app.py / JarvisBrain), Jarvis will try to
# produce structured actions via deterministic decision-making before calling
# the LLM.
AGENTIC_LOOP: bool = False
AGENTIC_MIN_CONFIDENCE: float = 0.88
AGENTIC_MAX_SUBTASKS: int = 6


# -------------------------
# LLM defaults (non-secret)
# -------------------------
LLM_PROVIDER: str = "openai_compatible"  # "ollama" | "openai_compatible"
PRIMARY_MODEL: str = "gpt-4o-mini"
PRIMARY_ENDPOINT: str = "https://api.openai.com/v1/chat/completions"

BACKUP_MODEL: str = "llama-3.1-8b-instant"
BACKUP_ENDPOINT: str = "https://api.groq.com/openai/v1/chat/completions"

PERSONA: str = "formal-gentle"
SMART_MODEL: str = ""  # optional
SMART_MODEL_MIN_COMPLEXITY: int = 2

LLM_MAX_TOKENS_DEFAULT: int = 450
LLM_MAX_TOKENS_MAX: int = 900


# -------------------------
# Learning / memory
# -------------------------
LEARNING_ENABLED: bool = True
LEARNING_BUFFER_MAX: int = 2000

LEARNING_RETRIEVE: bool = True
LEARNING_RETRIEVAL_K: int = 3
LEARNING_MAX_CONTEXT_CHARS: int = 1200

WEB_KNOWLEDGE_CONTEXT: bool = True
WEB_KNOWLEDGE_K: int = 3
WEB_KNOWLEDGE_MAX_CHARS: int = 1200

MIN_FINETUNE_EXAMPLES: int = 10
REQUIRE_MANUAL_APPROVAL: bool = True


# Filesystem sandbox allowlist (comma-separated paths relative to repo root)
ALLOWED_PATHS_CSV: str = "src,modules,frontend/src,data"


# -------------------------
# Git / background jobs
# -------------------------
AUTO_GIT_SYNC: bool = False

ENABLE_DB_MAINTENANCE: bool = True
ENABLE_WEB_TRAINING_JOB: bool = True
ENABLE_WIKI_TRAINING_JOB: bool = False
ENABLE_BACKGROUND_ANALYSIS_JOB: bool = True
ENABLE_LOCAL_REASONER_PREWARM_JOB: bool = True
ENABLE_MEMORY_OPTIMIZATION: bool = False
ENABLE_TRAINING_DATA_JOB: bool = False

# Progressive daily LLM/brain update job.
# Goal: gradual quality improvements without requiring manual voice update each day.
ENABLE_PROGRESSIVE_LLM_UPDATE_JOB: bool = True
PROGRESSIVE_LLM_UPDATE_INTERVAL_SECONDS: int = 86400
PROGRESSIVE_LLM_UPDATE_TARGET_FILES_CSV: str = "src/core/llm_adapter.py,src/core/jarvis_brain.py"
PROGRESSIVE_LLM_UPDATE_DRY_RUN: bool = False
PROGRESSIVE_LLM_UPDATE_AUTO_INSTALL_DEPS: bool = False
PROGRESSIVE_LLM_UPDATE_ACTOR: str = "scheduler"
PROGRESSIVE_LLM_UPDATE_DESCRIPTION: str = (
    "Apply a small, safe, incremental improvement to reasoning quality, fallback reliability, "
    "clarification quality, and response consistency while preserving existing behavior and APIs. "
    "Avoid large refactors and keep changes minimal and production-safe."
)


# -------------------------
# Autonomous engineer runtime
# -------------------------
AUTONOMY_ENABLED: bool = True
AUTONOMY_POLL_INTERVAL_SECONDS: int = 20

# OSS integrations (all optional)
ENABLE_OLLAMA: bool = False
ENABLE_CHROMADB: bool = True
ENABLE_WHISPER: bool = False
ENABLE_OPENCV: bool = True
CHROMADB_PATH: str = "data/chromadb"

WIKI_TRAINING_INTERVAL_SECONDS: int = 3600
BACKGROUND_ANALYSIS_INTERVAL_SECONDS: int = 1800
LOCAL_REASONER_PREWARM_INTERVAL_SECONDS: int = 86400
LOCAL_REASONER_PREWARM_MAX_QUERIES: int = 8
LOCAL_REASONER_PREWARM_RESULTS_PER_QUERY: int = 4
LOCAL_REASONER_PREWARM_ANALYSIS_FIRST: bool = True

WIKI_TRAINING_LANG: str = "en"
WIKI_TRAINING_MAX_PAGES: int = 5
WIKI_TRAINING_TOPICS: str = ""
BACKGROUND_ANALYSIS_BATCH: int = 30


# -------------------------
# Voice auth / biometrics
# -------------------------
VOICE_BIOMETRICS_ENABLED: bool = True
VOICE_BIOMETRICS_THRESHOLD: float = 0.70
VOICE_BIOMETRICS_MAX_EMBEDS: int = 5

AUTH_USE_DB: bool = True
AUTH_QUEUE_FLUSH_INTERVAL: int = 10
VOICE_HASH_PREFIX_MATCH: bool = False
VOICE_TEXT_SIMILARITY_THRESHOLD: float = 0.75
VOICE_MAX_SAMPLES: int = 5
AUTH_REQUIRE_DB: bool = False
