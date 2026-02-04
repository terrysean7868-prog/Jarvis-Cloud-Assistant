"""Offline analysis/synthesis helpers.

Purpose:
- Reduce dependency on the LLM for high-level informational tasks.
- When web_search/fetch_url results exist (or LLM is rate-limited), synthesize a concise,
  contract-compliant answer from snippets + URLs.

This module is intentionally lightweight (stdlib-only) and heuristic-based.
"""

from __future__ import annotations

import re
from typing import Any


_URL_RE = re.compile(r"https?://\S+")


def _shorten(text: str, *, max_chars: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    if len(t) <= max_chars:
        return t
    return t[: max(0, max_chars - 1)].rstrip() + "…"


def _dedupe_sentences(text: str, *, max_sentences: int = 6) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""

    parts = re.split(r"(?<=[\.!\?])\s+", t)
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        s = re.sub(r"\s+", " ", (p or "").strip())
        if not s:
            continue
        key = re.sub(r"\W+", "", s).lower()[:180]
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= max_sentences:
            break
    return " ".join(out).strip()


def _compress_semver_lists(text: str) -> str:
    """Reduce noisy pages that repeat long version lists.

    If we see many semver-like tokens, keep only the top few unique versions.
    """
    t = (text or "").strip()
    if not t:
        return ""

    versions = [m.group(1) for m in re.finditer(r"\b(v?\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)\b", t, flags=re.IGNORECASE)]
    if len(versions) < 10:
        return t

    uniq: list[str] = []
    seen: set[str] = set()
    for v in versions:
        vv = v.lower()
        if vv in seen:
            continue
        seen.add(vv)
        uniq.append(v)

    ranked: list[tuple[tuple[int, int, int], str]] = []
    for v in uniq:
        sv = _parse_semver(v)
        if sv:
            ranked.append((sv, v))
    ranked.sort(reverse=True)
    top = [v for _sv, v in ranked[:6]]
    if not top:
        return t

    return f"(Page contains many versions; sampled: {', '.join(top)})"


def _normalize_snippet(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t)
    t = _compress_semver_lists(t)
    t = _dedupe_sentences(t, max_sentences=6)
    return _shorten(t, max_chars=360)


def _format_compact_number(n: str) -> str:
    s = (n or "").strip()
    s = s.replace(",", "")
    # Keep as-is if not numeric.
    if not re.fullmatch(r"\d+(?:\.\d+)?", s):
        return (n or "").strip()
    # Re-insert commas for integers.
    if "." not in s:
        try:
            return f"{int(s):,}"
        except Exception:
            return (n or "").strip()
    return s


def _extract_data_points_from_text(text: str) -> list[str]:
    """Extract lightweight factual tokens from a snippet.

    The goal is NOT perfect IE; it's to surface concrete values seen in snippets
    (versions, dates, currencies, percentages, magnitudes) to improve trust.
    """
    t = (text or "").strip()
    if not t:
        return []

    pts: list[str] = []

    # Versions: v20.11.0 / 20.11.0 / 2025.1 etc.
    for m in re.finditer(r"\b(v?\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)\b", t, flags=re.IGNORECASE):
        v = m.group(1)
        if v and v.lower() not in {"v1.0"}:
            pts.append(f"version {v}")

    # Dates: ISO and common textual dates.
    for m in re.finditer(r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b", t):
        pts.append(f"date {m.group(1)}")
    for m in re.finditer(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d{1,2},?\s+20\d{2}\b",
        t,
        flags=re.IGNORECASE,
    ):
        pts.append(f"date {m.group(0)}")

    # Percentages.
    for m in re.finditer(r"\b\d{1,3}(?:\.\d+)?%\b", t):
        pts.append(f"{m.group(0)}")

    # Money: $105,000 / $3.5 trillion / €1.2B etc.
    money_pat = r"(?:\$|€|£|₹)\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:k|m|b|t|thousand|million|billion|trillion)?\b"
    for m in re.finditer(money_pat, t, flags=re.IGNORECASE):
        pts.append(m.group(0).replace("  ", " ").strip())

    # Large magnitudes without currency: 3.5 trillion, 930B, 105,000.
    mag_pat = r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d+(?:\.\d+)?\s*(?:k|m|b|t|thousand|million|billion|trillion)\b"
    for m in re.finditer(mag_pat, t, flags=re.IGNORECASE):
        val = m.group(0).strip()
        # Avoid duplicating money values already captured.
        if any(val in p for p in pts):
            continue
        pts.append(val)

    # De-dupe while keeping order.
    pts = _dedupe_keep_order([re.sub(r"\s+", " ", p).strip() for p in pts])
    return pts


def _collect_data_points(items: list[dict[str, str]], *, max_points: int = 6) -> list[str]:
    pts: list[str] = []
    for it in items[:6]:
        s = (it.get("snippet") or "").strip()
        if not s:
            continue
        for p in _extract_data_points_from_text(s):
            if p not in pts:
                pts.append(p)
            if len(pts) >= max_points:
                return pts
    return pts


def _collect_data_points_grouped(items: list[dict[str, str]], *, max_per_group: int = 4) -> list[str]:
    grouped: dict[str, list[str]] = {
        "Versions": [],
        "Dates": [],
        "Money": [],
        "Percent": [],
        "Magnitudes": [],
    }
    for it in items[:8]:
        s = (it.get("snippet") or "").strip()
        if not s:
            continue
        for p in _extract_data_points_from_text(s):
            if p.startswith("version "):
                v = p.replace("version ", "", 1).strip()
                if v and v not in grouped["Versions"] and len(grouped["Versions"]) < max_per_group:
                    grouped["Versions"].append(v)
            elif p.startswith("date "):
                d = p.replace("date ", "", 1).strip()
                if d and d not in grouped["Dates"] and len(grouped["Dates"]) < max_per_group:
                    grouped["Dates"].append(d)
            elif re.match(r"^(\$|€|£|₹)", p):
                if p not in grouped["Money"] and len(grouped["Money"]) < max_per_group:
                    grouped["Money"].append(p)
            elif p.endswith("%"):
                if p not in grouped["Percent"] and len(grouped["Percent"]) < max_per_group:
                    grouped["Percent"].append(p)
            else:
                if p not in grouped["Magnitudes"] and len(grouped["Magnitudes"]) < max_per_group:
                    grouped["Magnitudes"].append(p)

    lines: list[str] = []
    if grouped["Versions"]:
        lines.append(f"Versions: {', '.join(grouped['Versions'])}")
    if grouped["Dates"]:
        lines.append(f"Dates: {', '.join(grouped['Dates'])}")
    if grouped["Money"]:
        lines.append(f"Money: {', '.join(grouped['Money'])}")
    if grouped["Percent"]:
        lines.append(f"Percent: {', '.join(grouped['Percent'])}")
    if grouped["Magnitudes"]:
        lines.append(f"Magnitudes: {', '.join(grouped['Magnitudes'])}")
    return lines


def _extract_web_search_items(tool_results: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for r in tool_results or []:
        if not isinstance(r, dict):
            continue
        if (r.get("status") or "").lower() != "success":
            continue
        action = (r.get("action") or r.get("action_type") or "").lower()
        if action not in {"web_search", "search"}:
            continue

        for item in (r.get("results") or []) or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            if not (title or url or snippet):
                continue
            items.append({"title": title, "url": url, "snippet": snippet})
    return items


def _extract_fetch_url_items(tool_results: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for r in tool_results or []:
        if not isinstance(r, dict):
            continue
        if (r.get("status") or "").lower() != "success":
            continue
        action = (r.get("action") or r.get("action_type") or "").lower()
        if action != "fetch_url":
            continue

        url = str(r.get("url") or "").strip()
        title = str(r.get("title") or "").strip()
        summary = str(r.get("summary") or "").strip()
        if not (url or title or summary):
            continue
        # Treat fetched summaries as higher-signal snippets.
        items.append({"title": title, "url": url, "snippet": summary})
    return items


def _dedupe_keep_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        v = (v or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _is_market_analysis_request(user_text: str) -> bool:
    t = (user_text or "").lower()
    return bool(
        re.search(
            r"\b(crypto|bitcoin|ethereum|btc|eth|altcoin|market|markets|price|chart|sentiment|trend|outlook|scenario|bull|bear|base)\b",
            t,
        )
    )


def _is_high_level_request(user_text: str) -> bool:
    t = (user_text or "").lower()
    return bool(
        re.search(
            r"\b(analyze|analysis|research|compare|strategy|roadmap|tradeoff|trade\-offs|pros and cons|recommend|evaluation|outlook|forecast)\b",
            t,
        )
    )


def _is_specific_fact_request(user_text: str) -> bool:
    tl = (user_text or "").lower()
    # "Specific fact" is broader than just versions/prices: release dates, EOL, compatibility, requirements, etc.
    time_sensitive = bool(re.search(r"\b(latest|current|as\s+of\s+today|as\s+of\s+now|today|now)\b", tl))
    fact_terms = bool(
        re.search(
            r"\b(version|release|price|rate|cost|market\s+cap|marketcap|cap|value|"
            r"release\s+date|released\s+on|when\s+was|announcement|announced|published|"
            r"eol|end\s+of\s+life|end\-of\-life|support\s+ends|supported\s+until|"
            r"compatible|compatibility|requirements?|minimum|supported\s+versions?)\b",
            tl,
        )
    )
    return bool((time_sensitive and fact_terms) or re.search(r"\b(release\s+date|when\s+was|eol|end\s+of\s+life|compatible|compatibility|requirements?|minimum)\b", tl))


def _pick_best_date_from_text(text: str) -> str | None:
    t = (text or "").strip()
    if not t:
        return None

    # Prefer dates near release-ish keywords.
    release_context = []
    for m in re.finditer(
        r"(?i)(released\s+on|release\s+date|announced\s+on|published\s+on)[^\.\n]{0,120}",
        t,
    ):
        release_context.append(m.group(0))

    hay = " ".join(release_context) if release_context else t
    hay = re.sub(r"\s+", " ", hay)

    # ISO first.
    m = re.search(r"\b(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\b", hay)
    if m:
        return m.group(1)

    # Month name dates.
    m = re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d{1,2},?\s+20\d{2}\b",
        hay,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(0)

    return None


def _extract_requirement_claims(items: list[dict[str, str]], *, max_claims: int = 3) -> list[str]:
    claims: list[str] = []
    pat = re.compile(
        r"(?i)\b(requires|requirement|minimum|recommended|supported\s+on|supported\s+versions?|supports|compatible\s+with|works\s+with)\b"
    )
    for it in items[:10]:
        s = (it.get("snippet") or "").strip()
        if not s:
            continue
        s = _normalize_snippet(s)
        if not s:
            continue
        if pat.search(s):
            line = _shorten(s, max_chars=220)
            if line and line not in claims:
                claims.append(line)
        if len(claims) >= max_claims:
            break
    return claims


def _extract_comparison_options(user_text: str) -> tuple[str, str] | None:
    t = (user_text or "").strip()
    if not t:
        return None
    tl = t.lower()

    m = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:[\?\.!]|$)", tl, flags=re.IGNORECASE)
    if m:
        a = (m.group(1) or "").strip()
        b = (m.group(2) or "").strip()
        if a and b:
            return (_shorten(a, max_chars=50), _shorten(b, max_chars=50))

    m = re.search(r"(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?:[\?\.!]|$)", t, flags=re.IGNORECASE)
    if m:
        a = (m.group(1) or "").strip()
        b = (m.group(2) or "").strip()
        # Avoid grabbing the entire prompt; keep the last chunk before vs.
        a = re.split(r"[\n\r]", a)[0].strip()
        a = a.split(":")[-1].strip()
        if len(a) > 60:
            a = " ".join(a.split()[-5:]).strip()
        if a and b:
            return (_shorten(a, max_chars=50), _shorten(b, max_chars=50))

    # Conservative "X or Y" handling: only if phrased like "which should I choose".
    if re.search(r"\b(which\s+is\s+better|which\s+should\s+i\s+choose|choose\s+between)\b", tl):
        m = re.search(r"\b(.+?)\s+or\s+(.+?)(?:[\?\.!]|$)", t, flags=re.IGNORECASE)
        if m:
            a = (m.group(1) or "").strip()
            b = (m.group(2) or "").strip()
            if a and b and len(a) <= 60 and len(b) <= 60:
                return (_shorten(a, max_chars=50), _shorten(b, max_chars=50))
    return None


def _extract_criteria(user_text: str, *, max_criteria: int = 4) -> list[str]:
    tl = (user_text or "").lower()
    if not tl:
        return []

    catalog = [
        ("price", ("price", "cost", "budget", "cheap", "expensive")),
        ("performance", ("performance", "speed", "fast", "latency", "fps")),
        ("stability", ("stable", "stability", "reliable", "crash")),
        ("security", ("security", "secure", "privacy", "vulnerability")),
        ("compatibility", ("compatible", "compatibility", "works with", "support", "supported", "requirements", "minimum")),
        ("features", ("features", "capabilities", "functionality")),
        ("ease", ("easy", "easier", "learning curve", "setup", "configuration")),
        ("support", ("support", "docs", "documentation", "community")),
    ]

    out: list[str] = []
    for name, keys in catalog:
        if any(k in tl for k in keys):
            out.append(name)
        if len(out) >= max_criteria:
            break
    return out


def _select_source_urls(urls: list[str], user_text: str) -> list[str]:
    """Pick up to 2 URLs, preferring primary/reference sources when possible."""
    u = _dedupe_keep_order([x.rstrip(").,;") for x in (urls or []) if (x or "").strip()])
    if not u:
        return []

    tl = (user_text or "").strip().lower()
    prefer: list[str] = [
        "github.com",
        "docs.",
        "developer.",
        "python.org",
        "nodejs.org",
        "microsoft.com",
        "mozilla.org",
        "developer.mozilla.org",
        "w3schools.com",
        "wikipedia.org",
    ]

    # Topic-specific preference.
    if re.search(r"\b(history|historical|psychology|psychological|earth\s+crisis|climate|pandemic|war|conflict)\b", tl):
        prefer = ["wikipedia.org"] + [p for p in prefer if p != "wikipedia.org"]
    if re.search(r"\b(code|coding|programming|python|javascript|js|html|css|api|tutorial|docs|documentation)\b", tl):
        prefer = ["developer.mozilla.org", "mozilla.org", "python.org", "w3schools.com", "github.com"] + [
            p
            for p in prefer
            if p not in {"developer.mozilla.org", "mozilla.org", "python.org", "w3schools.com", "github.com"}
        ]

    def _rank(url: str) -> int:
        ul = (url or "").lower()
        for idx, dom in enumerate(prefer):
            if dom in ul:
                return idx
        return 10_000

    return sorted(u, key=_rank)[:2]


def _parse_semver(v: str) -> tuple[int, int, int] | None:
    s = (v or "").strip().lstrip("v").strip()
    m = re.fullmatch(r"(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?", s)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2))
    c = int(m.group(3) or 0)
    return (a, b, c)


def _pick_best_version_from_text(text: str) -> str | None:
    cand: list[str] = []
    for m in re.finditer(r"\b(v?\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)\b", text or "", flags=re.IGNORECASE):
        cand.append(m.group(1))
    best = None
    best_t = None
    for c in cand:
        t = _parse_semver(c)
        if not t:
            continue
        if best_t is None or t > best_t:
            best_t = t
            best = c
    return best


def synthesize_from_web(user_text: str, tool_results: list[dict[str, Any]], *, found: bool) -> str:
    """Create a best-effort structured answer from web tool results.

    Contract:
    - If found: starts with "I found this:" and includes 1-2 Source URLs.
    - If not found: starts with "Not found this:" and asks exactly one question.

    This function avoids personalized financial advice; it is informational.
    """

    if not found:
        return (
            "Not found this: No usable web results were returned. "
            "What exactly should I look for (asset/topic + timeframe or a specific question)?"
        )

    # Include both search result snippets and fetched-page summaries.
    items = _extract_fetch_url_items(tool_results) + _extract_web_search_items(tool_results)
    urls = _dedupe_keep_order([i.get("url", "") for i in items])
    urls = [u.rstrip(").,;") for u in urls if u]
    source_urls = _select_source_urls(urls, user_text)

    snippets = [i.get("snippet", "") for i in items if (i.get("snippet") or "").strip()]
    snippets = [_normalize_snippet(s) for s in snippets]
    snippets = [s for s in snippets if s]

    data_points = _collect_data_points(items)
    data_points_grouped = _collect_data_points_grouped(items)

    # Build a compact, non-hallucinated summary from snippets.
    summary_bits: list[str] = []
    for s in snippets[:3]:
        if len(s) < 40:
            continue
        summary_bits.append(s)
        if len(" ".join(summary_bits)) > 260:
            break

    summary = " ".join(summary_bits).strip()
    if not summary:
        # Fallback to describing what we did.
        summary = (user_text or "").strip()
        summary = summary[:160] + ("..." if len(summary) > 160 else "")

    comparison = _extract_comparison_options(user_text)
    requirement_claims: list[str] = []

    # If the user wants a specific fact, try to answer directly (best-effort).
    if _is_specific_fact_request(user_text):
        tl = (user_text or "").lower()
        joined = " ".join([i.get("snippet") or "" for i in items[:8]] + [i.get("title") or "" for i in items[:8]])
        joined = re.sub(r"\s+", " ", joined).strip()

        # 1) Dates first (avoid treating "release date" as a version/release request).
        if re.search(r"\b(release\s+date|released\s+on|when\s+was|announced|published)\b", tl):
            d = _pick_best_date_from_text(joined)
            if d:
                summary = f"The release/announcement date mentioned in the sources is {d}."
        # 2) EOL/support date.
        elif re.search(r"\b(eol|end\s+of\s+life|end\-of\-life|supported\s+until|support\s+ends)\b", tl):
            d = _pick_best_date_from_text(joined)
            if d:
                summary = f"The support/EOL-related date mentioned in the sources is {d}."
        # 3) Compatibility/requirements.
        elif re.search(r"\b(compatible|compatibility|requirements?|minimum|supported\s+versions?|works\s+with|requires)\b", tl):
            requirement_claims = _extract_requirement_claims(items)

            # If it's a yes/no compatibility question, provide a cautious synthesis.
            if re.search(r"\b(is|are|does|do)\b", tl) and "?" in (user_text or ""):
                # We can't prove true/false; we can only summarize what sources indicate.
                if any(re.search(r"\bwindows\s*11\b", (it.get("snippet") or "").lower()) for it in items[:10]):
                    summary = "The sources discuss installing/using it on Windows 11, which suggests it is compatible (verify against official requirements)."
                elif requirement_claims:
                    summary = "The sources mention compatibility/requirements details; see key points for the closest matching claim."
            else:
                if requirement_claims:
                    summary = "Compatibility/requirements are described in the sources (see key points)."

        # 4) Price/value.
        elif "price" in tl or "how much" in tl or "rate" in tl or "cost" in tl or "value" in tl:
            # Prefer currency-looking tokens.
            money = None
            for p in data_points:
                if re.match(r"^(\$|€|£|₹)", p):
                    money = p
                    break
            if not money:
                # Fallback: scan joined for currency.
                m = re.search(r"(?:\$|€|£|₹)\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:k|m|b|t|thousand|million|billion|trillion)?\b", joined, flags=re.IGNORECASE)
                money = m.group(0).strip() if m else None
            if money:
                summary = f"The most recent price/value mentioned in the sources is {re.sub(r'\s+', ' ', money).strip()}."
        # 5) Version/release (only when the user asked for versions/releases, not dates).
        elif re.search(r"\bversion\b", tl) or (
            re.search(r"\b(latest|current|today|as\s+of\s+now)\b", tl) and re.search(r"\brelease\b", tl)
        ):
            best_v = _pick_best_version_from_text(joined)
            if best_v:
                summary = f"The most relevant version mentioned in the sources appears to be {best_v}."
                if re.search(r"\b(latest|current|today|as\s+of\s+now)\b", tl):
                    summary = f"The latest listed version appears to be {best_v}."

    # Comparison / scoring (deterministic and based on snippets only).
    scorecard_lines: list[str] = []
    if comparison:
        a, b = comparison
        criteria = _extract_criteria(user_text)
        if not criteria:
            criteria = ["compatibility", "price", "performance", "support"]

        def _mentions_option(s: str, opt: str) -> bool:
            o = re.escape(opt.lower())
            return bool(re.search(rf"\b{o}\b", s.lower())) or (opt.lower() in s.lower())

        def _criterion_hit(s: str, crit: str) -> bool:
            cl = s.lower()
            table = {
                "price": ("price", "cost", "pricing", "free", "$", "€", "£", "₹"),
                "performance": ("performance", "fast", "faster", "speed", "latency"),
                "stability": ("stable", "stability", "reliable", "crash"),
                "security": ("security", "secure", "privacy", "vulnerability"),
                "compatibility": ("compatible", "compatibility", "works with", "supports", "supported", "requires", "minimum"),
                "features": ("feature", "features", "capability", "capabilities"),
                "ease": ("easy", "easier", "setup", "configure", "configuration", "learning curve"),
                "support": ("support", "docs", "documentation", "community"),
            }
            keys = table.get(crit, (crit,))
            return any(k in cl for k in keys)

        evidence: dict[str, list[str]] = {"A": [], "B": []}
        for it in items[:12]:
            s = " ".join([it.get("title") or "", it.get("snippet") or ""]).strip()
            s = _normalize_snippet(s)
            if not s:
                continue
            if _mentions_option(s, a):
                evidence["A"].append(s)
            if _mentions_option(s, b):
                evidence["B"].append(s)

        scores: dict[str, int] = {"A": 0, "B": 0}
        for k in ("A", "B"):
            # Base evidence count (capped) so we don't overfit noise.
            scores[k] += min(3, len(evidence[k]))
            for crit in criteria:
                hits = 0
                for s in evidence[k][:8]:
                    if _criterion_hit(s, crit):
                        hits += 1
                scores[k] += min(2, hits)

        winner = a if scores["A"] >= scores["B"] else b
        alt = b if winner == a else a
        summary = (
            f"Between {a} and {b}, the snippets provide slightly stronger support for {winner} "
            f"against your criteria ({', '.join(criteria[:4])}). "
            f"If you prioritize something else, {alt} may be a better fit."
        )

        scorecard_lines = [
            f"{a}: score {scores['A']} (evidence snippets: {len(evidence['A'])})",
            f"{b}: score {scores['B']} (evidence snippets: {len(evidence['B'])})",
        ]

    # Key points: derive from top snippets/titles (shortened and de-duplicated).
    key_points: list[str] = []
    for i in items[:5]:
        t = (i.get("title") or "").strip()
        s = (i.get("snippet") or "").strip()
        line = ""
        if t and s:
            line = f"{t}: {s}"
        elif s:
            line = s
        elif t:
            line = t
        line = _normalize_snippet(line)
        line = _shorten(line, max_chars=220)
        if line and line not in key_points:
            key_points.append(line)
        if len(key_points) >= 4:
            break

    if requirement_claims:
        for c in requirement_claims:
            if c not in key_points:
                key_points.append(c)
            if len(key_points) >= 6:
                break

    # Risks/assumptions (generic, safe).
    risks: list[str] = [
        "Sources may be incomplete or biased; verify with primary sources.",
        "Information can change quickly; treat this as a point-in-time summary.",
    ]

    include_scenarios = _is_market_analysis_request(user_text) and (
        "scenario" in (user_text or "").lower() or "bull" in (user_text or "").lower() or "bear" in (user_text or "").lower()
    )

    scenario_lines: list[str] = []
    if include_scenarios:
        scenario_lines = [
            "Bull: Risk-on sentiment persists (e.g., strong inflows / improving macro backdrop) and BTC/ETH hold key support levels.",
            "Base: Range-bound chop; catalysts are mixed and price reacts to macro data/ETF flows and liquidity conditions.",
            "Bear: Risk-off shock (macro tightening, adverse regulation, liquidity stress) breaks key supports and resets leverage.",
        ]

    # Format: structured and concise.
    lines: list[str] = []
    lines.append(f"I found this: {summary}")

    include_structured = _is_high_level_request(user_text) or _is_specific_fact_request(user_text) or bool(comparison)

    # If the summary is generic/weak, include structure anyway so users get something useful.
    try:
        if len((summary or "").strip()) < 120:
            include_structured = True
        if re.fullmatch(r"\s*ok(?:ay)?\.?\s*", (summary or ""), flags=re.IGNORECASE):
            include_structured = True
        if re.match(r"\s*ok(?:ay)?\b", (summary or ""), flags=re.IGNORECASE):
            include_structured = True
    except Exception:
        pass

    if include_structured:
        lines.append("")
        lines.append("Key points:")
        for p in key_points[:6]:
            lines.append(f"- {p}")

        if data_points_grouped:
            lines.append("")
            lines.append("Data points seen (from snippets):")
            for p in data_points_grouped[:6]:
                lines.append(f"- {p}")

        if scorecard_lines:
            lines.append("")
            lines.append("Scorecard (heuristic, from snippets):")
            for s in scorecard_lines[:4]:
                lines.append(f"- {s}")

        lines.append("")
        lines.append("Risks/assumptions:")
        for r in risks[:4]:
            lines.append(f"- {r}")

        if scenario_lines:
            lines.append("")
            lines.append("Scenarios (bull/base/bear):")
            for s in scenario_lines:
                lines.append(f"- {s}")

        # General decision guidance (works across domains).
        lines.append("")
        lines.append("Decision/next steps:")
        if re.search(r"\b(compare|choose|which|vs\.?|versus|best)\b", (user_text or "").lower()):
            lines.append("- Pick 2–4 criteria (cost, risk, performance, time) and validate each claim in the top sources.")
            lines.append("- If sources disagree, prefer official docs / primary announcements and the newest timestamped info.")
        elif re.search(r"\b(how\s+to|install|setup|configure|steps|troubleshoot|fix)\b", (user_text or "").lower()):
            lines.append("- Open the first source and follow the official/primary steps; use the second source to cross-check.")
            lines.append("- If you share your OS/app version and the exact error text, I can narrow it down further.")
        else:
            lines.append("- Review the top 1–2 sources, then ask for a deeper dive on any specific sub-question.")

        # Finance/crypto: avoid personalized advice.
        if _is_market_analysis_request(user_text):
            lines.append("")
            lines.append("Note: informational only, not financial advice.")

    # Always include 1-2 URLs.
    lines.append("")
    lines.append("Source URLs:")
    if source_urls:
        for idx, u in enumerate(source_urls[:2], start=1):
            lines.append(f"{idx}. {u}")
    else:
        # If URLs missing, try to extract any URLs from tool_results stringified.
        raw = str(tool_results)
        raw_urls = _dedupe_keep_order(_URL_RE.findall(raw))
        for idx, u in enumerate(raw_urls[:2], start=1):
            lines.append(f"{idx}. {u}")
        if len(lines) == 0:
            lines.append("1. (no source URL available)")

    return "\n".join(lines).strip()
