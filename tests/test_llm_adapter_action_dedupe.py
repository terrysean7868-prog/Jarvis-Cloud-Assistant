import asyncio

from src.core.llm_adapter import LLMAdapter


def _types(actions):
    return [a.get("type") for a in (actions or []) if isinstance(a, dict)]


def test_generate_response_dedupes_duplicate_open_app(monkeypatch):
    adapter = LLMAdapter()

    async def _no_decision_maker():
        return None

    monkeypatch.setattr(adapter, "_ensure_decision_maker", _no_decision_maker)

    async def _fake_call_openai(*_a, **_k):
        # Simulate a model output that redundantly includes both open_app and an open_url
        # that postprocessing converts to open_app as well.
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"text":"ok","actions":['
                            '{"type":"open_app","app_name":"notepad","args":[]},'
                            '{"type":"open_url","url":"","url_name":"notepad"}'
                            "]}"
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(adapter, "_call_openai", _fake_call_openai)

    try:
        out = asyncio.run(adapter.generate_response("open notepad", context="", mode="chat"))
        assert isinstance(out, dict)
        actions = out.get("actions") or []

        assert _types(actions).count("open_app") == 1
        assert actions[0].get("type") == "open_app"
        assert (actions[0].get("app_name") or "").lower() == "notepad"
    finally:
        # Best-effort: avoid aiohttp unclosed-session warnings.
        asyncio.run(adapter.close())
