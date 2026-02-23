from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter
from pydantic import BaseModel


class SearchRequest(BaseModel):
    query: str
    num_results: int | None = 5
    session_id: str | None = None


class FetchRequest(BaseModel):
    url: str
    include_content: bool | None = True
    session_id: str | None = None


def build_internet_router(require_voice_session: Callable[[str | None], Any]) -> APIRouter:
    router = APIRouter(tags=["internet"])

    @router.post("/api/internet/search")
    async def search_web(req: SearchRequest):
        require_voice_session(req.session_id)
        try:
            from src.internet.internet import InternetAccess

            internet = InternetAccess()
            await internet.initialize()
            results = await internet.search(req.query, num_results=req.num_results or 5)
            await internet.close()
            return {
                "status": "success",
                "query": req.query,
                "results": results,
                "count": len(results),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @router.post("/api/internet/fetch")
    async def fetch_webpage(req: FetchRequest):
        require_voice_session(req.session_id)
        try:
            from src.internet.internet import InternetAccess

            internet = InternetAccess()
            await internet.initialize()
            result = await internet.fetch_webpage(req.url, include_content=req.include_content or True)
            await internet.close()
            return {
                "status": "success",
                "url": req.url,
                "content": result,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @router.post("/api/internet/search-summarize")
    async def search_and_summarize(req: SearchRequest):
        require_voice_session(req.session_id)
        try:
            from src.internet.internet import InternetAccess

            internet = InternetAccess()
            await internet.initialize()
            results = await internet.search_and_summarize(req.query, num_results=req.num_results or 3)
            await internet.close()
            return {
                "status": "success",
                "query": req.query,
                "results": results,
                "count": len(results),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @router.get("/api/internet/news")
    async def get_news_endpoint(topic: str = "latest", num_results: int = 5, session_id: str | None = None):
        require_voice_session(session_id)
        try:
            from src.internet.internet import InternetAccess

            internet = InternetAccess()
            await internet.initialize()
            news = await internet.get_news(topic, num_results)
            await internet.close()
            return {
                "status": "success",
                "topic": topic,
                "news": news,
                "count": len(news),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return router
