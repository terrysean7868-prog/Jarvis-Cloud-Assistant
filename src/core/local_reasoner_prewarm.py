from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlparse

from ..config import runtime_defaults as rd
from ..internet.internet import get_internet
from ..internet.web_scraper import close_scraper
from ..utils.db import db


def _normalize_alias(text: str) -> str:
    s = (text or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[^a-z0-9@._\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80]


def _title_candidates(title: str) -> list[str]:
    t = (title or "").strip()
    if not t:
        return []
    # Keep compact left/right chunks around separators.
    parts = re.split(r"\s+[\-|:|\u2013|\u2014]\s+", t)
    out: list[str] = []
    for p in parts[:2]:
        n = _normalize_alias(p)
        if n and len(n) >= 3:
            out.append(n)
    return list(dict.fromkeys(out))


def _domain_aliases(url: str) -> list[str]:
    try:
        host = (urlparse(url).netloc or "").lower().strip()
    except Exception:
        host = ""
    if not host:
        return []
    host = re.sub(r"^www\.", "", host)
    base = host.split(":", 1)[0]
    labels = base.split(".")
    aliases: list[str] = []
    if labels:
        aliases.append(_normalize_alias(labels[0]))
    aliases.append(_normalize_alias(base))
    return [a for a in list(dict.fromkeys(aliases)) if a]


def merge_site_aliases(state: dict[str, Any], aliases: list[str], url: str, score_inc: float = 1.0) -> bool:
    if not isinstance(state, dict):
        return False
    if not url:
        return False
    site_aliases = state.get("site_aliases") if isinstance(state.get("site_aliases"), dict) else {}
    changed = False
    now = datetime.now(timezone.utc).isoformat()
    for raw in aliases:
        a = _normalize_alias(raw)
        if not a or len(a) < 3:
            continue
        item = site_aliases.get(a) if isinstance(site_aliases.get(a), dict) else {"url": url, "score": 0.0}
        item["url"] = url
        item["score"] = round(float(item.get("score") or 0.0) + float(score_inc), 4)
        item["updated_at"] = now
        site_aliases[a] = item
        changed = True
    if changed:
        state["site_aliases"] = site_aliases
    return changed


def _state_key_for_prewarm() -> str:
    raw = str(getattr(rd, "LOCAL_REASONER_STATE_KEY", "global") or "global").strip().lower()
    # Prewarm should seed shared/global base knowledge. User-specific learning still
    # grows later from interactions.
    if raw in {"user", "per_user", "user_scoped", "userscoped"}:
        return "global"
    return raw or "global"


def _queries_from_documents(docs: list[dict], max_queries: int) -> list[str]:
    def _clean_signal_phrase(s: str) -> str:
        q = _normalize_alias(s)
        if not q:
            return ""
        # Remove conversational filler/noise frequently seen in prompts.
        q = re.sub(r"\b(yes|no|please|kindly|give me|tell me|i want|i need|about|latest knowledge|knowledge|results?)\b", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        toks = [t for t in re.findall(r"[a-z0-9]+", q) if t]
        if len(toks) < 2 and not (len(toks) == 1 and len(toks[0]) >= 4):
            return ""
        # Keep phrases compact and web-search friendly.
        return " ".join(toks[:6]).strip()

    topic_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}

    for d in docs or []:
        if not isinstance(d, dict):
            continue
        topic = _clean_signal_phrase(str(d.get("topic") or ""))
        if topic and len(topic) >= 4:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1

        tags = d.get("analysis_tags")
        if isinstance(tags, list):
            for t in tags:
                tag = _clean_signal_phrase(str(t or ""))
                if tag and len(tag) >= 3:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

    ranked_topics = [k for k, _ in sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)]
    ranked_tags = [k for k, _ in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)]

    out: list[str] = []
    for t in ranked_topics[: max(1, max_queries // 2)]:
        out.append(f"latest {t} official documentation")
    for t in ranked_tags[: max(1, max_queries // 2)]:
        out.append(f"{t} official website")

    # De-dup while preserving order.
    dedup = []
    seen = set()
    for q in out:
        q2 = q.strip().lower()
        if q2 and q2 not in seen:
            seen.add(q2)
            dedup.append(q)
    return dedup[: max(1, int(max_queries))]


def _build_systematic_queries(max_queries: int) -> list[str]:
    curated = [
        "official website github",
        "official python documentation",
        "official npm documentation",
        "stack overflow official site",
        "openai official website",
        "microsoft developer documentation",
        "react official documentation",
        "fastapi official documentation",
    ]

    dynamic: list[str] = []
    try:
        db._ensure_connected()
        if getattr(db, "db", None) is not None:
            cutoff = datetime.utcnow() - timedelta(days=21)
            docs = list(
                db.db.web_training_data.find(
                    {
                        "$or": [
                            {"analysis_tags": {"$exists": True}},
                            {"analysis_insight": {"$exists": True}},
                        ],
                        "fetched_at": {"$gte": cutoff},
                    },
                    {"topic": 1, "analysis_tags": 1},
                )
                .sort("fetched_at", -1)
                .limit(250)
            )
            dynamic = _queries_from_documents(docs, max_queries=max_queries)
    except Exception:
        dynamic = []

    merged = dynamic + curated
    out: list[str] = []
    seen = set()
    for q in merged:
        q2 = q.strip().lower()
        if q2 and q2 not in seen:
            seen.add(q2)
            out.append(q)
        if len(out) >= max(1, int(max_queries)):
            break
    return out


async def prewarm_local_reasoner_from_web(*, max_queries: int = 6, results_per_query: int = 4) -> dict[str, Any]:
    """Seed local reasoner site aliases from live web search + scraping.

    - Stores searchable snippets in web_training_data.
    - Stores alias->url mappings in local_reasoner_state (DB shared state).
    """
    report = {
        "queries": 0,
        "results_seen": 0,
        "summaries_saved": 0,
        "aliases_added": 0,
        "state_key": _state_key_for_prewarm(),
    }

    queries = _build_systematic_queries(max(1, int(max_queries)))

    # Load existing state (or default skeleton)
    state = db.local_reasoner_state_get(report["state_key"]) or {
        "version": 1,
        "updated_at": "",
        "last_maintenance_day": "",
        "app_aliases": {},
        "site_aliases": {},
        "stats": {"learn_events": 0, "hits": 0},
    }

    internet = await get_internet()
    try:
        for q in queries:
            report["queries"] += 1
            try:
                results = await internet.search(q, num_results=max(1, int(results_per_query)))
            except Exception:
                results = []
            for r in results:
                report["results_seen"] += 1
                title = str(r.get("title") or "").strip()
                url = str(r.get("url") or "").strip()
                snippet = str(r.get("snippet") or "").strip()
                if not url:
                    continue

                # Scrape short summary for better cold-start context in web_training_data.
                summary = ""
                try:
                    page = await internet.fetch_webpage(url, include_content=True)
                    if isinstance(page, dict):
                        summary = str(page.get("summary") or "").strip()[:1200]
                except Exception:
                    summary = ""

                try:
                    db.save_web_training_item(
                        topic=q,
                        title=title,
                        snippet=snippet[:500],
                        summary=summary,
                        url=url,
                        source="local_reasoner_prewarm",
                    )
                    report["summaries_saved"] += 1
                except Exception:
                    pass

                aliases = []
                aliases.extend(_title_candidates(title))
                aliases.extend(_domain_aliases(url))
                # Query-based alias as fallback (e.g., "python documentation")
                aliases.append(_normalize_alias(re.sub(r"\bofficial\b", "", q, flags=re.IGNORECASE)))

                if merge_site_aliases(state, aliases, url, score_inc=1.0):
                    report["aliases_added"] += 1
    finally:
        try:
            await close_scraper()
        except Exception:
            pass

    # Keep alias table bounded.
    max_aliases = int(getattr(rd, "LOCAL_REASONER_MAX_ALIASES", 400) or 400)
    site_aliases = state.get("site_aliases") if isinstance(state.get("site_aliases"), dict) else {}
    if len(site_aliases) > max_aliases:
        ordered = sorted(site_aliases.items(), key=lambda x: float((x[1] or {}).get("score") or 0.0), reverse=True)
        state["site_aliases"] = dict(ordered[:max_aliases])

    db.local_reasoner_state_upsert(state, state_key=report["state_key"])
    return report
