from src.core.llm_adapter import LLMAdapter


def test_save_screenshot_blocked_when_not_explicit():
    parsed = {
        "text": "",
        "actions": [
            {"type": "device_action", "name": "save_screenshot", "args": {"path": "data/tmp/x.png"}},
        ],
    }
    out = LLMAdapter._postprocess_system_safety("hello", parsed)
    assert out.get("actions") == []


def test_save_screenshot_allowed_when_explicit():
    parsed = {
        "text": "",
        "actions": [
            {"type": "device_action", "name": "save_screenshot", "args": {"path": "data/tmp/x.png"}},
        ],
    }
    out = LLMAdapter._postprocess_system_safety("take a screenshot", parsed)
    assert len(out.get("actions") or []) == 1
    assert (out.get("actions") or [])[0].get("type") == "device_action"
