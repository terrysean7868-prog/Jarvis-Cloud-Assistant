from src.core.llm_adapter import LLMAdapter


def _types(actions):
    return [a.get("type") for a in (actions or []) if isinstance(a, dict)]


def test_deterministic_voice_open_notepad():
    out = LLMAdapter._preparse_deterministic_voice_actions("open Notepad")
    assert out is not None
    assert _types(out.get("actions")) == ["open_app"]
    assert out["actions"][0]["app_name"] == "notepad"


def test_deterministic_voice_open_notepad_natural_phrasing():
    out = LLMAdapter._preparse_deterministic_voice_actions("Jarvis please open the note pad")
    assert out is not None
    assert _types(out.get("actions")) == ["open_app"]
    assert out["actions"][0]["app_name"] == "notepad"


def test_deterministic_voice_open_notepad_and_type():
    out = LLMAdapter._preparse_deterministic_voice_actions("open notepad and type hello")
    assert out is not None
    assert _types(out.get("actions")) == ["open_app", "type_text"]
    assert out["actions"][0]["app_name"] == "notepad"
    assert out["actions"][1]["text"].strip() == "hello"


def test_deterministic_voice_open_notepad_and_type_natural_phrasing():
    out = LLMAdapter._preparse_deterministic_voice_actions("can you open notepad then type hello")
    assert out is not None
    assert _types(out.get("actions")) == ["open_app", "type_text"]
    assert out["actions"][0]["app_name"] == "notepad"
    assert out["actions"][1]["text"].strip() == "hello"


def test_deterministic_voice_close_notepad_natural_phrasing():
    out = LLMAdapter._preparse_deterministic_voice_actions("please close the notepad")
    assert out is not None
    assert _types(out.get("actions")) == ["close_app"]
    assert out["actions"][0]["app_name"] in {"notepad", "note pad"}


def test_deterministic_voice_does_not_hijack_search_tasks():
    out = LLMAdapter._preparse_deterministic_voice_actions("open notepad download")
    assert out is None
