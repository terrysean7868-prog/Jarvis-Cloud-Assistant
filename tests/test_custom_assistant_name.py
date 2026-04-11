"""Tests that verify custom assistant names (e.g. "Zara") are recognised by
deterministic fast-path helpers and that the LLM system prompt is personalised.

These guard the fix for the "zara" issue where hardcoded "jarvis" patterns
prevented users from renaming their assistant to any other name.
"""

import re

import pytest

from src.core.llm_adapter import LLMAdapter


# ---------------------------------------------------------------------------
# _quick_local_chat_reply
# ---------------------------------------------------------------------------


class TestQuickLocalChatReply:
    def test_default_jarvis_wake_still_works(self):
        out = LLMAdapter._quick_local_chat_reply("jarvis")
        assert out is not None
        assert "here" in (out.get("text") or "").lower()

    def test_hey_jarvis_recognised(self):
        out = LLMAdapter._quick_local_chat_reply("hey jarvis")
        assert out is not None

    def test_custom_name_zara_wake(self):
        """'zara' alone should be a valid wake phrase when assistant_name='Zara'."""
        out = LLMAdapter._quick_local_chat_reply("zara", assistant_name="Zara")
        assert out is not None, "Expected a fast-path response for 'zara' wake"
        assert "here" in (out.get("text") or "").lower()

    def test_hey_zara_wake(self):
        out = LLMAdapter._quick_local_chat_reply("hey zara", assistant_name="Zara")
        assert out is not None, "Expected a fast-path response for 'hey zara'"

    def test_ok_zara_wake(self):
        out = LLMAdapter._quick_local_chat_reply("ok zara", assistant_name="Zara")
        assert out is not None

    def test_greeting_with_custom_name(self):
        out = LLMAdapter._quick_local_chat_reply("hello zara", assistant_name="Zara")
        assert out is not None

    def test_jarvis_not_matched_when_name_is_zara(self):
        """When the user renamed to Zara, 'jarvis' alone should NOT trigger
        the wake fast-path (it would fall through to the LLM)."""
        out = LLMAdapter._quick_local_chat_reply("jarvis", assistant_name="Zara")
        assert out is None, "Expected no fast-path for old name after rename"

    def test_fallback_to_jarvis_when_no_name_given(self):
        """Without an explicit assistant_name, the default 'jarvis' still works."""
        out = LLMAdapter._quick_local_chat_reply("jarvis")
        assert out is not None

    def test_multi_word_custom_name(self):
        out = LLMAdapter._quick_local_chat_reply("hey friday", assistant_name="Friday")
        assert out is not None


# ---------------------------------------------------------------------------
# _preparse_deterministic_voice_actions  — greeting + self-ID patterns
# ---------------------------------------------------------------------------


class TestPreparseDeterministicVoiceActions:
    def test_default_jarvis_greeting(self):
        out = LLMAdapter._preparse_deterministic_voice_actions("jarvis")
        assert out is not None
        assert out.get("source") == "deterministic-greeting"

    def test_zara_greeting_recognised(self):
        out = LLMAdapter._preparse_deterministic_voice_actions("zara", assistant_name="Zara")
        assert out is not None, "'zara' wake word should match with assistant_name='Zara'"
        assert out.get("source") == "deterministic-greeting"

    def test_hey_zara_greeting(self):
        out = LLMAdapter._preparse_deterministic_voice_actions(
            "hey zara", assistant_name="Zara"
        )
        assert out is not None
        assert out.get("source") == "deterministic-greeting"

    def test_who_are_you_returns_custom_name(self):
        out = LLMAdapter._preparse_deterministic_voice_actions(
            "who are you", assistant_name="Zara"
        )
        assert out is not None
        assert "Zara" in (out.get("text") or ""), "Self-identification should use the custom name"

    def test_who_are_you_default_name(self):
        out = LLMAdapter._preparse_deterministic_voice_actions("who are you")
        assert out is not None
        assert "Jarvis" in (out.get("text") or "")

    def test_jarvis_not_matched_when_renamed_to_zara(self):
        """After renaming to Zara, the old 'jarvis' wake phrase should not produce
        the greeting fast-path (it may produce some other parse or None)."""
        out = LLMAdapter._preparse_deterministic_voice_actions("jarvis", assistant_name="Zara")
        # Either None or some non-greeting result is acceptable.
        if out is not None:
            assert out.get("source") != "deterministic-greeting", (
                "Old name 'jarvis' should not be a wake phrase when renamed to Zara"
            )


