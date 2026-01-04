"""Background analysis for stored web knowledge.

Goal:
- Do lightweight, non-LLM enrichment of stored `web_training_data` items.
- Keep outputs compact (tags + a short insight string) to reduce memory.
- Avoid storing large third-party text (we only process what is already stored).

This is NOT meant to replace live web lookup; it's a cache-enrichment step.
"""

from __future__ import annotations

import re
from collections import Counter


_STOPWORDS = {
    "the","a","an","and","or","but","if","then","else","when","while","for","to","of","in","on","at","by","with",
    "is","are","was","were","be","been","being","as","it","its","this","that","these","those","from","into","over","under",
    "we","you","they","he","she","i","me","my","our","your","their","them","his","her","who","whom","which","what","why",
    "how","can","could","should","would","may","might","will","just","also","more","most","less","least","very","such","than",
}


_DOMAIN_TAGS = [
    ("psychology", r"\b(psychology|psychological|cognitive|behavioral|behavioural|emotion|memory|attention|motivation|personality|therapy)\b"),
    ("bias", r"\b(bias|confirmation bias|availability heuristic|anchoring|halo effect|framing)\b"),
    ("learning", r"\b(learning|conditioning|reinforcement|habit|practice)\b"),
    ("social", r"\b(social|group|norms|conformity|obedience|influence|identity)\b"),
    ("history", r"\b(history|historical|century|ancient|medieval|modern era)\b"),
    ("science", r"\b(science|scientific|research|experiment|evidence|theory)\b"),
    ("tech", r"\b(software|hardware|computer|programming|api|protocol|python|javascript|html|css)\b"),
]


def _shorten(text: str, max_chars: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    if len(t) <= max_chars:
        return t
    return t[: max(0, max_chars - 1)].rstrip() + "…"


def _sentences(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return []
    parts = re.split(r"(?<=[\.!\?])\s+", t)
    out: list[str] = []
    for p in parts:
        s = (p or "").strip()
        if not s:
            continue
        out.append(s)
    return out


def extract_tags(text: str, *, max_tags: int = 10) -> list[str]:
    """Extract compact keyword tags from text."""
    tl = (text or "").lower()
    tokens = re.findall(r"[a-z]{3,}", tl)
    tokens = [t for t in tokens if t not in _STOPWORDS]
    if not tokens:
        return []

    counts = Counter(tokens)
    tags = [w for w, _n in counts.most_common(max_tags)]

    # Add domain tags (if relevant) but keep bounded.
    for tag, pat in _DOMAIN_TAGS:
        if len(tags) >= max_tags:
            break
        if re.search(pat, tl):
            if tag not in tags:
                tags.append(tag)

    # De-dupe preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_tags:
            break
    return out


def analyze_web_training_item(*, topic: str, title: str, snippet: str, summary: str) -> dict:
    """Return a small enrichment payload for a stored web-training item."""
    topic = (topic or "").strip()
    title = (title or "").strip()
    snippet = (snippet or "").strip()
    summary = (summary or "").strip()

    base = "\n".join([x for x in [topic, title, summary, snippet] if x]).strip()
    tags = extract_tags(base, max_tags=10)

    # Insight: prefer the first 1-2 sentences of summary, else snippet.
    src = summary or snippet
    sents = _sentences(src)
    if sents:
        insight = " ".join(sents[:2]).strip()
    else:
        insight = src

    # Keep it compact and add a header if we have a title/topic.
    header = title or topic
    insight = _shorten(insight, 360)
    if header and insight:
        insight = _shorten(f"{header}: {insight}", 420)

    return {"analysis_tags": tags, "analysis_insight": insight}
