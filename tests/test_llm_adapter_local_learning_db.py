import src.core.llm_adapter as llm_mod
from src.core.llm_adapter import LLMAdapter


class _FakeDB:
    def __init__(self):
        self.store = {}

    def local_reasoner_state_get(self, state_key: str = "global"):
        return self.store.get((state_key or "global").strip().lower())

    def local_reasoner_state_upsert(self, state: dict, state_key: str = "global") -> bool:
        key = (state_key or "global").strip().lower()
        self.store[key] = {**state, "state_key": key}
        return True


def test_local_reasoner_uses_db_state(monkeypatch):
    fake_db = _FakeDB()
    fake_db.local_reasoner_state_upsert(
        {
            "version": 1,
            "updated_at": "",
            "last_maintenance_day": "",
            "app_aliases": {
                "note app": {
                    "app_name": "notepad",
                    "score": 2.0,
                    "updated_at": "",
                }
            },
            "site_aliases": {},
            "stats": {"learn_events": 0, "hits": 0},
        },
        state_key="shared",
    )

    monkeypatch.setattr(llm_mod, "db", fake_db, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_LEARNING_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_DB_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_STATE_KEY", "shared", raising=False)

    adapter = LLMAdapter()
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


def test_local_reasoner_writes_to_db(monkeypatch):
    fake_db = _FakeDB()

    monkeypatch.setattr(llm_mod, "db", fake_db, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_LEARNING_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_DB_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_STATE_KEY", "shared", raising=False)

    adapter = LLMAdapter()
    adapter._learn_from_actions(
        "open notes app",
        [{"type": "open_app", "app_name": "notepad", "args": []}],
    )

    doc = fake_db.local_reasoner_state_get("shared")
    assert isinstance(doc, dict)
    aliases = doc.get("app_aliases") if isinstance(doc.get("app_aliases"), dict) else {}
    assert "notes app" in aliases
    assert (aliases["notes app"].get("app_name") or "").lower() == "notepad"


def test_user_scoped_state_key_isolated_between_users(monkeypatch):
    fake_db = _FakeDB()

    monkeypatch.setattr(llm_mod, "db", fake_db, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_LEARNING_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_DB_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_STATE_KEY", "user", raising=False)

    adapter = LLMAdapter()

    adapter._ensure_local_reasoner_state_scope({"user_id": "alice"})
    adapter._learn_from_actions(
        "open alpha workspace",
        [{"type": "open_app", "app_name": "notepad", "args": []}],
    )

    out_alice = adapter._build_local_reasoned_response(
        text="open alpha workspace",
        context="",
        mode="chat",
        decision_hint=None,
    )
    assert isinstance(out_alice, dict)
    assert out_alice.get("source") == "local-reasoner-learned"
    assert (out_alice.get("actions") or [])[0].get("type") == "open_app"
    assert ((out_alice.get("actions") or [])[0].get("app_name") or "").lower() == "notepad"

    adapter._ensure_local_reasoner_state_scope({"user_id": "bob"})
    out_bob = adapter._build_local_reasoned_response(
        text="open alpha workspace",
        context="",
        mode="chat",
        decision_hint=None,
    )
    assert isinstance(out_bob, dict)
    assert out_bob.get("source") != "local-reasoner-learned"

    assert fake_db.local_reasoner_state_get("user:alice") is not None
    assert fake_db.local_reasoner_state_get("user:bob") is not None


def test_user_scoped_state_inherits_global_seed_on_first_load(monkeypatch):
    fake_db = _FakeDB()
    fake_db.local_reasoner_state_upsert(
        {
            "version": 1,
            "updated_at": "",
            "last_maintenance_day": "",
            "app_aliases": {},
            "site_aliases": {
                "github": {
                    "url": "https://github.com",
                    "score": 3.0,
                    "updated_at": "",
                }
            },
            "stats": {"learn_events": 0, "hits": 0},
        },
        state_key="global",
    )

    monkeypatch.setattr(llm_mod, "db", fake_db, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_CHAT_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_LEARNING_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_DB_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_mod.rd, "LOCAL_REASONER_STATE_KEY", "user", raising=False)

    adapter = LLMAdapter()
    adapter._ensure_local_reasoner_state_scope({"user_id": "alice"})

    out = adapter._build_local_reasoned_response(
        text="open github",
        context="",
        mode="chat",
        decision_hint=None,
    )

    assert isinstance(out, dict)
    actions = out.get("actions") or []
    assert actions and actions[0].get("type") == "open_url"
    assert "github.com" in (actions[0].get("url") or "")
