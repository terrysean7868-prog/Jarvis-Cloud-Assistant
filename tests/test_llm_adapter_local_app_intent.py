from src.core.llm_adapter import LLMAdapter


def _types(actions):
    return [a.get("type") for a in (actions or []) if isinstance(a, dict)]


def test_open_notepad_becomes_open_app():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_open_url_actions("open notepad", parsed)
    assert _types(out.get("actions")) == ["open_app"]
    assert out["actions"][0]["app_name"] == "notepad"


def test_open_calculator_becomes_open_app():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_open_url_actions("open calculator", parsed)
    assert _types(out.get("actions")) == ["open_app"]
    assert out["actions"][0]["app_name"] in {"calculator", "calc"}


def test_open_task_manager_becomes_open_app():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_open_url_actions("open task manager", parsed)
    assert _types(out.get("actions")) == ["open_app"]
    assert out["actions"][0]["app_name"] == "taskmgr"


def test_open_site_still_opens_url():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_open_url_actions("open github", parsed)
    assert _types(out.get("actions")) == ["open_url"]
    assert "github.com" in out["actions"][0]["url"]


def test_open_notepad_and_type_adds_type_text():
    parsed = {"text": "", "actions": []}
    out = LLMAdapter._postprocess_open_url_actions("open notepad and type hello", parsed)
    assert _types(out.get("actions")) == ["open_app", "type_text"]
    assert out["actions"][0]["app_name"] == "notepad"
    assert out["actions"][1]["text"].strip() == "hello"
