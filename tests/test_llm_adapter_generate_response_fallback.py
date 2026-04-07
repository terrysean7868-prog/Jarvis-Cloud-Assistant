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

    try:
        out = asyncio.run(adapter.generate_response("open Notepad", context="", mode="voice"))
        assert isinstance(out, dict)
        assert out.get("source") in {"deterministic-voice", "fallback-local", "fallback-web", "fallback"}
        actions = out.get("actions") or []
        assert any(isinstance(a, dict) and a.get("type") == "open_app" for a in actions)
        first = actions[0]
        assert first.get("app_name") == "notepad"
    finally:
        asyncio.run(adapter.close())


def test_provider_unavailable_fallback_resets_stale_runtime_task(monkeypatch):
    monkeypatch.setattr(llm_mod.db, "save_system_event", lambda *a, **k: None)

    adapter = LLMAdapter()

    async def _boom(*_a, **_k):
        raise Exception("OpenAI down")

    monkeypatch.setattr(adapter, "_call_openai", _boom)

    # Simulate a stale active task context from a prior turn.
    stale = adapter._default_runtime_task_context()
    stale["task_active"] = True
    stale["task_goal"] = "old task"
    stale["task_step"] = 3
    stale["task_status"] = "in_progress"
    adapter._set_runtime_task_context(None, stale)

    try:
        out = asyncio.run(adapter.generate_response("tell me something", context="", mode="chat"))
        assert isinstance(out, dict)
        assert out.get("source") == "fallback"

        ctx = adapter._get_runtime_task_context(None)
        assert ctx.get("task_active") is False
        assert str(ctx.get("task_status") or "") == "waiting_for_input"

        next_out = asyncio.run(adapter.generate_response("open notepad", context="", mode="chat"))
        actions = next_out.get("actions") or []
        assert any(isinstance(a, dict) and a.get("type") == "open_app" for a in actions)
    finally:
        asyncio.run(adapter.close())


def test_resolve_contextual_user_text_rewrites_open_it_from_last_action():
    adapter = LLMAdapter()
    ctx = adapter._default_runtime_task_context()
    ctx["last_action"] = "open_app:gemini"
    ctx["task_active"] = True

    out = adapter._resolve_contextual_user_text("Gemini but didn't open so open it", ctx)
    assert "open gemini" in str(out or "").lower()


def test_resolve_contextual_user_text_rewrites_open_that_and_close_it():
    adapter = LLMAdapter()
    ctx = adapter._default_runtime_task_context()
    ctx["last_action"] = "open_app:chrome"
    ctx["task_active"] = True

    out_open = adapter._resolve_contextual_user_text("open that", ctx)
    assert "open chrome" in str(out_open or "").lower()

    out_close = adapter._resolve_contextual_user_text("close it", ctx)
    assert "close chrome" in str(out_close or "").lower()


def test_resolve_contextual_user_text_rewrites_retry_again_to_open_target():
    adapter = LLMAdapter()
    ctx = adapter._default_runtime_task_context()
    ctx["last_action"] = "open_app:gemini"
    ctx["task_active"] = True

    out = adapter._resolve_contextual_user_text("it did not open, try again", ctx)
    first_line = str(out or "").strip().lower().splitlines()[0]
    assert first_line == "open gemini"


def test_infer_result_status_does_not_mark_success_from_actions_only():
    llm = LLMAdapter()
    assert llm._infer_result_status("", has_actions=True) == ""


def test_suggest_next_task_step_reports_queued_waiting_message():
    llm = LLMAdapter()
    ctx = llm._default_runtime_task_context()
    ctx["task_active"] = True
    ctx["task_status"] = "queued"
    parsed = {"actions": [{"type": "device_action", "name": "set_brightness"}]}
    msg = llm._suggest_next_task_step(ctx, parsed)
    assert "execution queued" in str(msg or "").lower()


def test_action_text_for_device_action_is_queued_not_done():
    msg = LLMAdapter._action_text_from_first_action([{"type": "device_action", "name": "set_bluetooth"}])
    assert "queued" in str(msg or "").lower()


