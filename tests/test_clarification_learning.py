import os

from src.core.clarification_learning import ClarificationLearner, ClarificationExample


def test_clarification_learning_records_and_augments(tmp_path, monkeypatch):
    learner = ClarificationLearner(base_dir=tmp_path, enabled=True, min_similarity=0.2)

    session_id = "u1"
    original = "Please research digital assistants market"
    answer = "Region: India\nPast 2 years\nOutput: table"

    slots = learner.extract_slots("research", "Which region/time range?", answer)
    ex = ClarificationExample(
        kind="research",
        question="Which region/time range?",
        original_user_text=original,
        answer_text=answer,
        slots=slots,
        created_at=123.0,
    )
    learner.record(session_id, ex)

    new_text = "research digital assistants market"
    augmented, applied = learner.augment_request(session_id, new_text)

    assert augmented != new_text
    assert "Defaults (from your previous answers):" in augmented
    assert applied.get("region") == "india"
    assert applied.get("output_format") == "table"
    # time_range may be captured as relative or via kv; ensure at least one time hint applied
    assert ("time_range" in applied) or ("year_range" in applied) or ("year" in applied)


def test_clarification_learning_similarity_threshold_blocks(tmp_path, monkeypatch):
    learner = ClarificationLearner(base_dir=tmp_path, enabled=True, min_similarity=0.99)

    ex = ClarificationExample(
        kind="research",
        question="Which region?",
        original_user_text="Research electric cars market",
        answer_text="Region: US",
        slots=learner.extract_slots("research", "Which region?", "Region: US"),
        created_at=123.0,
    )
    learner.record("u1", ex)

    new_text = "research digital assistants market"
    augmented, applied = learner.augment_request("u1", new_text)

    assert augmented == new_text
    assert applied == {}
