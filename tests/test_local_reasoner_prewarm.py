from src.core.local_reasoner_prewarm import (
    merge_site_aliases,
    _domain_aliases,
    _title_candidates,
    _queries_from_documents,
)


def test_domain_aliases_extracts_core_names():
    aliases = _domain_aliases("https://www.github.com/features")
    assert "github" in aliases
    assert "github.com" in aliases


def test_title_candidates_splits_common_title_formats():
    aliases = _title_candidates("React – A JavaScript library for building user interfaces")
    assert any("react" in a for a in aliases)


def test_merge_site_aliases_increments_score():
    state = {"site_aliases": {}}
    changed1 = merge_site_aliases(state, ["github", "git hub"], "https://github.com", score_inc=1.0)
    changed2 = merge_site_aliases(state, ["github"], "https://github.com", score_inc=1.5)

    assert changed1 is True
    assert changed2 is True
    item = state["site_aliases"]["github"]
    assert item["url"] == "https://github.com"
    assert float(item["score"]) >= 2.5


def test_queries_from_documents_prioritizes_topics_and_tags():
    docs = [
        {"topic": "python programming", "analysis_tags": ["fastapi", "api"]},
        {"topic": "python programming", "analysis_tags": ["fastapi"]},
        {"topic": "react ui", "analysis_tags": ["frontend"]},
    ]
    queries = _queries_from_documents(docs, max_queries=6)
    joined = " | ".join(queries).lower()
    assert "python programming" in joined
    assert "fastapi" in joined
