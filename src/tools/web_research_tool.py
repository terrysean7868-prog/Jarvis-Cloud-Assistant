from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

try:
    import aiohttp
except Exception:
    aiohttp = None


class Tool:
    name = "web_research"
    description = "Performs lightweight OSS web lookup using DuckDuckGo HTML search."

    async def run(self, **kwargs: Any) -> dict[str, Any]:
        query = str(kwargs.get("query") or "").strip()
        limit = max(1, min(10, int(kwargs.get("limit") or 5)))
        if not query:
            return {"status": "error", "message": "query is required"}
        if aiohttp is None:
            return {"status": "error", "message": "aiohttp is not available"}

        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {"User-Agent": "Jarvis-Autonomy/1.0"}

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return {"status": "error", "message": f"search failed: {resp.status}"}
                    html = await resp.text()

            links = []
            for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE | re.DOTALL):
                href = re.sub(r"\s+", " ", m.group(1) or "").strip()
                title = re.sub(r"<[^>]+>", "", m.group(2) or "")
                title = re.sub(r"\s+", " ", title).strip()
                if href and title:
                    links.append({"title": title, "url": href})
                if len(links) >= limit:
                    break

            return {"status": "success", "query": query, "results": links}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}


tool = Tool()