# ---------------------------------------------------------------------------
# _apply_voice_reply_style  — wake word detection in voice mode
# ---------------------------------------------------------------------------


class TestApplyVoiceReplyStyle:
    def _make_adapter(self):
        return LLMAdapter()

    def test_jarvis_wake_voice_mode(self):
        llm = self._make_adapter()
        result = llm._apply_voice_reply_style(
            text="",
            user_text="jarvis",
            actions=[],
            user_prefs={},
            response_mode="voice",
        )
        assert "here" in result.lower()

    def test_zara_wake_voice_mode(self):
        llm = self._make_adapter()
        result = llm._apply_voice_reply_style(
            text="",
            user_text="zara",
            actions=[],
            user_prefs={"assistant_name": "Zara"},
            response_mode="voice",
        )
        assert "here" in result.lower(), (
            "Voice reply for 'zara' wake should produce a readiness response"
        )

    def test_hey_zara_wake_voice_mode(self):
        llm = self._make_adapter()
        result = llm._apply_voice_reply_style(
            text="",
            user_text="hey zara",
            actions=[],
            user_prefs={"assistant_name": "Zara"},
            response_mode="voice",
        )
        assert "here" in result.lower()

    def test_non_wake_text_unchanged(self):
        llm = self._make_adapter()
        result = llm._apply_voice_reply_style(
            text="The weather is sunny today.",
            user_text="what is the weather",
            actions=[],
            user_prefs={"assistant_name": "Zara"},
            response_mode="voice",
        )
        # Non-wake input should not produce the wake readiness phrase
        assert "how can i help" not in result.lower(), (
            "Non-wake text should not trigger the wake readiness response"
        )
        # The actual response content should be preserved (passed through clean-up)
        assert result  # non-empty


# ---------------------------------------------------------------------------
# generate_response system prompt — assistant name injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_response_uses_custom_name_in_system_prompt(monkeypatch):
    """The LLM call should receive a system prompt that names the assistant
    as the user's chosen name, not the hardcoded 'Jarvis'."""
    from src.core.llm_adapter import LLMAdapter

    llm = LLMAdapter()

    captured_messages: list[list] = []

    async def _fake_openai(self, messages, *, max_tokens, temperature, model=None, endpoint=None, api_key=None):
        captured_messages.append(list(messages))
        # Simulate a valid provider response
        return {"choices": [{"message": {"content": '{"text": "Hello!", "actions": []}'}}]}

    monkeypatch.setattr(LLMAdapter, "_call_openai", _fake_openai)
    # Disable quick/deterministic paths so we reach the LLM call
    monkeypatch.setattr(LLMAdapter, "_quick_local_chat_reply", staticmethod(lambda *a, **kw: None))
    monkeypatch.setattr(LLMAdapter, "_preparse_deterministic_voice_actions", staticmethod(lambda *a, **kw: None))

    await llm.generate_response(
        "tell me something interesting",
        user_prefs={"assistant_name": "Zara"},
    )

    assert captured_messages, "Expected at least one LLM call"
    # Look for the system message in the first call
    system_content = ""
    for m in captured_messages[0]:
        if m.get("role") == "system":
            system_content = m.get("content", "")
            break

    assert system_content, "Expected a system message in the LLM call"
    assert "Zara" in system_content, (
        f"System prompt should name the assistant 'Zara', got: {system_content[:200]}"
    )
    assert "Jarvis" not in system_content, (
        "System prompt should NOT still say 'Jarvis' when name is 'Zara'"
    )
