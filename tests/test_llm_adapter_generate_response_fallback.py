import asyncio

import src.core.llm_adapter as llm_mod
from src.core.llm_adapter import LLMAdapter


def test_generate_response_falls_back_to_open_app_when_llm_down(monkeypatch):
    # Avoid DB writes during unit tests.
    monkeypatch.setattr(llm_mod.db, "save_system_event", lambda *a, **k: None)

    adapter = LLMAdapter()

    async def _boom(*_a, **_k):
        raise Exception("OpenAI down")

    # Force the OpenAI call to fail.
    monkeypatch.setattr(adapter, "_call_openai", _boom)

    out = asyncio.run(adapter.generate_response("open Notepad", context="", mode="voice"))
    assert isinstance(out, dict)
    assert out.get("source") in {"deterministic-voice", "fallback-local", "fallback-web", "fallback"}
    actions = out.get("actions") or []
    assert any(isinstance(a, dict) and a.get("type") == "open_app" for a in actions)
    first = actions[0]
    assert first.get("app_name") == "notepad"


def test_voice_analysis_prompt_triggers_preweb_search(monkeypatch):
    """Voice mode should run web_search first for research/time-sensitive prompts."""

    adapter = LLMAdapter()

    def _boom(*_args, **_kwargs):
        raise AssertionError("LLM should not be called for voice pre-web lookup")

    # If the code calls the model, the test should fail.
    monkeypatch.setattr(adapter, "_call_openai", _boom, raising=True)

    out = asyncio.run(
        adapter.generate_response(
            "Analyze the current state of the Bitcoin market and summarize key drivers.",
            mode="voice",
        )
    )

    assert isinstance(out, dict)
    assert out.get("source") == "voice-pre-web"
    actions = out.get("actions")
    assert isinstance(actions, list)
    assert any(isinstance(a, dict) and a.get("type") == "web_search" for a in actions)


def test_should_use_web_lookup_true_for_high_level_analysis_with_sources():
    assert LLMAdapter._should_use_web_lookup(
        "Compare PostgreSQL vs MongoDB for event sourcing (2026) with sources"
    )
