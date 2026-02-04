import time

from src.core.dialogue_state import DialogueStateStore, PendingClarification


def test_pending_clarification_roundtrip_and_answer_detection(tmp_path, monkeypatch):
    # Redirect session storage to temp dir.
    # We patch the module-level _DATA_DIR by monkeypatching Path-returning helpers.
    import src.core.dialogue_state as ds_mod

    monkeypatch.setattr(ds_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ds_mod, "_PENDING_TTL_SECONDS", 1800)

    store = DialogueStateStore()
    session_id = "user_1"

    pending = PendingClarification(
        kind="research",
        question="What topic should I research?",
        original_user_text="do research",
        created_at=time.time(),
    )

    store.save_pending(session_id, pending)
    loaded = store.load_pending(session_id)
    assert loaded is not None
    assert loaded.kind == "research"

    # For research, an answer starting with 'research ...' should be treated as an answer.
    assert store.looks_like_direct_answer("research digital assistants market", pending_kind=loaded.kind) is True

    # Obvious new imperative command should not be treated as an answer.
    assert store.looks_like_direct_answer("open notepad", pending_kind=loaded.kind) is False


def test_pending_clarification_expires(tmp_path, monkeypatch):
    import src.core.dialogue_state as ds_mod

    monkeypatch.setattr(ds_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ds_mod, "_PENDING_TTL_SECONDS", 1)

    store = DialogueStateStore()
    session_id = "user_2"

    pending = PendingClarification(
        kind="file_action",
        question="Which file path?",
        original_user_text="read a file",
        created_at=time.time() - 999,
    )
    store.save_pending(session_id, pending)

    # Should expire and return None.
    assert store.load_pending(session_id) is None