def test_update_runtime_context_derives_last_entity_from_open_app_action():
    adapter = LLMAdapter()
    parsed = {
        "text": "Opening gemini.",
        "actions": [{"type": "open_app", "app_name": "gemini"}],
    }

    adapter._update_runtime_task_context(
        user_text="open gemini",
        planning_text="open gemini",
        parsed=parsed,
        user_prefs=None,
    )

    ctx = adapter._get_runtime_task_context(None)
    assert str(ctx.get("last_entity") or "").lower() == "gemini"


def test_voice_analysis_prompt_triggers_preweb_search(monkeypatch):
    """Voice mode should run web_search first for research/time-sensitive prompts."""

    adapter = LLMAdapter()

    def _boom(*_args, **_kwargs):
        raise AssertionError("LLM should not be called for voice pre-web lookup")

    # If the code calls the model, the test should fail.
    monkeypatch.setattr(adapter, "_call_openai", _boom, raising=True)

    try:
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
    finally:
        asyncio.run(adapter.close())


def test_should_use_web_lookup_true_for_high_level_analysis_with_sources():
    assert LLMAdapter._should_use_web_lookup(
        "Compare PostgreSQL vs MongoDB for event sourcing (2026) with sources"
    )


def test_should_use_web_lookup_false_for_research_status_questions():
    assert not LLMAdapter._should_use_web_lookup("do you completed research")
    assert not LLMAdapter._should_use_web_lookup("did you finish the research?")
    assert not LLMAdapter._should_use_web_lookup("research status")


def test_app_manager_executes_ms_settings_via_explorer(monkeypatch):
    import src.utils.app_manager as am

    calls = []

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _R()

    monkeypatch.setattr(am.platform, "system", lambda: "Windows")
    monkeypatch.setattr(am.subprocess, "run", _fake_run)

    mgr = am.AppManager()
    out = mgr.execute_command('start "" "ms-settings:bluetooth"', wait=True)
    assert out.get("status") == "success"
    assert calls, "expected subprocess.run to be called"
    args, kwargs = calls[0]
    assert args[0] == "explorer.exe"
    assert "ms-settings:bluetooth" in args[1]
    assert kwargs.get("shell") is False


def test_app_manager_executes_url_via_explorer(monkeypatch):
    import src.utils.app_manager as am

    calls = []

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _R()

    monkeypatch.setattr(am.platform, "system", lambda: "Windows")
    monkeypatch.setattr(am.subprocess, "run", _fake_run)

    mgr = am.AppManager()
    out = mgr.execute_command('start "" "https://example.com"', wait=True)
    assert out.get("status") == "success"
    args, kwargs = calls[0]
    assert args[0] == "explorer.exe"
    assert args[1].startswith("https://")
    assert kwargs.get("shell") is False


def test_app_manager_routes_shell_builtins_through_cmd(monkeypatch):
    import src.utils.app_manager as am

    calls = []

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _R()

    monkeypatch.setattr(am.platform, "system", lambda: "Windows")
    monkeypatch.setattr(am.subprocess, "run", _fake_run)

    mgr = am.AppManager()
    out = mgr.execute_command("dir", wait=True)
    assert out.get("status") == "success"
    args, kwargs = calls[0]
    assert args[:2] == ["cmd.exe", "/c"]
    assert args[2] == "dir"
    assert kwargs.get("shell") is False


def test_app_manager_runs_normal_exe_without_shell(monkeypatch):
    import src.utils.app_manager as am

    calls = []

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    def _fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _R()

    monkeypatch.setattr(am.platform, "system", lambda: "Windows")
    monkeypatch.setattr(am.subprocess, "run", _fake_run)

    mgr = am.AppManager()
    out = mgr.execute_command("notepad.exe", wait=True)
    assert out.get("status") == "success"
    args, kwargs = calls[0]
    assert args == ["notepad.exe"]
    assert kwargs.get("shell") is False


def test_app_manager_wait_false_uses_popen(monkeypatch):
    import src.utils.app_manager as am

    popen_calls = []

    def _fake_popen(args, **_kwargs):
        popen_calls.append(args)

        class _P:
            pid = 123

        return _P()

    monkeypatch.setattr(am.platform, "system", lambda: "Windows")
    monkeypatch.setattr(am.subprocess, "Popen", _fake_popen)

    mgr = am.AppManager()
    out = mgr.execute_command("notepad.exe", wait=False)
    assert out.get("status") == "success"
    assert popen_calls and popen_calls[0] == ["notepad.exe"]
