from src.core.llm_adapter import LLMAdapter


def test_intent_profile_goal_oriented():
    profile = LLMAdapter._classify_intent_profile("I want to learn python fast")
    assert profile.get("intent_type") == "goal_oriented"
    assert profile.get("response_strategy") == "explain_plus_plan_plus_optional_execution"


def test_goal_transforms_into_actionable_plan():
    out = LLMAdapter._build_goal_oriented_plan_response("I want to learn python fast", user_prefs={})
    assert isinstance(out, dict)
    txt = str(out.get("text") or "").lower()
    assert "1. open browser" in txt
    assert "2. search:" in txt
    assert "do you want me" in txt
    assert out.get("intent_type") == "goal_oriented"


def test_intent_profile_direct_action():
    profile = LLMAdapter._classify_intent_profile("open chrome")
    assert profile.get("intent_type") == "direct_action"
    assert profile.get("response_strategy") == "execute_immediately"


def test_informational_strips_unnecessary_execution_actions():
    llm = LLMAdapter()
    parsed = {
        "text": "Python is a programming language.",
        "actions": [{"type": "open_app", "app_name": "chrome"}],
    }
    out = llm._sanitize_output_text("what is python", parsed)
    assert out.get("actions") == []


def test_open_youtube_adds_proactive_followup():
    llm = LLMAdapter()
    parsed = {
        "text": "Opening it now.",
        "actions": [{"type": "open_url", "url": "https://www.youtube.com"}],
    }
    out = llm._postprocess_proactive_followup("open youtube", parsed)
    assert "want me to search something" in str(out.get("text") or "").lower()
