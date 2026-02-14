from pathlib import Path

import src.core.llm_adapter as llm_mod
from src.core.llm_adapter import LLMAdapter


def test_local_reasoner_learns_and_predicts_alias(monkeypatch, tmp_path):
    state_path = tmp_path / "local_reasoner_state.json"

    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_LEARNING_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_DB_ENABLED", False, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_STATE_FILE", str(state_path), raising=False)

    adapter = LLMAdapter()
    adapter._learn_from_actions(
        "open note app",
        [{"type": "open_app", "app_name": "notepad", "args": []}],
    )

    out = adapter._build_local_reasoned_response(
        text="open note app",
        context="",
        mode="chat",
        decision_hint=None,
    )

    assert isinstance(out, dict)
    actions = out.get("actions") or []
    assert actions and actions[0].get("type") == "open_app"
    assert (actions[0].get("app_name") or "").lower() == "notepad"
    assert out.get("source") == "local-reasoner-learned"


def test_local_reasoner_learning_persists_state(monkeypatch, tmp_path):
    state_path = tmp_path / "local_reasoner_state.json"

    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_LEARNING_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_DB_ENABLED", False, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_STATE_FILE", str(state_path), raising=False)

    a1 = LLMAdapter()
    a1._learn_from_actions(
        "open my notes",
        [{"type": "open_app", "app_name": "notepad", "args": []}],
    )

    assert Path(state_path).exists()

    a2 = LLMAdapter()
    out = a2._build_local_reasoned_response(
        text="open my notes",
        context="",
        mode="chat",
        decision_hint=None,
    )

    assert isinstance(out, dict)
    actions = out.get("actions") or []
    assert actions and actions[0].get("type") == "open_app"
    assert (actions[0].get("app_name") or "").lower() == "notepad"
