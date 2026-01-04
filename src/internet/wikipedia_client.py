"""Wikipedia ingestion helpers.

Design goals:
- Use the official Wikipedia APIs (not HTML scraping).
- Store only compact summaries + URLs (no full-page mirroring).
- Keep requests bounded (max pages per topic) to avoid large crawls.

This is meant for building a small RAG-lite cache in MongoDB via
`Database.save_web_training_item()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp
import os
from urllib.parse import quote


WIKI_API_BASE = "https://{lang}.wikipedia.org"
WIKI_ACTION_API = WIKI_API_BASE + "/w/api.php"
WIKI_REST_SUMMARY = WIKI_API_BASE + "/api/rest_v1/page/summary/{title}"


def _wiki_headers() -> dict[str, str]:
    """Return Wikimedia-compliant request headers.

    Wikimedia endpoints may return 403 to generic or missing User-Agent headers.
    Provide a descriptive UA with a contact method. Allow override via env.
    """

    ua = (
        os.getenv("JARVIS_WIKI_USER_AGENT")
        or os.getenv("WIKIPEDIA_USER_AGENT")
        or "Jarvis-Cloud-Assistant/1.0 (https://github.com; contact: admin@example.com)"
    )
    ua = (ua or "").strip() or "Jarvis-Cloud-Assistant/1.0 (https://github.com; contact: admin@example.com)"

    return {
        "User-Agent": ua,
        "Accept": "application/json",
    }


@dataclass(frozen=True)
class WikiSummary:
    title: str
    url: str
    extract: str
    description: str | None = None


def _clamp_int(v: int | None, lo: int, hi: int, default: int) -> int:
    try:
        iv = int(v) if v is not None else default
    except Exception:
        iv = default
    return max(lo, min(hi, iv))


def _safe_lang(lang: str | None) -> str:
    l = (lang or "en").strip().lower()
    # Keep conservative: Wikipedia language subdomains are usually 2-3 letters.
    if not l or len(l) > 10 or any(c for c in l if not (c.isalnum() or c in ("-",))):
        return "en"
    return l


async def _get_json(session: aiohttp.ClientSession, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        async with session.get(url, params=params) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


async def wikipedia_search_titles(topic: str, *, lang: str = "en", limit: int = 3) -> list[str]:
    """Return up to `limit` Wikipedia page titles for a topic."""
    q = (topic or "").strip()
    if not q:
        return []

    lang = _safe_lang(lang)
    limit = _clamp_int(limit, 1, 10, 3)

    params = {
        "action": "query",
        "list": "search",
        "srsearch": q,
        "srlimit": str(limit),
        "format": "json",
        "utf8": "1",
    }

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=_wiki_headers()) as session:
        data = await _get_json(session, WIKI_ACTION_API.format(lang=lang), params=params)
        if not data:
            return []
        items = (((data.get("query") or {}).get("search")) or [])
        titles: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = str(it.get("title") or "").strip()
            if t and t not in titles:
                titles.append(t)
        return titles


async def wikipedia_fetch_summary(title: str, *, lang: str = "en") -> WikiSummary | None:
    """Fetch the REST summary for a Wikipedia title."""
    t = (title or "").strip()
    if not t:
        return None

    lang = _safe_lang(lang)
    # Wikipedia REST expects the title in the path; quote it.
    title_path = quote(t.replace(" ", "_"), safe="")

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, headers=_wiki_headers()) as session:
        data = await _get_json(session, WIKI_REST_SUMMARY.format(lang=lang, title=title_path))
        if not data:
            return None

        extract = str(data.get("extract") or "").strip()
        title_out = str(data.get("title") or t).strip()
        desc = (data.get("description") or None)
        if isinstance(desc, str):
            desc = desc.strip() or None

        url = ""
        try:
            cu = data.get("content_urls") or {}
            desktop = cu.get("desktop") or {}
            url = str(desktop.get("page") or "").strip()
        except Exception:
            url = ""

        if not url:
            url = f"{WIKI_API_BASE.format(lang=lang)}/wiki/{quote(title_out.replace(' ', '_'), safe='')}"

        if not extract:
            return None

        return WikiSummary(title=title_out, url=url, extract=extract, description=desc)


async def wikipedia_topic_summaries(topic: str, *, lang: str = "en", max_pages: int = 2) -> list[WikiSummary]:
    """Search a topic and return up to `max_pages` summaries."""
    max_pages = _clamp_int(max_pages, 1, 5, 2)
    titles = await wikipedia_search_titles(topic, lang=lang, limit=max_pages)
    out: list[WikiSummary] = []
    for t in titles:
        s = await wikipedia_fetch_summary(t, lang=lang)
        if s:
            out.append(s)
    return out
